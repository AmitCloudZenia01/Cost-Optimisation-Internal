"""
Cost attribution per resource using Cost Explorer RESOURCE_ID grouping.

CE can return cost grouped by individual resource ID — so instead of knowing
"EC2 costs $5,000 this month", we know "instance i-abc123 costs $420/mo".

This requires cost allocation by resource to be enabled in the billing console.
We handle the case where it's not enabled gracefully.
"""

from datetime import timedelta
from collections import defaultdict

from utils import utcnow
from collectors.cost_explorer import _paged_cost_and_usage
from collectors import api_errors
from analysis.provenance import gaps


def get_cost_by_resource(session, days=30):
    """
    Returns {resource_id: monthly_cost_usd} for all resources with spend.
    Falls back to empty dict if resource-level cost allocation is not enabled.
    """
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow()
    start = end - timedelta(days=days)

    try:
        periods = _paged_cost_and_usage(
            ce,
            TimePeriod={
                "Start": start.strftime("%Y-%m-%d"),
                "End":   end.strftime("%Y-%m-%d"),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
        )
    except Exception as e:
        # "Not enabled" and "not permitted" produce the same empty result but
        # mean different things — the first is a customer setting, the second
        # is a blind spot in this report.
        kind = api_errors.classify(e)
        gaps.add(
            category="Cost data",
            what="Per-resource cost attribution",
            why=("Permission denied for Cost Explorer RESOURCE_ID grouping."
                 if kind == "denied" else
                 "Resource-level cost allocation is not enabled on this account."),
            how_to_fix=("Grant ce:GetCostAndUsage." if kind == "denied" else
                        "Enable resource-level cost allocation in Billing settings."),
            impact="Per-resource costs fall back to list price.")
        return {}

    cost_map = {}
    for period in periods:
        for group in period.get("Groups", []):
            resource_id = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if resource_id and cost > 0:
                # Accumulate across months
                cost_map[resource_id] = cost_map.get(resource_id, 0) + round(cost, 4)

    return cost_map


def enrich_resources_with_cost(grouped_resources, cost_by_resource, overwrite=False):
    """
    Attach per-resource cost to each resource dict.

    Fill-only by default: a cost already set by a service collector (e.g. the
    EKS control-plane flat rate) is left alone. CE returns either a bare
    resource id or a full ARN depending on the service, so we try both.
    """
    if not cost_by_resource:
        return grouped_resources

    for resource_type, resources in grouped_resources.items():
        for r in resources:
            actual = (cost_by_resource.get(r.get("id", ""))
                      or cost_by_resource.get(r.get("arn", "")))
            if actual and (overwrite or not r.get("monthly_cost_usd")):
                r["monthly_cost_usd"] = round(actual, 2)
                r["cost_source"] = "cost_explorer"
    return grouped_resources
