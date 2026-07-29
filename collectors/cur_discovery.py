"""
Detect whether this account already publishes a Cost and Usage Report.

Why it matters: without CUR the only cost signal is Cost Explorer, which is
aggregated and — for most resources — reports *list* price rather than what the
customer actually pays after Reserved Instances, Savings Plans, credits and any
negotiated discount. Savings quoted against list price can be materially wrong.

Many accounts already have a CUR configured. Detecting one costs two API calls
and turns a guess into a fact, so the report can state its own accuracy instead
of implying more confidence than the data supports.

Read-only: describe/list calls only. Nothing is created.
"""

from analysis.provenance import gaps
from collectors import api_errors

# Legacy Cost & Usage Reports, and the newer BCM Data Exports (CUR 2.0).
_LEGACY_REGION = "us-east-1"


def _legacy_reports(session, status=None):
    found = []
    status = status if status is not None else {}
    try:
        cur = session.client("cur", region_name=_LEGACY_REGION)
        paginator = cur.get_paginator("describe_report_definitions")
        for page in paginator.paginate():
            for report in page.get("ReportDefinitions", []):
                found.append({
                    "name": report.get("ReportName", ""),
                    "format": report.get("Format", ""),
                    "compression": report.get("Compression", ""),
                    "bucket": report.get("S3Bucket", ""),
                    "prefix": report.get("S3Prefix", ""),
                    "region": report.get("S3Region", ""),
                    "time_unit": report.get("TimeUnit", ""),
                    "resource_ids": "RESOURCES" in (report.get("AdditionalSchemaElements") or []),
                    "api": "cur:DescribeReportDefinitions",
                    "version": "CUR 1.0",
                    # AWS reports its own delivery state. Empty lastDelivery
                    # means it has genuinely never delivered — better evidence
                    # than inferring it from an empty bucket, which looks the
                    # same as a permissions problem.
                    "last_delivery": (report.get("ReportStatus") or {}).get("lastDelivery", ""),
                    "last_status": (report.get("ReportStatus") or {}).get("lastStatus", ""),
                })
    except Exception as e:
        # A denial here would otherwise be reported as "no CUR configured",
        # which sends the whole report down the list-price path on a false
        # premise. Absence and inaccessibility are not the same thing.
        if api_errors.classify(e) in ("denied", "throttled", "error"):
            status["blocked"] = True
        api_errors.note(e, context="cur:DescribeReportDefinitions")
    return found


def _data_exports(session, status=None):
    found = []
    status = status if status is not None else {}
    try:
        client = session.client("bcm-data-exports", region_name=_LEGACY_REGION)
        paginator = client.get_paginator("list_exports")
        for page in paginator.paginate():
            for export in page.get("Exports", []):
                name = export.get("ExportName") or export.get("Name") or ""
                detail = {}
                try:
                    detail = client.get_export(
                        ExportArn=export.get("ExportArn", "")).get("Export", {})
                except Exception as e:
                    api_errors.note(e, context="bcm-data-exports:GetExport")
                destination = ((detail.get("DestinationConfigurations") or {})
                               .get("S3Destination") or {})
                query = (detail.get("DataQuery") or {}).get("QueryStatement", "")
                found.append({
                    "name": name,
                    "format": (destination.get("S3OutputConfigurations") or {}).get("Format", ""),
                    "compression": (destination.get("S3OutputConfigurations") or {}).get("Compression", ""),
                    "bucket": destination.get("S3Bucket", ""),
                    "prefix": destination.get("S3Prefix", ""),
                    "region": destination.get("S3Region", ""),
                    "time_unit": "HOURLY",
                    "resource_ids": "resource_id" in query.lower(),
                    "api": "bcm-data-exports:ListExports",
                    "version": "CUR 2.0",
                })
    except Exception as e:
        if api_errors.classify(e) in ("denied", "throttled", "error"):
            status["blocked"] = True
        api_errors.note(e, context="bcm-data-exports:ListExports")
    return found


def detect(session):
    """
    Returns a dict describing the account's cost-data quality:

        {available, reports[], best, resource_level, quality, message}

    `quality` is one of: "cur" (actuals available), "cost-explorer" (list price),
    and drives the accuracy banner on the Summary tab.
    """
    status = {}
    reports = _legacy_reports(session, status) + _data_exports(session, status)

    if not reports and status.get("blocked"):
        # We could not determine whether a CUR exists. Say that, rather than
        # asserting there is none.
        gaps.add(
            category="Cost data",
            what="CUR detection blocked",
            why="The CUR and Data Exports APIs could not be queried, so it is "
                "unknown whether this account publishes a Cost and Usage Report.",
            how_to_fix="Grant cur:DescribeReportDefinitions and "
                       "bcm-data-exports:ListExports, then re-run.",
            impact="Cost basis is UNKNOWN, not confirmed as list price.")
        return {
            "available": False, "reports": [], "best": None,
            "resource_level": False, "quality": "unknown",
            "message": ("Could not determine whether a Cost and Usage Report "
                        "exists (API access denied). Costs fall back to list "
                        "price, but this is 'not checked', not 'none configured'."),
        }

    if not reports:
        gaps.add(
            category="Cost data",
            what="Cost and Usage Report",
            why=("No CUR or Data Export is configured in this account, so costs "
                 "fall back to Cost Explorer list prices. Any Reserved Instance, "
                 "Savings Plan, credit or negotiated discount already in effect "
                 "is not reflected, and savings may be overstated."),
            how_to_fix=("Billing console -> Data Exports -> Create, including "
                        "resource IDs. First data lands within 24 hours."),
            impact="Savings are labelled Estimated rather than Confirmed.")
        return {
            "available": False,
            "reports": [],
            "best": None,
            "resource_level": False,
            "quality": "cost-explorer",
            "message": ("No Cost and Usage Report found. Costs are list-price "
                        "estimates from Cost Explorer and exclude any existing "
                        "commitments or discounts."),
        }

    # Prefer an export that includes resource IDs — without them a CUR cannot
    # attribute cost to individual resources.
    best = next((r for r in reports if r["resource_ids"]), reports[0])
    if not best["resource_ids"]:
        gaps.add(
            category="Cost data",
            what="CUR without resource IDs",
            why=(f"Report '{best['name']}' does not include the resource ID "
                 f"column, so per-resource cost cannot be attributed."),
            how_to_fix=("Recreate the export with resource IDs enabled "
                        "(CUR 1.0: additional schema element RESOURCES)."),
            impact="Per-resource costs remain list-price estimates.")

    return {
        "available": True,
        "reports": reports,
        "best": best,
        "resource_level": best["resource_ids"],
        "quality": "cur" if best["resource_ids"] else "cost-explorer",
        "message": (f"Cost and Usage Report '{best['name']}' found in "
                    f"s3://{best['bucket']}/{best['prefix']} "
                    f"({best['version']}, {best['format'] or 'unknown format'})."),
    }


def finalise_quality(cost_quality, cur_data):
    """
    Downgrade the declared cost basis when the CUR was not actually read.

    `detect()` sets quality="cur" from the report DEFINITION alone — before a
    single byte has been read. A report created minutes ago delivers nothing
    for up to 24 hours, and a partially-readable one is rejected outright, so
    trusting discovery alone made the summary announce "Cost basis: actual
    billed cost (CUR)" over figures that were still list price.

    Returns the same dict, mutated, so callers can use it inline.
    """
    if cost_quality.get("quality") != "cur":
        return cost_quality
    if cur_data.get("available"):
        return cost_quality

    best = cost_quality.get("best") or {}
    delivered = best.get("last_delivery") or ""
    status = best.get("last_status") or ""
    if not delivered:
        # AWS's own answer, not our inference from an empty bucket.
        why = ("AWS has never delivered this report "
               f"(ReportStatus.lastDelivery is empty{', lastStatus=' + status if status else ''}).")
        how = ("The first delivery of a newly created report takes up to 24h. "
               "Re-run after AWS has delivered it once.")
    elif status and status.upper() != "SUCCESS":
        why = f"AWS's last delivery attempt reported '{status}' at {delivered}."
        how = ("Check the CUR bucket policy still allows "
               "billingreports.amazonaws.com to write.")
    else:
        why = cur_data.get("reason", "unknown")
        how = (f"AWS last delivered at {delivered}, so the files exist but "
               f"could not be read — check s3:GetObject on the bucket.")

    cost_quality["quality"] = "cost-explorer"
    cost_quality["cur_read_failed"] = cur_data.get("reason", "unknown")
    gaps.add(
        category="Cost data",
        what="CUR is configured but was not readable this run",
        why=why,
        how_to_fix=how,
        impact=("Costs fall back to list price, so savings are shown as "
                "Estimated rather than Confirmed."))
    return cost_quality
