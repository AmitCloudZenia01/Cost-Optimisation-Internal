"""
Data transfer and other usage-type spend, from Cost Explorer.

Data transfer is routinely 10-20% of an AWS bill and was entirely invisible
here: it has no "resource" to discover, so an inventory-driven tool misses it
completely. That spend was landing unexplained inside "EC2 - Other".

This does not invent anything — it groups actual billed cost by USAGE_TYPE,
which is real charged money, and buckets the transfer-related ones. It is the
only place in the report where a figure comes straight from billing rather than
from a price list, so these are the most trustworthy numbers we produce.
"""

from datetime import timedelta

from analysis.provenance import gaps
from collectors.cost_explorer import _paged_cost_and_usage
from utils import utcnow

# Usage-type fragments that indicate cross-boundary traffic.
_TRANSFER_MARKERS = (
    "DataTransfer", "-Out-Bytes", "-In-Bytes", "AWS-Out", "AWS-In",
    "DataProcessing-Bytes", "CloudFront", "-Bytes-",
)

# How each bucket is usually reduced — stated as guidance, never as a saving.
_GUIDANCE = {
    "NAT Gateway processing":
        "Route S3/DynamoDB traffic via VPC Gateway Endpoints (free) instead of NAT.",
    "Inter-AZ transfer":
        "Co-locate chatty services in one AZ, or use cross-zone-aware routing.",
    "Internet egress":
        "Serve public traffic through CloudFront; its egress rate is lower than direct EC2 egress.",
    "Inter-region transfer":
        "Confirm cross-region replication is required; consider regional caches.",
    "VPC peering / endpoints":
        "Check whether interface endpoints could be gateway endpoints.",
}


def _bucket(usage_type):
    u = usage_type.lower()
    if "natgateway-bytes" in u:
        return "NAT Gateway processing"
    if "regional-bytes" in u or "-datatransfer-regional" in u:
        return "Inter-AZ transfer"
    if "-out-bytes" in u or "aws-out" in u or "dataxfer-out" in u:
        return "Internet egress"
    if "-in-bytes" in u or "aws-in" in u:
        return "Inbound transfer (usually free)"
    if "peering" in u or "vpcendpoint" in u or "privatelink" in u:
        return "VPC peering / endpoints"
    if "cloudfront" in u:
        return "CloudFront delivery"
    if "interregion" in u or "-xr-" in u:
        return "Inter-region transfer"
    return "Other transfer"


def collect(session, days=30):
    """
    Returns {"total_usd": float, "buckets": [...], "top_usage_types": [...]}.

    Every figure here is billed cost from Cost Explorer, not a price-list
    estimate — so it is reported as measured, not estimated.
    """
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow().replace(day=1)
    start = (end - timedelta(days=days)).replace(day=1)

    try:
        periods = _paged_cost_and_usage(
            ce,
            TimePeriod={"Start": start.strftime("%Y-%m-%d"),
                        "End": end.strftime("%Y-%m-%d")},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    except Exception as e:
        gaps.add(
            category="Cost data",
            what="Data transfer breakdown",
            why=f"Cost Explorer USAGE_TYPE grouping failed: {str(e)[:150]}",
            how_to_fix="Grant ce:GetCostAndUsage.",
            impact="Data transfer spend is not attributed and remains inside "
                   "the 'EC2 - Other' line item.")
        return {"total_usd": 0.0, "buckets": [], "top_usage_types": [],
                "available": False}

    # Use the most recent complete month so partial-month data cannot skew it.
    latest_month = ""
    by_usage = {}
    qty_by_usage = {}
    for period in periods:
        month = period["TimePeriod"]["Start"][:7]
        latest_month = max(latest_month, month)
    for period in periods:
        if period["TimePeriod"]["Start"][:7] != latest_month:
            continue
        for group in period.get("Groups", []):
            usage_type = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                by_usage[usage_type] = by_usage.get(usage_type, 0.0) + cost
                qty_by_usage[usage_type] = (qty_by_usage.get(usage_type, 0.0)
                    + float(group["Metrics"].get("UsageQuantity", {}).get("Amount", 0)))

    transfer = {u: c for u, c in by_usage.items()
                if any(m.lower() in u.lower() for m in _TRANSFER_MARKERS)}

    buckets = {}
    for usage_type, cost in transfer.items():
        name = _bucket(usage_type)
        entry = buckets.setdefault(name, {"bucket": name, "cost_usd": 0.0,
                                          "usage_types": []})
        entry["cost_usd"] += cost
        entry["usage_types"].append(usage_type)

    bucket_rows = []
    for entry in buckets.values():
        entry["cost_usd"] = round(entry["cost_usd"], 2)
        entry["guidance"] = _GUIDANCE.get(entry["bucket"], "")
        entry["usage_types"] = ", ".join(sorted(entry["usage_types"])[:6])
        bucket_rows.append(entry)
    bucket_rows.sort(key=lambda b: b["cost_usd"], reverse=True)

    top = sorted(transfer.items(), key=lambda kv: kv[1], reverse=True)[:20]

    # GB of internet egress, so the CDN recommendation can name a measured
    # volume ("6.6 TB served from EC2") rather than only a dollar figure.
    egress_cost = sum(c for u, c in transfer.items()
                      if _bucket(u) == "Internet egress")
    egress_gb = sum(qty_by_usage.get(u, 0.0) for u in transfer
                    if _bucket(u) == "Internet egress")

    return {
        "available": True,
        "month": latest_month,
        "total_usd": round(sum(transfer.values()), 2),
        "egress_usd": round(egress_cost, 2),
        "egress_gb": round(egress_gb, 1),
        "buckets": bucket_rows,
        "top_usage_types": [{"usage_type": u, "cost_usd": round(c, 2),
                             "gb": round(qty_by_usage.get(u, 0.0), 1)}
                            for u, c in top],
    }
