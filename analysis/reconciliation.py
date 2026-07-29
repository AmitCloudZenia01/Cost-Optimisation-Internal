"""
Prove the report against the bill.

Every other check in this project is internal: provenance is present, columns
line up, a saving never exceeds its resource's cost. All of them pass happily
on a report that is uniformly wrong — and did. Two bugs shipped that a single
glance at the invoice would have caught:

  * 29 Elastic IPs priced at $0.00 against ~$105/mo of real charges, because
    the pricer encoded AWS's pre-February-2024 rule that attached addresses
    were free.
  * A whole account's spend reported as $868.58 against a $1,394.85 bill,
    because promotional credits were netted against usage.

Neither is detectable by reading the code. Both are obvious the moment you
compare the total to Cost Explorer.

The design is deliberately inverted: rather than maintaining a list of AWS
services to cover — which can never be complete, and goes stale every time AWS
changes how something bills — this starts from what was actually charged and
asks how much of it the report can explain. Anything AWS bills that we cannot
account for names itself, including services and usage types that did not exist
when this code was written.
"""

from analysis.provenance import gaps
import analysis.provenance as prov

# A gap is worth reporting when it is material in EITHER absolute or relative
# terms — deliberately OR, not AND.
#
# Requiring both missed the bug this check was written for: 29 Elastic IPs
# mispriced at $0.00 were $105.85/mo, but only 1.76% of a $6,022 bill. A
# percentage test alone hides real money inside a large account, and a dollar
# test alone hides a badly-wrong small one. The cost of a false positive is one
# extra row on a tab built for exactly this; the cost of a false negative is a
# shipped bug, which is what happened.
MATERIAL_USD = 10.0
MATERIAL_PCT = 2.0

# Spend that legitimately has no resource to attach to. These are not failures
# of the report — they are billed activity rather than billed things — but they
# must be named rather than silently absorbed into a delta.
NON_RESOURCE_HINTS = (
    "DataTransfer", "-Out-Bytes", "-In-Bytes", "Requests", "Request",
    "GB-Second", "Lambda-GB-Second", "BoxUsage", "Tax", "Refund",
)


def _attributed_total(resources):
    """Every dollar this report has attached to a specific resource."""
    total = 0.0
    counted = 0
    for _type, items in (resources or {}).items():
        for r in items:
            if not isinstance(r, dict):
                continue
            cost = prov.cost_of(r)
            if cost:
                total += cost
                counted += 1
    return round(total, 2), counted


def _latest_month(monthly_costs):
    months = {row["month"] for row in monthly_costs or []}
    return max(months) if months else ""


def reconcile(monthly_costs, resources, usage_types=None, explained=None):
    """
    `explained` is spend the report accounts for WITHOUT attaching it to a
    resource — data transfer, and compute billed by instances terminated before
    the scan. Both already have their own tab or gap.

    Counting only resource-attributed spend made a report that explained ~100%
    of a bill display 16% coverage, because $868 of transfer and $317 of
    terminated instances were both explained and both uncounted. That number
    would alarm a client about a report that was right.
    """
    return _reconcile(monthly_costs, resources, usage_types, explained or {})


def _reconcile(monthly_costs, resources, usage_types, explained):
    """
    Compare what AWS billed against what this report explains.

    `usage_types` is the Cost Explorer USAGE_TYPE breakdown for the same month
    (from data_transfer.collect or an equivalent query). It is optional; without
    it the totals still reconcile, there is just less detail about the gap.

    Returns a dict suitable for both a sheet and the Summary headline.
    """
    month = _latest_month(monthly_costs)
    if not month:
        return {"available": False}

    billed = round(sum(row["cost"] for row in monthly_costs
                       if row["month"] == month), 2)
    attributed, counted = _attributed_total(resources)
    # Spend the report explains elsewhere rather than attaching to a resource.
    explained_total = round(sum(v for v in explained.values() if v), 2)
    accounted = round(attributed + explained_total, 2)
    unexplained = round(billed - accounted, 2)
    coverage = round(accounted / billed * 100, 1) if billed else 0.0
    resource_coverage = round(attributed / billed * 100, 1) if billed else 0.0

    by_service = {}
    for row in monthly_costs:
        if row["month"] == month and row["cost"] > 0:
            by_service[row["service"]] = round(
                by_service.get(row["service"], 0.0) + row["cost"], 2)

    # Usage types carrying the largest charges, so an unexplained gap points at
    # something specific rather than leaving the reader to guess.
    top_usage = []
    for entry in sorted(usage_types or [], key=lambda u: -u.get("cost_usd", 0)):
        cost = entry.get("cost_usd", 0)
        if cost < MATERIAL_USD:
            continue
        name = entry.get("usage_type", "")
        top_usage.append({
            "usage_type": name,
            "cost_usd": cost,
            "resource_backed": not any(h.lower() in name.lower()
                                       for h in NON_RESOURCE_HINTS),
        })

    material = bool(billed) and (
        abs(unexplained) > MATERIAL_USD
        or abs(unexplained) / billed * 100 > MATERIAL_PCT)

    if material and unexplained > 0:
        detail = ", ".join(f"{u['usage_type']} ${u['cost_usd']:,.2f}"
                           for u in top_usage[:5]) or "no usage-type detail available"
        gaps.add(
            category="Cost data",
            what=f"${unexplained:,.2f} of billed spend is not attributed to any resource",
            why=(f"AWS billed ${billed:,.2f} in {month}. This report attaches "
                 f"${attributed:,.2f} to {counted} resources and explains a "
                 f"further ${explained_total:,.2f} elsewhere "
                 f"(data transfer, terminated instances), leaving "
                 f"${unexplained:,.2f} unaccounted. "
                 f"Largest usage types: {detail}."),
            how_to_fix=("Some of this is normal — data transfer and per-request "
                        "services bill activity, not resources. A large or "
                        "growing gap means a charge this report cannot see. "
                        "Enable a CUR with resource IDs to attribute it."),
            impact=("Savings are computed only against the attributed portion, "
                    "so opportunities in the remainder are invisible."))
    elif material and unexplained < 0:
        # Attributing MORE than was billed means something is counted twice or
        # priced above its real rate — a worse failure than under-attribution,
        # because the savings derived from it are inflated.
        gaps.add(
            category="Cost data",
            what=f"Attributed cost exceeds the bill by ${abs(unexplained):,.2f}",
            why=(f"This report accounts for ${accounted:,.2f} but AWS billed "
                 f"${billed:,.2f} in {month}."),
            how_to_fix=("Check for resources counted under two types, or list "
                        "prices applied to resources that ran part-time."),
            impact="Savings derived from the over-attributed cost are overstated.")

    return {
        "available": True,
        "month": month,
        "billed_usd": billed,
        "attributed_usd": attributed,
        "explained_elsewhere_usd": explained_total,
        "explained_breakdown": {k: round(v, 2) for k, v in explained.items() if v},
        "accounted_usd": accounted,
        "resource_coverage_pct": resource_coverage,
        "unexplained_usd": unexplained,
        "coverage_pct": coverage,
        "resources_priced": counted,
        "material": material,
        "by_service": dict(sorted(by_service.items(), key=lambda kv: -kv[1])),
        "top_usage_types": top_usage[:25],
    }
