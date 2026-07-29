"""
Exercise the code paths a single development account cannot reach.

Everything found so far was found by RUNNING the tool, and it has only ever run
against one account: no CUR, zero commitments, full permissions. That leaves
whole branches that have never executed even once — and those are precisely
where the next client's first report would break.

Each scenario here simulates an account shape ours does not have:

    with a CUR                 -> actual cost replaces list price,
                                  savings become Confirmed
    with commitments           -> confidence downgraded, RI rules suppressed
    with partial permissions   -> gaps recorded, no silent under-reporting
    with expired credentials   -> "not checked", never "nothing found"
    with an empty account      -> no crash, no invented findings

Run:  python3 tests/test_account_shapes.py
"""

import csv
import datetime
import gzip
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from botocore.exceptions import ClientError                      # noqa: E402
from analysis import provenance as prov, recommender            # noqa: E402
from analysis.provenance import CONFIRMED, ESTIMATED, gaps      # noqa: E402
from collectors import (aws_pricing, commitments, cur_discovery,  # noqa: E402
                        cur_reader, purchase_recommendations)

PASSES, FAILURES, SKIPS = [], [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILURES).append(f"{name}{(' — ' + detail) if detail else ''}")


def client_error(code, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


# ── Scenario 1: an account WITH a Cost and Usage Report ─────────────────────

def scenario_cur_present():
    """Never executed: our account has no CUR, so the reader is unproven."""
    gaps.clear()
    rows = [
        # customer pays 78% of list — a 22% effective discount
        {"lineItem/ResourceId": "i-aaa", "lineItem/UnblendedCost": "78.00",
         "pricing/publicOnDemandCost": "100.00",
         "lineItem/UsageType": "BoxUsage:m5.xlarge",
         "reservation/ReservationARN": "", "savingsPlan/SavingsPlanARN": ""},
        {"lineItem/ResourceId": "i-covered", "lineItem/UnblendedCost": "0.00",
         "pricing/publicOnDemandCost": "140.16",
         "lineItem/UsageType": "BoxUsage:m5.xlarge",
         "reservation/ReservationARN": "arn:aws:ec2:::reserved-instances/r1",
         "savingsPlan/SavingsPlanARN": ""},
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    body = gzip.compress(buf.getvalue().encode())

    class S3:
        def list_objects_v2(self, **kw):
            if kw.get("Delimiter"):
                return {"CommonPrefixes": [{"Prefix": "cur/r/20260701-20260801/"}],
                        "IsTruncated": False}
            return {"Contents": [{"Key": "cur/r/20260701-20260801/p.csv.gz",
                                  "Size": len(body),
                                  "LastModified": datetime.datetime(2026, 7, 29)}],
                    "IsTruncated": False}

        def get_object(self, **kw):
            return {"Body": io.BytesIO(body)}

    class Session:
        def client(self, *a, **k):
            return S3()

    result = cur_reader.read(Session(), {"bucket": "b", "prefix": "cur",
                                         "region": "ap-south-1"})
    check("CUR: reader parses gzipped CSV", result.get("available"))
    check("CUR: per-resource actual cost extracted",
          result.get("by_resource", {}).get("i-aaa") == 78.0)
    check("CUR: RI-covered resource detected",
          "i-covered" in result.get("covered_resources", set()))
    check("CUR: effective discount measured, not assumed",
          result.get("discount_pct") is not None)

    resources = {"EC2": [{"type": "EC2", "id": "i-aaa", "region": "ap-south-1",
                          "instance_type": "m5.xlarge", "state": "running",
                          "platform": "Linux", "monthly_cost_usd": 147.46,
                          "cost_source": "list_price", "tags": {"o": "x"},
                          "ami_architecture": "x86_64", "ssm_managed": True,
                          "ssm_app_count": 5, "x86_only_software": [],
                          "arm_verify_software": []}]}
    applied = cur_reader.apply_actual_costs(resources, result)
    r = resources["EC2"][0]
    check("CUR: actual cost overrides list price",
          applied == 1 and r["monthly_cost_usd"] == 78.0)
    check("CUR: cost is flagged as actual", prov.is_actual_cost(r))

    metrics = {"i-aaa": {"periods": {"30d": {"cpu_avg_pct": 4.0, "cpu_p95_pct": 6.0,
                                             "mem_used_pct": 10.0, "mem_p95_pct": 14.0}},
                         "spikes": [], "datapoints": 2000, "spike_window_days": 90,
                         "cwagent_installed": True}}
    _, recs, summary = recommender.generate_all_recommendations(
        resources, metrics, {"recommendations": {}, "metrics": {}})
    priced = [f for f in recs["i-aaa"]["phase1"] + recs["i-aaa"]["phase2"]
              if f["saving_usd"]]
    # Pricing a rightsize target needs a live backend. Without one the rule is
    # correctly gated off as "unable to verify", so asserting on savings here
    # would test the environment rather than the code.
    from collectors import vantage_pricing
    if not priced and not vantage_pricing.configured():
        SKIPS.append("CUR: Confirmed-tier assertions (no pricing backend "
                     "configured — set VANTAGE_API_TOKEN or .vantage_token)")
    else:
        check("CUR: savings become Confirmed, not Estimated",
              bool(priced) and all(f["confidence"] == CONFIRMED for f in priced),
              str([f["confidence"] for f in priced]))
        check("CUR: confirmed total is non-zero",
              summary["confirmed_savings_usd"] > 0)


# ── Scenario 2: an account that ALREADY HOLDS commitments ──────────────────

def scenario_commitments_held():
    """Never executed: our account holds zero RIs and zero Savings Plans."""
    gaps.clear()
    items = [
        {"service": "EC2", "region": "ap-south-1", "id": "ri-1",
         "instance_type": "m5.large", "count": 2, "scope": "Region",
         "availability_zone": "", "platform": "Linux", "offering_class": "standard",
         "offering_type": "No Upfront", "start": "", "end": "", "state": "active"},
        {"service": "SavingsPlan", "region": "ap-south-1", "id": "sp-1",
         "instance_type": "m5", "count": "", "scope": "Compute",
         "availability_zone": "", "platform": "EC2", "offering_class": "No Upfront",
         "offering_type": "", "start": "", "end": "", "state": "active"},
    ]
    coverage = commitments.build_coverage(items)

    c1 = commitments.is_covered(coverage, "EC2", "ap-south-1", "m5.large")
    c2 = commitments.is_covered(coverage, "EC2", "ap-south-1", "m5.large")
    c3 = commitments.is_covered(coverage, "EC2", "ap-south-1", "m5.large")
    check("Commitments: reserved capacity is consumed, not blanket",
          c1[0] and c2[0], "first two covered")
    check("Commitments: a Savings Plan family still flags the third",
          c3[0], "SP covers the m5 family")

    r = {"type": "EC2", "id": "i-1", "region": "ap-south-1",
         "instance_type": "m5.large", "state": "running", "platform": "Linux",
         "monthly_cost_usd": 73.73, "cost_source": "list_price", "tags": {"o": "x"},
         "ami_architecture": "x86_64", "ssm_managed": True, "ssm_app_count": 5,
         "x86_only_software": [], "arm_verify_software": []}
    metrics = {"i-1": {"periods": {"30d": {"cpu_avg_pct": 4.0, "cpu_p95_pct": 6.0}},
                       "spikes": [], "datapoints": 2000, "spike_window_days": 90}}

    _, recs, _ = recommender.generate_all_recommendations(
        {"EC2": [dict(r)]}, metrics, {"recommendations": {}, "metrics": {}},
        coverage=commitments.build_coverage(items), has_commitments=True)
    findings = recs["i-1"]["phase1"] + recs["i-1"]["phase2"]

    priced = [f for f in findings if f["saving_usd"]]
    check("Commitments: no saving is Confirmed on a list-price baseline",
          all(f["confidence"] != CONFIRMED for f in priced),
          str([f["confidence"] for f in priced]))
    ri = [f for f in findings if "Reserved Instance" in f["action"] and f["saving_usd"]]
    check("Commitments: no RI recommended for covered capacity", not ri,
          str([f["action"] for f in ri]))


# ── Scenario 3: partial permissions ────────────────────────────────────────

def scenario_partial_permissions():
    """Never executed: our credentials are administrator."""
    gaps.clear()

    class Denied:
        def client(self, name, region_name=None):
            class C:
                def get_paginator(self, op):
                    raise client_error("AccessDenied", op)

                def __getattr__(self, item):
                    def call(**kw):
                        raise client_error("AccessDenied", item)
                    return call
            return C()

    q = cur_discovery.detect(Denied())
    check("Denied CUR API is reported as UNKNOWN, not 'none configured'",
          q["quality"] == "unknown", q["quality"])
    check("Denied CUR API records a gap saying so",
          any("UNKNOWN" in g.get("impact", "") for g in gaps.all()))

    gaps.clear()
    data = commitments.collect(Denied(), ["ap-south-1"])
    check("Denied commitment APIs record a gap", gaps.count() > 0)
    check("Denied commitment APIs do not claim zero commitments falsely",
          data["has_commitments"] is False and gaps.count() > 0,
          "absence is reported alongside the denial")


# ── Scenario 4: credentials expire mid-run ─────────────────────────────────

def scenario_expired_credentials():
    gaps.clear()

    class Expired:
        def client(self, name, region_name=None):
            class C:
                def __getattr__(self, item):
                    def call(**kw):
                        raise client_error("ExpiredTokenException", item)
                    return call
            return C()

    data = purchase_recommendations.collect(Expired())
    check("Expired credentials flagged, not reported as $0 opportunity",
          data.get("credentials_failed") is True)
    check("Expired credentials produce a NOT ASSESSED gap",
          any("NOT ASSESSED" in g.get("impact", "") for g in gaps.all()))


# ── Scenario 5: an empty account ───────────────────────────────────────────

def scenario_empty_account():
    gaps.clear()
    aws_pricing.configure(session=None)
    resources = {"EC2": [], "RDS": [], "EBS": []}
    try:
        _, recs, summary = recommender.generate_all_recommendations(
            resources, {}, {"recommendations": {}, "metrics": {}})
        check("Empty account does not crash", True)
        check("Empty account invents no findings", not recs, str(recs))
        check("Empty account reports zero savings",
              summary["confirmed_savings_usd"] == 0
              and summary["estimated_savings_usd"] == 0)
    except Exception as e:
        check("Empty account does not crash", False, f"{type(e).__name__}: {e}")


# ── Scenario 6: a Parquet CUR with CUR 2.0 column names ────────────────────

def scenario_parquet_cur():
    """
    Never executed against real data. CUR 2.0 defaults to Parquet with
    snake_case columns, and reports resource IDs in whichever ARN form the
    service uses — colon for RDS/Lambda, slash for EC2/EBS. Stripping only the
    slash form left RDS and Lambda costs unmatched, so they stayed at list
    price while the report claimed a CUR-backed baseline.
    """
    gaps.clear()
    try:
        import pyarrow as pa, pyarrow.parquet as pq
    except ImportError:
        check("Parquet CUR: pyarrow available", False,
              "install pyarrow to read Parquet exports")
        return

    tbl = pa.table({
        "line_item_resource_id": [
            "arn:aws:ec2:ap-south-1:1:instance/i-abc",
            "arn:aws:rds:ap-south-1:1:db:mydb",
            "arn:aws:lambda:ap-south-1:1:function:myfn",
            "my-bucket", ""],
        "line_item_unblended_cost": [78.0, 250.0, 3.0, 5.0, 9.0],
        "pricing_public_on_demand_cost": [100.0, 300.0, 3.0, 5.0, 9.0],
        "line_item_usage_type": ["BoxUsage", "InstanceUsage", "GB-Second", "S3", "Tax"],
        "reservation_reservation_a_r_n": ["", "arn:aws:rds:::ri/x", "", "", ""],
        "savings_plan_savings_plan_a_r_n": ["", "", "", "", ""],
    })
    buf = io.BytesIO(); pq.write_table(tbl, buf); body = buf.getvalue()

    class S3:
        def list_objects_v2(self, **kw):
            if kw.get("Delimiter"):
                return {"CommonPrefixes": [{"Prefix": "c/r/20260701-20260801/"}],
                        "IsTruncated": False}
            return {"Contents": [{"Key": "c/r/20260701-20260801/p.snappy.parquet",
                                  "Size": len(body),
                                  "LastModified": datetime.datetime(2026, 7, 29)}],
                    "IsTruncated": False}

        def get_object(self, **kw):
            return {"Body": io.BytesIO(body)}

    class Session:
        def client(self, *a, **k):
            return S3()

    r = cur_reader.read(Session(), {"bucket": "b", "prefix": "c",
                                    "region": "ap-south-1"})
    check("Parquet CUR: file is read", r.get("available"))
    check("Parquet CUR: CUR 2.0 snake_case columns recognised",
          bool(r.get("by_resource")))

    resources = {
        "EC2": [{"type": "EC2", "id": "i-abc", "monthly_cost_usd": 147.46,
                 "cost_source": "list_price"}],
        "RDS": [{"type": "RDS", "id": "mydb", "monthly_cost_usd": 369.38,
                 "cost_source": "list_price"}],
        "Lambda": [{"type": "Lambda", "id": "myfn"}],
        "S3": [{"type": "S3", "id": "my-bucket", "monthly_cost_usd": 6.0,
                "cost_source": "list_price"}],
    }
    applied = cur_reader.apply_actual_costs(resources, r)
    check("Parquet CUR: every ARN form matches its resource", applied == 4,
          f"applied to {applied}/4")
    check("Parquet CUR: colon-form RDS ARN resolves",
          prov.cost_of(resources["RDS"][0]) == 250.0,
          str(prov.cost_of(resources["RDS"][0])))
    check("Parquet CUR: colon-form Lambda ARN resolves",
          prov.cost_of(resources["Lambda"][0]) == 3.0)
    check("Parquet CUR: blank resource id ignored",
          "" not in r.get("by_resource", {}))


def scenario_truncated_cur():
    """
    A CUR too large for the read caps must yield NO cost data, not partial.
    Partial rows are applied as MEASURED and shown as Confirmed, so a resource
    whose rows sat in an unread file would silently understate while wearing
    the highest confidence tier.
    """
    gaps.clear()
    rows = [{"lineItem/ResourceId": "i-aaa", "lineItem/UnblendedCost": "78.00",
             "pricing/publicOnDemandCost": "100.00",
             "lineItem/UsageType": "BoxUsage", "reservation/ReservationARN": "",
             "savingsPlan/SavingsPlanARN": ""}]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    body = gzip.compress(buf.getvalue().encode())

    class S3:
        def list_objects_v2(self, **kw):
            if kw.get("Delimiter"):
                return {"CommonPrefixes": [{"Prefix": "c/r/20260701-20260801/"}],
                        "IsTruncated": False}
            return {"Contents": [{"Key": f"c/r/20260701-20260801/p{i}.csv.gz",
                                  "Size": len(body),
                                  "LastModified": datetime.datetime(2026, 7, 29)}
                                 for i in range(5)], "IsTruncated": False}

        def get_object(self, **kw):
            return {"Body": io.BytesIO(body)}

    class Session:
        def client(self, *a, **k):
            return S3()

    # cap below the file count -> the read is knowingly partial
    r = cur_reader.read(Session(), {"bucket": "b", "prefix": "c",
                                    "region": "ap-south-1"}, max_objects=2)
    check("Truncated CUR: partial data is NOT reported as available",
          not r.get("available"), str(r.get("reason"))[:60])
    check("Truncated CUR: no costs leak out",
          not r.get("by_resource"))
    check("Truncated CUR: a gap is recorded for the user",
          any("partially readable" in (g.get("what") or "") for g in gaps.all()))

    resources = {"EC2": [{"type": "EC2", "id": "i-aaa", "monthly_cost_usd": 147.46,
                          "cost_source": "list_price"}]}
    check("Truncated CUR: list price is left untouched",
          cur_reader.apply_actual_costs(resources, r) == 0
          and resources["EC2"][0]["cost_source"] == "list_price")

    # an unreadable file mid-batch is equally disqualifying
    gaps.clear()

    class BadS3(S3):
        def get_object(self, **kw):
            if kw["Key"].endswith("p1.csv.gz"):
                raise OSError("corrupt gzip stream")
            return {"Body": io.BytesIO(body)}

    class BadSession:
        def client(self, *a, **k):
            return BadS3()

    r2 = cur_reader.read(BadSession(), {"bucket": "b", "prefix": "c",
                                        "region": "ap-south-1"})
    check("Corrupt CUR file: read fails closed rather than under-reporting",
          not r2.get("available"), str(r2.get("reason"))[:60])


def scenario_cur_configured_but_empty():
    """
    A CUR that exists but has not delivered must NOT be announced as the cost
    basis. detect() sets quality="cur" from the report definition alone, so a
    report created minutes earlier made the summary print "Cost basis: actual
    billed cost (CUR)" over figures that were still list price — precisely the
    mislabelling this project exists to prevent.
    """
    for reason, label in (("No CUR data files found yet (first delivery can "
                           "take 24h)", "not delivered yet"),
                          ("CUR incomplete: 900 data files exceed the 200-file "
                           "cap", "partially readable")):
        gaps.clear()
        q = {"available": True, "quality": "cur", "best": {"bucket": "b"}}
        cur_discovery.finalise_quality(q, {"available": False, "reason": reason})
        check(f"CUR {label}: cost basis downgraded off 'cur'",
              q["quality"] != "cur", q["quality"])
        check(f"CUR {label}: the reason is carried for the summary line",
              q.get("cur_read_failed") == reason)
        check(f"CUR {label}: a gap tells the reader what happened",
              any("not readable this run" in (g.get("what") or "")
                  for g in gaps.all()))

    # AWS's own ReportStatus distinguishes "never delivered" from "delivered
    # but unreadable" — an empty bucket and a permissions failure look
    # identical from the outside, and they need opposite advice.
    gaps.clear()
    q = {"available": True, "quality": "cur",
         "best": {"bucket": "b", "last_delivery": "", "last_status": ""}}
    cur_discovery.finalise_quality(q, {"available": False, "reason": "no files"})
    why = next(g["why"] for g in gaps.all() if "not readable" in g["what"])
    check("Never-delivered CUR: reason cites AWS's own status, not a guess",
          "never delivered" in why.lower(), why[:70])

    gaps.clear()
    q = {"available": True, "quality": "cur",
         "best": {"bucket": "b", "last_delivery": "2026-07-30T04:00:00Z",
                  "last_status": "ERROR_PERMISSIONS"}}
    cur_discovery.finalise_quality(q, {"available": False, "reason": "no files"})
    fix = next(g["how_to_fix"] for g in gaps.all() if "not readable" in g["what"])
    check("Failed delivery: advice points at the bucket policy, not at waiting",
          "bucket policy" in fix, fix[:70])

    # A CUR that DID read must keep its Confirmed basis.
    gaps.clear()
    q = {"available": True, "quality": "cur", "best": {"bucket": "b"}}
    cur_discovery.finalise_quality(q, {"available": True, "by_resource": {"i": 1.0}})
    check("CUR read successfully: basis stays 'cur'", q["quality"] == "cur")
    check("CUR read successfully: no spurious gap", gaps.count() == 0)


def scenario_credit_covered_account():
    """
    An account paid entirely by promotional credits.

    Cost Explorer returns Usage, Credit, Refund and Tax rows together. Summing
    them nets credits against usage, so a fully-credited account showed every
    service at roughly zero — and the `cost > 0` guard then discarded whichever
    services went negative, leaving an arbitrary positive residue as "spend".
    On a real account this reported $868.58 of spend against a $1,394.85 bill,
    and data transfer as $0.00 against $868.57 of egress.
    """
    gaps.clear()
    from collectors import cost_explorer

    captured = {}

    class CE:
        def get_cost_and_usage(self, **kw):
            captured.update(kw)
            groups = [("Amazon Elastic Compute Cloud - Compute", 1316.59),
                      ("EC2 - Other", 45.64),
                      ("Amazon Simple Storage Service", 18.76)]
            # Without the RECORD_TYPE filter the API would also return the
            # credit rows that cancel these out.
            if not _usage_only(kw.get("Filter")):
                groups.append(("AWS Data Transfer", -868.58))
            return {"ResultsByTime": [{
                "TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
                "Groups": [{"Keys": [k],
                            "Metrics": {"UnblendedCost": {"Amount": str(v)},
                                        "UsageQuantity": {"Amount": "1"}}}
                           for k, v in groups]}]}

    class Session:
        def client(self, *a, **k):
            return CE()

    rows = cost_explorer.get_monthly_costs(Session(), months=1)
    total = round(sum(r["cost"] for r in rows), 2)
    check("Credit-covered: RECORD_TYPE=Usage filter is sent",
          _usage_only(captured.get("Filter")), str(captured.get("Filter"))[:70])
    check("Credit-covered: total is real usage, not the netted residue",
          total == 1380.99, f"${total}")
    check("Credit-covered: no negative row survives to be silently dropped",
          all(r["cost"] > 0 for r in rows))


def _usage_only(flt):
    """True if the filter restricts RECORD_TYPE to Usage, nested or not."""
    if not flt:
        return False
    if flt.get("Dimensions", {}).get("Key") == "RECORD_TYPE":
        return flt["Dimensions"].get("Values") == ["Usage"]
    return any(_usage_only(f) for f in flt.get("And", []))


def scenario_part_time_instances():
    """
    Instances priced at 730 hours that did not run 730 hours.

    An inventory scan sees an instance; the pricer multiplies its hourly rate
    by a full month. One real instance ran 141.6 of 720 hours and was reported
    at five times its cost — and its Graviton saving inherited the same
    multiple. Billed hours come from Cost Explorer and are measured.
    """
    gaps.clear()
    from collectors import instance_hours

    hours = {"available": True, "month": "2026-06-29 to 2026-07-29",
             "window_hours": 720,
             "by_type": {"t3.xlarge":  {"hours": 141.6, "cost": 25.38},
                         "t2.medium":  {"hours": 715.8, "cost": 35.51},
                         "m5.16xlarge": {"hours": 33.7, "cost": 108.94},
                         "c5.large":   {"hours": 500.0, "cost": 40.00}}}

    resources = {"EC2": [
        {"type": "EC2", "id": "part-time", "instance_type": "t3.xlarge",
         "state": "running", "monthly_cost_usd": 130.82, "cost_source": "list_price"},
        {"type": "EC2", "id": "full-time", "instance_type": "t2.medium",
         "state": "running", "monthly_cost_usd": 36.21, "cost_source": "list_price"},
        # two of a kind sharing 500h -> not attributable to either
        {"type": "EC2", "id": "pair-a", "instance_type": "c5.large",
         "state": "running", "monthly_cost_usd": 62.05, "cost_source": "list_price"},
        {"type": "EC2", "id": "pair-b", "instance_type": "c5.large",
         "state": "running", "monthly_cost_usd": 62.05, "cost_source": "list_price"},
    ]}
    instance_hours.apply_uptime(resources, hours)
    by_id = {r["id"]: r for r in resources["EC2"]}

    check("Part-time: actual billed cost is preferred over rate x uptime",
          by_id["part-time"]["monthly_cost_usd"] == 25.38,
          str(by_id["part-time"]["monthly_cost_usd"]))
    check("Part-time: uptime recorded as evidence",
          by_id["part-time"]["uptime_pct"] == 19.7)
    check("Part-time: full-time instance left untouched",
          by_id["full-time"]["monthly_cost_usd"] == 36.21
          and by_id["full-time"]["cost_source"] == "list_price")
    check("Part-time: hours are MEASURED, so the cost counts as actual",
          prov.is_actual_cost(by_id["part-time"]))
    check("Part-time: ambiguous split is a gap, not an even division",
          by_id["pair-a"]["monthly_cost_usd"] == 62.05
          and any("not attributable" in (g.get("what") or "") for g in gaps.all()))

    # A brand-new instance must not inherit a predecessor's uptime.
    gaps.clear()
    fresh = {"EC2": [{"type": "EC2", "id": "i-new", "name": "just-launched",
                      "instance_type": "t3.xlarge", "state": "running",
                      "age_days": 0, "monthly_cost_usd": 130.82,
                      "cost_source": "list_price"}]}
    instance_hours.apply_uptime(fresh, hours)
    check("New instance: does not inherit a terminated predecessor's uptime",
          fresh["EC2"][0]["monthly_cost_usd"] == 130.82
          and fresh["EC2"][0]["cost_source"] == "list_price",
          str(fresh["EC2"][0]["monthly_cost_usd"]))
    check("New instance: recorded as a gap rather than silently full-priced",
          any("not measurable" in (g.get("what") or "") for g in gaps.all()))

    # Instances billed but gone before the scan must not vanish silently.
    gaps.clear()
    missing = instance_hours.detect_untracked(resources, hours)
    check("Terminated instances: billed-but-absent types are surfaced",
          [m["instance_type"] for m in missing] == ["m5.16xlarge"],
          str(missing))
    check("Terminated instances: recorded as a coverage gap",
          any("not in inventory" in (g.get("what") or "") for g in gaps.all()))


def scenario_commitment_risk():
    """
    A Savings Plan is sound arithmetic and can still be a bad decision.

    AWS computes the saving from N days of history and says nothing about
    whether N days predicts N years, nor that the same report may recommend
    removing the capacity being committed to.
    """
    from collectors import purchase_recommendations as pr

    purchase = {"best_savings_plan": {
        "term": "THREE_YEARS", "hourly_commitment": 0.204,
        "lookback_days": "THIRTY_DAYS", "monthly_savings": 137.18,
        "savings_pct": 25.0, "current_on_demand": 548.0, "roi_pct": 30.0,
        "source": "ce:GetSavingsPlansPurchaseRecommendation"}}
    falling = [{"month": "2026-04", "service": "EC2 - Compute", "cost": 900.0},
               {"month": "2026-06", "service": "EC2 - Compute", "cost": 600.0}]
    shrinking = [{"action": "Rightsize t2.medium -> t3.small", "saving_usd": 18.11},
                 {"action": "Terminate idle instance", "saving_usd": 40.0}]

    risk = pr.assess_commitment_risk(purchase, falling, shrinking)
    check("Commitment: total exposure is stated, not just the monthly saving",
          risk["exposure_usd"] == round(0.204 * 8760 * 3, 2),
          f"${risk['exposure_usd']}")
    check("Commitment: a 30-day lookback for a 3-year term is flagged",
          any("thirty days" in w.lower() for w in risk["warnings"]))
    check("Commitment: falling compute spend is flagged",
          any("falling" in w.lower() for w in risk["warnings"]),
          str(risk["trend"]))
    check("Commitment: conflicting rightsizing advice is flagged",
          risk["conflict_count"] == 2 and risk["conflict_savings"] == 58.11)
    check("Commitment: a shorter term is preferred when evidence is thin",
          risk["prefer_shorter_term"])

    # Strong evidence, no conflicts -> no manufactured warnings.
    solid = dict(purchase["best_savings_plan"],
                 term="ONE_YEAR", lookback_days="SIXTY_DAYS")
    growing = [{"month": "2026-04", "service": "EC2 - Compute", "cost": 600.0},
               {"month": "2026-06", "service": "EC2 - Compute", "cost": 900.0}]
    calm = pr.assess_commitment_risk({"best_savings_plan": solid}, growing, [])
    check("Commitment: a well-evidenced plan carries no invented warnings",
          not calm["warnings"] and not calm["prefer_shorter_term"],
          str(calm["warnings"]))


def scenario_rightsize_target():
    """
    Downsizing must not strand the customer on an obsolete family, and must say
    what happens to memory when memory was never measured.
    """
    from analysis import recommender as rc

    check("Rightsize: obsolete family is modernised (t2 -> t3)",
          rc.MODERN_FAMILY.get("t2") == "t3")
    check("Rightsize: current-generation family is left alone",
          "m5" not in rc.MODERN_FAMILY and "t3" not in rc.MODERN_FAMILY)


def scenario_public_ipv4_billing():
    """
    Every public IPv4 address is billed since 1 Feb 2024, attached or not.

    The pricer zeroed any attached address, which hid the whole charge: three
    Elastic IPs read $0.00 against a real $13.86, no "release it" finding was
    produced for the address on a stopped instance, and "terminate this
    instance" was worth $2.01 when it was worth $5.61.
    """
    gaps.clear()
    from analysis import recommender as rc

    resources = {
        "EC2": [{"type": "EC2", "id": "i-stopped", "state": "stopped"},
                {"type": "EC2", "id": "i-running", "state": "running"}],
        "ElasticIPs": [
            {"type": "ElasticIPs", "id": "eip-on-stopped", "region": "ap-south-1",
             "attached_to": "i-stopped", "unattached": False},
            {"type": "ElasticIPs", "id": "eip-live", "region": "ap-south-1",
             "attached_to": "i-running", "unattached": False}],
    }
    from collectors import service_costs
    service_costs._link_eips_to_instance_state(resources)
    check("Public IPv4: address on a STOPPED instance counts as idle",
          resources["ElasticIPs"][0].get("attached_to_stopped") is True)
    check("Public IPv4: address on a RUNNING instance is not idle",
          not resources["ElasticIPs"][1].get("attached_to_stopped"))

    # The rule must fire for the stopped-instance address, not just unattached.
    ctx = {"resource": resources["ElasticIPs"][0], "cost": 3.65,
           "cost_is_actual": False, "metrics": {}}
    f = rc.eip_unattached(ctx, gated=False)
    check("Public IPv4: a release finding is produced for it",
          f is not None and f["saving_usd"] == 3.65,
          str(f and f["action"]))
    check("Public IPv4: running-instance address produces no finding",
          rc.eip_unattached({"resource": resources["ElasticIPs"][1], "cost": 3.65,
                             "cost_is_actual": False, "metrics": {}},
                            gated=False) is None)

    # Termination must count the address as well as the volumes.
    ctx2 = {"resource": {"type": "EC2", "id": "i-stopped", "state": "stopped"},
            "cost": 0.0, "cost_is_actual": False, "metrics": {},
            "volumes_by_instance": {"i-stopped": [{"monthly_cost_usd": 2.01}]},
            "eips_by_instance": {"i-stopped": [{"monthly_cost_usd": 3.60}]}}
    t = rc.ec2_stopped(ctx2, gated=False)
    check("Termination saving includes the address it releases",
          t["saving_usd"] == 5.61, str(t["saving_usd"]))


def scenario_unverified_uptime_savings():
    """
    A precise saving must not rest on unmeasured uptime.

    Disclosing "this may be overstated" in a footnote while printing a
    confident number is the failure this project exists to prevent.
    """
    gaps.clear()
    from analysis import rules as R

    produced = [{"action": "Move to Graviton", "saving_usd": 65.41,
                 "confidence": ESTIMATED, "caveats": []}]
    ctx = {"resource": {"type": "EC2", "id": "i-new", "name": "fresh",
                        "instance_type": "t3.xlarge",
                        "uptime_unverified": True, "type_uptime_pct": 19.7}}
    R._gate_unverified_uptime(ctx, produced)
    f = produced[0]
    check("Unverified uptime: precise saving is withdrawn",
          f["saving_usd"] is None and f["confidence"] == "Unpriced",
          str(f["saving_usd"]))
    check("Unverified uptime: the range is disclosed instead",
          any("$12.89" in c and "$65.41" in c for c in f["caveats"]),
          str(f["caveats"])[:110])

    # A measured instance keeps its number.
    kept = [{"action": "Move to Graviton", "saving_usd": 65.41,
             "confidence": ESTIMATED, "caveats": []}]
    R._gate_unverified_uptime({"resource": {"type": "EC2", "id": "i-old"}}, kept)
    check("Measured uptime: saving is left intact", kept[0]["saving_usd"] == 65.41)


def scenario_savings_plan_scope_label():
    """
    CurrentOnDemandSpend covers the whole lookback, so a 60-day lookback makes
    it a two-month figure. Printed beside a monthly saving it does not
    reconcile: $843.73 x 31.8% = $268, not the $136 claimed.
    """
    from analysis import recommender as rc
    plan = {"current_on_demand": 843.73, "lookback_days": "SIXTY_DAYS"}
    check("SP scope: 60-day spend is normalised to a month",
          abs(rc._monthly_scope(plan) - 421.865) < 0.01,
          str(rc._monthly_scope(plan)))
    check("SP scope: monthly lookback is left unchanged",
          rc._monthly_scope({"current_on_demand": 421.87,
                             "lookback_days": "THIRTY_DAYS"}) == 421.87)


def scenario_reconciliation():
    """
    The report must be provable against the bill.

    Every other check here is internal and passes happily on a uniformly wrong
    report — which is how 29 Elastic IPs sat at $0.00 against ~$105/mo of real
    charges through five separate readings of that report. This is the only
    check that compares output to what AWS charged.
    """
    gaps.clear()
    from analysis import reconciliation as rec

    monthly = [{"month": "2026-06", "service": "EC2 - Compute", "cost": 1316.59},
               {"month": "2026-06", "service": "Amazon Virtual Private Cloud",
                "cost": 13.86},
               {"month": "2026-05", "service": "EC2 - Compute", "cost": 900.0}]

    # Elastic IPs priced at $0.00 — the bug as it actually shipped.
    broken = {"EC2": [{"type": "EC2", "id": "i-1", "monthly_cost_usd": 1316.59}],
              "ElasticIPs": [{"type": "ElasticIPs", "id": "e-1",
                              "monthly_cost_usd": 0.0}]}
    r = rec.reconcile(monthly, broken)
    check("Reconciliation: uses the latest complete month only",
          r["month"] == "2026-06" and r["billed_usd"] == 1330.45,
          f"{r['month']} ${r['billed_usd']}")
    check("Reconciliation: unattributed spend is surfaced",
          r["unexplained_usd"] == 13.86, str(r["unexplained_usd"]))
    check("Reconciliation: a material gap raises a data gap",
          any("not attributed to any resource" in (g.get("what") or "")
              for g in gaps.all()))

    # Priced correctly -> reconciles, no gap.
    gaps.clear()
    fixed = {"EC2": [{"type": "EC2", "id": "i-1", "monthly_cost_usd": 1316.59}],
             "ElasticIPs": [{"type": "ElasticIPs", "id": "e-1",
                             "monthly_cost_usd": 13.86}]}
    ok = rec.reconcile(monthly, fixed)
    check("Reconciliation: a correct report reconciles to 100%",
          ok["coverage_pct"] == 100.0 and not ok["material"],
          f"{ok['coverage_pct']}%")
    check("Reconciliation: no gap raised when the numbers agree",
          not any("not attributed" in (g.get("what") or "") for g in gaps.all()))

    # Over-attribution is worse than under — savings built on it are inflated.
    gaps.clear()
    over = {"EC2": [{"type": "EC2", "id": "i-1", "monthly_cost_usd": 2000.0}]}
    r3 = rec.reconcile(monthly, over)
    check("Reconciliation: attributing MORE than the bill is flagged",
          r3["unexplained_usd"] < 0
          and any("exceeds the bill" in (g.get("what") or "") for g in gaps.all()),
          str(r3["unexplained_usd"]))

    check("Reconciliation: no monthly data returns unavailable, not zero",
          rec.reconcile([], {}).get("available") is False)

    # The figures shown must add up on the page. An earlier version printed
    # attributed cost beside a coverage percentage that silently included the
    # explained-elsewhere portion, so a reader checking the arithmetic found it
    # broken and the tab undermined the report it was meant to vouch for.
    gaps.clear()
    shown = rec.reconcile(monthly, fixed,
                          explained={"Data transfer (own tab)": 100.0})
    total = (shown["attributed_usd"] + shown["explained_elsewhere_usd"]
             + shown["unexplained_usd"])
    check("Reconciliation: attributed + explained + unexplained = billed",
          abs(total - shown["billed_usd"]) < 0.02,
          f"{total:,.2f} vs {shown['billed_usd']:,.2f}")
    check("Reconciliation: coverage matches the figures shown",
          abs(shown["coverage_pct"]
              - (shown["accounted_usd"] / shown["billed_usd"] * 100)) < 0.05)


def scenario_public_ipv4_beyond_eips():
    """
    Every public IPv4 address is billed, not only Elastic IPs.

    Auto-assigned instance addresses, NAT gateway addresses and one address per
    AZ on an internet-facing load balancer are all charged since 1 Feb 2024.
    Pricing only Elastic IPs left ten such addresses unattributed on a real
    account, invisible because each resource's own price looked correct.
    """
    gaps.clear()
    from collectors import service_costs, aws_pricing as ap
    if not ap.price("AmazonVPC", "us-east-1",
                    usagetype_suffix="PublicIPv4:InUseAddress",
                    label="probe"):
        SKIPS.append("Public IPv4 pricing (no pricing backend reachable)")
        return

    resources = {
        "ElasticIPs": [{"type": "ElasticIPs", "id": "e-1", "public_ip": "1.1.1.1",
                        "monthly_cost_usd": 3.65}],
        "EC2": [
            # holds the Elastic IP above -> must NOT be charged twice
            {"type": "EC2", "id": "i-eip", "state": "running",
             "public_ip": "1.1.1.1", "monthly_cost_usd": 100.0},
            # auto-assigned -> must be charged
            {"type": "EC2", "id": "i-auto", "state": "running",
             "public_ip": "2.2.2.2", "monthly_cost_usd": 100.0},
            # stopped instances hold no address
            {"type": "EC2", "id": "i-stop", "state": "stopped",
             "public_ip": "3.3.3.3", "monthly_cost_usd": 0.0},
        ],
        "NATGateway": [{"type": "NATGateway", "id": "nat-1", "state": "available",
                        "connectivity_type": "public", "public_ips": "4.4.4.4",
                        "monthly_cost_usd": 32.85}],
        "ELB": [
            {"type": "ELB", "id": "lb-1", "name": "web", "scheme": "internet-facing",
             "az_count": 3, "monthly_cost_usd": 16.43},
            {"type": "ELB", "id": "lb-2", "name": "internal", "scheme": "internal",
             "az_count": 2, "monthly_cost_usd": 16.43},
            {"type": "ELB", "id": "lb-3", "name": "unknown-az",
             "scheme": "internet-facing", "monthly_cost_usd": 16.43},
        ],
    }
    n = service_costs._price_public_ipv4(resources, "us-east-1")
    by = {r["id"]: r for v in resources.values() for r in v}

    check("Public IPv4: instance holding an Elastic IP is not charged twice",
          "public_ipv4_count" not in by["i-eip"])
    check("Public IPv4: auto-assigned address is charged",
          by["i-auto"].get("public_ipv4_count") == 1)
    check("Public IPv4: stopped instance is not charged",
          "public_ipv4_count" not in by["i-stop"])
    check("Public IPv4: NAT gateway address is charged",
          by["nat-1"].get("public_ipv4_count") == 1)
    check("Public IPv4: internet-facing LB charged once per AZ",
          by["lb-1"].get("public_ipv4_count") == 3)
    check("Public IPv4: internal LB is not charged",
          "public_ipv4_count" not in by["lb-2"])
    check("Public IPv4: unknown AZ count is a gap, never a guess",
          "public_ipv4_count" not in by["lb-3"]
          and any("Public IPv4 for unknown-az" in (g.get("what") or "")
                  for g in gaps.all()))
    check("Public IPv4: charged resources counted", n == 3, str(n))

    # The bill is a hard ceiling — counting addresses assumes each existed all
    # month, which over-attributes on any account with churn.
    gaps.clear()
    capped = {
        "ElasticIPs": [{"type": "ElasticIPs", "id": "e-1", "public_ip": "1.1.1.1",
                        "monthly_cost_usd": 3.65}],
        "EC2": [{"type": "EC2", "id": f"i-{i}", "state": "running",
                 "public_ip": f"10.0.0.{i}", "monthly_cost_usd": 10.0}
                for i in range(10)],
    }
    service_costs._price_public_ipv4(capped, "us-east-1", actual_total=10.00)
    charged = sum(r.get("public_ipv4_cost_usd", 0) for r in capped["EC2"])
    check("Public IPv4: attribution never exceeds AWS's own total",
          charged <= 10.00 - 3.65 + 0.01, f"${charged:,.2f} vs budget $6.35")
    check("Public IPv4: allocation is disclosed, not silent",
          any("allocated rather than counted" in (g.get("what") or "")
              for g in gaps.all()))


def scenario_generic_coverage():
    """
    The tool discovers generically but analyses specifically.

    The Resource Groups Tagging API returns every type AWS knows about,
    including ones invented after this code was written — 65 types on a real
    account against 17 with a rule. Everything else was silently dropped, so
    "we have no logic for this" was indistinguishable from "this is free".
    """
    from analysis import rules as R

    gaps.clear()
    ctx = {"resource": {"type": "SomeServiceInventedLater", "id": "x-1",
                        "region": "ap-south-1", "monthly_cost_usd": 40.0},
           "cost": 40.0, "cost_is_actual": False, "metrics": {},
           "min_datapoints": 0}
    out = R.run_rules(ctx)
    check("Generic coverage: unreviewed spend on an unknown type is named",
          any("No optimisation rule covers" in f["action"] for f in out),
          str([f["action"] for f in out]))
    check("Generic coverage: no saving is invented for it",
          all(f["saving_usd"] is None for f in out))
    check("Generic coverage: recorded as a coverage gap",
          any(g.get("category") == "Coverage" for g in gaps.all()))

    # Free resources must stay silent — 784 of them on a real account.
    gaps.clear()
    quiet = R.run_rules({"resource": {"type": "Subnet", "id": "s-1"},
                         "cost": None, "cost_is_actual": False,
                         "metrics": {}, "min_datapoints": 0})
    check("Generic coverage: unpriced resources produce no noise",
          not quiet and gaps.count() == 0, f"{len(quiet)} findings")

    # Trivial spend is not worth a row.
    gaps.clear()
    R.run_rules({"resource": {"type": "Unknown", "id": "u-1",
                              "monthly_cost_usd": 0.10},
                 "cost": 0.10, "cost_is_actual": False, "metrics": {},
                 "min_datapoints": 0})
    check("Generic coverage: sub-dollar spend is not flagged",
          gaps.count() == 0)

    # A type WITH rules must not also get the generic row.
    gaps.clear()
    covered = R.run_rules({"resource": {"type": "EC2", "id": "i-1",
                                        "state": "stopped",
                                        "monthly_cost_usd": 5.0},
                           "cost": 5.0, "cost_is_actual": False, "metrics": {},
                           "min_datapoints": 0, "volumes_by_instance": {},
                           "eips_by_instance": {}})
    check("Generic coverage: covered types do not get a duplicate row",
          not any("No optimisation rule covers" in f["action"] for f in covered))


def main():
    for fn in (scenario_cur_present, scenario_commitments_held,
               scenario_partial_permissions, scenario_expired_credentials,
               scenario_empty_account, scenario_parquet_cur,
               scenario_cur_configured_but_empty,
               scenario_credit_covered_account, scenario_part_time_instances,
               scenario_commitment_risk, scenario_rightsize_target,
               scenario_public_ipv4_billing, scenario_unverified_uptime_savings,
               scenario_savings_plan_scope_label, scenario_reconciliation,
               scenario_public_ipv4_beyond_eips, scenario_generic_coverage,
               scenario_truncated_cur):
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{fn.__name__} crashed — {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    for line in PASSES:
        print(f"  PASS  {line}")
    for line in FAILURES:
        print(f"  FAIL  {line}")
    print("=" * 72)
    for s in SKIPS:
        print(f"  SKIP  {s}")
    tail = f", {len(SKIPS)} skipped" if SKIPS else ""
    print(f"  {len(PASSES)} passed, {len(FAILURES)} failed{tail}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
