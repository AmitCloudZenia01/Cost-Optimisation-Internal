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


def main():
    for fn in (scenario_cur_present, scenario_commitments_held,
               scenario_partial_permissions, scenario_expired_credentials,
               scenario_empty_account, scenario_parquet_cur,
               scenario_cur_configured_but_empty,
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
