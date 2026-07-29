"""
Cost and Usage Report reader — actual billed cost per resource.

This is what turns Estimated savings into Confirmed ones. CUR carries, per
line item, both what you were charged (`line_item_unblended_cost`) and the
public on-demand cost for the identical usage
(`pricing_public_on_demand_cost`). The ratio of those two is the customer's
real effective discount, measured rather than assumed:

    effective_rate = unblended_cost / public_on_demand_cost
    saving         = actual_cost - (target_list_price x effective_rate)

Read-only by design: `s3:GetObject` on the CUR bucket and nothing else. No
Athena, no Glue table, no query-results bucket — the tool never writes to the
customer's account.

Formats: gzipped CSV is parsed with the standard library. Parquet needs
pyarrow; when it is absent that is reported as a gap rather than guessed at.
"""

import csv
import gzip
import io
import json
import os
import re
from collections import defaultdict

from analysis.provenance import gaps

# CUR 1.0 uses slash-separated names, CUR 2.0 uses snake_case. Accept both.
_COLUMNS = {
    "resource_id":  ("lineItem/ResourceId", "line_item_resource_id"),
    "unblended":    ("lineItem/UnblendedCost", "line_item_unblended_cost"),
    "public_cost":  ("pricing/publicOnDemandCost", "pricing_public_on_demand_cost"),
    "usage_type":   ("lineItem/UsageType", "line_item_usage_type"),
    "product_code": ("lineItem/ProductCode", "line_item_product_code"),
    "line_type":    ("lineItem/LineItemType", "line_item_line_item_type"),
    "ri_arn":       ("reservation/ReservationARN", "reservation_reservation_a_r_n"),
    "sp_arn":       ("savingsPlan/SavingsPlanARN", "savings_plan_savings_plan_a_r_n"),
    "region":       ("product/region", "product_region"),
}

# Guards against pulling an unbounded month of data. A DAILY export with
# OVERWRITE_REPORT is a handful of files; these caps only bite on very large
# organisations, and when they do the read fails closed (see `incomplete`
# below) rather than returning partial costs.
MAX_OBJECTS = 200
MAX_BYTES = 2 * 1024 * 1024 * 1024


def _short_id(resource_id):
    """
    Reduce a CUR resource identifier to the bare id the collectors use.

    arn:aws:ec2:...:instance/i-abc   -> i-abc
    arn:aws:rds:...:db:mydb          -> mydb
    arn:aws:lambda:...:function:myfn -> myfn
    my-bucket                        -> my-bucket
    """
    if not resource_id.startswith("arn:"):
        return resource_id
    tail = resource_id.split(":", 5)[-1] if resource_id.count(":") >= 5 else resource_id
    if "/" in tail:
        return tail.rsplit("/", 1)[-1]
    if ":" in tail:
        return tail.rsplit(":", 1)[-1]
    return tail


def _pick(header, names):
    for name in names:
        if name in header:
            return name
    lowered = {h.lower(): h for h in header}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _latest_prefix(s3, bucket, prefix):
    """
    Find the most recent billing-period folder under the report prefix.

    CUR lays data out as <prefix>/<report>/<YYYYMMDD-YYYYMMDD>/... so the
    lexically greatest period folder is the current month.
    """
    periods = set()
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"\d{8}-\d{8}", name):
                periods.add(cp["Prefix"])
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            break
    return max(periods) if periods else None


def _list_data_objects(s3, bucket, prefix):
    """Data files under a prefix, newest first, excluding manifests."""
    objects = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(("Manifest.json", ".json", "/")):
                continue
            if key.endswith((".csv.gz", ".csv.zip", ".csv", ".parquet", ".snappy.parquet")):
                objects.append(obj)
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            break
    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    return objects


def _rows_from_csv(body):
    text = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(body))
                            if body[:2] == b"\x1f\x8b" else io.BytesIO(body),
                            encoding="utf-8", errors="replace")
    reader = csv.DictReader(text)
    for row in reader:
        yield row


def _rows_from_parquet(body):
    try:
        import pyarrow.parquet as pq          # optional dependency
    except ImportError:
        raise RuntimeError("parquet")
    table = pq.read_table(io.BytesIO(body))
    columns = table.column_names
    for batch in table.to_batches():
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            yield {c: data[c][i] for c in columns}


def read(session, report, max_objects=MAX_OBJECTS):
    """
    Read the newest billing period of a discovered CUR.

    `report` is an entry from collectors.cur_discovery.detect()["best"].
    Returns {"available", "by_resource", "effective_rate", ...}.
    """
    bucket, prefix = report.get("bucket"), report.get("prefix") or ""
    if not bucket:
        return {"available": False, "reason": "CUR has no S3 bucket recorded"}

    region = report.get("region") or "us-east-1"
    s3 = session.client("s3", region_name=region)

    try:
        period_prefix = _latest_prefix(s3, bucket, prefix.rstrip("/") + "/")
        objects = _list_data_objects(s3, bucket, period_prefix or prefix)
    except Exception as e:
        gaps.add(category="Cost data", what="CUR objects",
                 why=f"Could not list s3://{bucket}/{prefix}: {str(e)[:150]}",
                 how_to_fix="Grant s3:ListBucket and s3:GetObject on the CUR bucket.",
                 impact="Savings stay list-price estimates.")
        return {"available": False, "reason": str(e)[:200]}

    if not objects:
        return {"available": False, "reason": "No CUR data files found yet "
                                              "(first delivery can take 24h)"}

    by_resource = defaultdict(lambda: {"unblended": 0.0, "public": 0.0,
                                       "covered": False, "usage_types": set()})
    total_unblended = total_public = 0.0
    read_bytes = 0
    parquet_skipped = False
    # A partially-read CUR is more dangerous than no CUR at all: the costs it
    # does produce are applied as MEASURED and shown as Confirmed, so any
    # resource whose rows sat in an unread file would silently understate.
    # Track every reason the read could be incomplete and fail closed below.
    incomplete = []

    if len(objects) > max_objects:
        incomplete.append(f"{len(objects)} data files exceed the {max_objects}-file cap")

    for obj in objects[:max_objects]:
        if read_bytes + obj["Size"] > MAX_BYTES:
            incomplete.append(
                f"total size exceeds the {MAX_BYTES // (1024 ** 3)} GiB read cap")
            break
        try:
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            read_bytes += len(body)
            if obj["Key"].endswith((".parquet", ".snappy.parquet")):
                rows = _rows_from_parquet(body)
            else:
                rows = _rows_from_csv(body)

            header = None
            cols = {}
            for row in rows:
                if header is None:
                    header = list(row.keys())
                    cols = {k: _pick(header, names) for k, names in _COLUMNS.items()}
                    if not cols.get("unblended"):
                        incomplete.append(
                            f"{obj['Key'].rsplit('/', 1)[-1]} has no recognisable "
                            f"cost column")
                        break
                rid = str(row.get(cols["resource_id"]) or "").strip()
                try:
                    unblended = float(row.get(cols["unblended"]) or 0)
                except (TypeError, ValueError):
                    unblended = 0.0
                try:
                    public = float(row.get(cols["public_cost"]) or 0) if cols.get("public_cost") else 0.0
                except (TypeError, ValueError):
                    public = 0.0

                total_unblended += unblended
                total_public += public
                if not rid:
                    continue
                # CUR reports resource IDs in whichever form the service uses:
                #   EC2/EBS   arn:aws:ec2:...:instance/i-abc     (slash)
                #   RDS       arn:aws:rds:...:db:mydb            (colon)
                #   Lambda    arn:aws:lambda:...:function:myfn   (colon)
                #   S3        my-bucket                          (bare)
                # Stripping only the slash form left RDS, Lambda and
                # ElastiCache keyed by full ARN, so their costs never matched
                # the collected resource and stayed at list price even with a
                # CUR present. Index under every form so either side matches.
                entry = by_resource[_short_id(rid)]
                by_resource.setdefault(rid, entry)   # full ARN points at the same record
                entry["unblended"] += unblended
                entry["public"] += public
                if cols.get("ri_arn") and row.get(cols["ri_arn"]):
                    entry["covered"] = True
                if cols.get("sp_arn") and row.get(cols["sp_arn"]):
                    entry["covered"] = True
                if cols.get("usage_type"):
                    entry["usage_types"].add(str(row.get(cols["usage_type"]) or "")[:40])
        except RuntimeError:
            parquet_skipped = True
            continue
        except Exception as e:
            incomplete.append(
                f"{obj['Key'].rsplit('/', 1)[-1]} could not be read: {str(e)[:80]}")
            continue

    if parquet_skipped and not by_resource:
        gaps.add(
            category="Cost data",
            what="CUR is in Parquet format",
            why=("The export is Parquet and the optional `pyarrow` package is "
                 "not installed, so actual costs could not be read."),
            how_to_fix=("pip install pyarrow, or recreate the export in "
                        "gzipped CSV which needs no extra dependency."),
            impact="Savings remain list-price estimates.")
        return {"available": False, "reason": "parquet requires pyarrow"}

    if incomplete:
        # Fail closed. Partial billing data would be applied as actual cost and
        # labelled Confirmed, which is worse than falling back to list price:
        # the number would be wrong AND carry the highest confidence tier.
        gaps.add(
            category="Cost data",
            what="CUR was only partially readable",
            why="; ".join(dict.fromkeys(incomplete))[:400],
            how_to_fix=("Switch the export to DAILY granularity, or raise "
                        "MAX_OBJECTS/MAX_BYTES in collectors/cur_reader.py if "
                        "the account genuinely exports more than that."),
            impact=("Actual costs were discarded rather than applied in part. "
                    "Savings stay list-price Estimated."))
        return {"available": False, "reason": "CUR incomplete: " + incomplete[0]}

    if not by_resource:
        return {"available": False, "reason": "No resource-level rows in CUR "
                                              "(resource IDs may be disabled)"}

    effective = (total_unblended / total_public) if total_public > 0 else None
    costs = {rid: round(v["unblended"], 4) for rid, v in by_resource.items()
             if v["unblended"] > 0}
    covered = {rid for rid, v in by_resource.items() if v["covered"]}
    # Both the bare id and the full ARN resolve to the same figure, so a
    # collector holding either form finds its cost.
    covered |= {_short_id(rid) for rid in covered}

    return {
        "available": True,
        "by_resource": costs,
        "covered_resources": covered,
        "effective_rate": effective,
        "total_unblended": round(total_unblended, 2),
        "total_public_on_demand": round(total_public, 2),
        "objects_read": min(len(objects), max_objects),
        "discount_pct": round((1 - effective) * 100, 1) if effective else None,
    }


def apply_actual_costs(resources, cur_result):
    """
    Overwrite list-price estimates with actual billed cost.

    Unlike the list-price path this DOES overwrite, because an actual charge
    always beats an estimate.
    """
    if not cur_result.get("available"):
        return 0
    from analysis import provenance as prov
    from analysis.provenance import Basis, MEASURED

    costs = cur_result["by_resource"]
    applied = 0
    for _type, items in resources.items():
        for r in items:
            actual = costs.get(r.get("id")) or costs.get(r.get("arn"))
            if actual is None:
                continue
            prov.set_cost(r, actual, Basis(
                MEASURED,
                formula="Actual billed cost for the latest complete billing period",
                provider="cur"))
            r["cost_source"] = "cur"
            applied += 1
    return applied
