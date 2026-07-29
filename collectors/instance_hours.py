"""
Billed instance hours per instance type, from Cost Explorer.

Two failures this fixes, both of which produced confidently wrong numbers:

1. **Part-time instances priced as if they run 24/7.** An inventory scan sees
   an instance and the pricer multiplies its hourly rate by 730. An instance
   that ran 142 hours last month was reported at five times its real cost, and
   every saving derived from it inherited that multiple.

2. **Instances that are billed but no longer exist.** The scan is
   point-in-time, so anything terminated before the run is invisible — the tool
   inventories the present while pricing the past, and simply omits the spend
   without saying so.

`BoxUsage:<type>` usage quantity is billed hours, straight from Cost Explorer.
It is measured, not inferred. The one thing it cannot do is attribute hours to
an individual instance when several of a type exist, so that case is reported
as a gap rather than divided and guessed at.
"""

from datetime import timedelta

from analysis import provenance as prov
from analysis.provenance import Basis, DERIVED, MEASURED, gaps
from collectors.cost_explorer import _paged_cost_and_usage
from utils import utcnow

HOURS_PER_MONTH = 730

# Below this, an instance is materially part-time and pricing it at 730h is
# a real distortion rather than a rounding difference.
PART_TIME_THRESHOLD = 0.95


def collect(session, days=30):
    """
    Billed hours and cost per instance type over a rolling window.

    Rolling, not calendar-month: an instance created this month has no hours in
    the last complete month, so a calendar view leaves exactly the newest — and
    often most expensive — resources priced at a full 730 hours.

    Returns {"available", "month", "window_hours", "by_type": {...}}.
    """
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow()
    start = end - timedelta(days=days)

    try:
        periods = _paged_cost_and_usage(
            ce,
            TimePeriod={"Start": start.strftime("%Y-%m-%d"),
                        "End": end.strftime("%Y-%m-%d")},
            Granularity="MONTHLY",
            Metrics=["UsageQuantity", "UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    except Exception as e:
        gaps.add(
            category="Cost data",
            what="Billed instance hours",
            why=f"Cost Explorer USAGE_TYPE grouping failed: {str(e)[:150]}",
            how_to_fix="Grant ce:GetCostAndUsage.",
            impact=("Instances are priced at 730 hours a month whether or not "
                    "they ran that long."))
        return {"available": False, "by_type": {}}

    if not periods:
        return {"available": False, "by_type": {}}

    window = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
    by_type = {}
    for period in periods:
        for group in period.get("Groups", []):
            usage_type = group["Keys"][0]
            # APS3-BoxUsage:t3.large / BoxUsage:m5.large / APS3-SpotUsage:...
            if "BoxUsage:" not in usage_type and "SpotUsage:" not in usage_type:
                continue
            instance_type = usage_type.split(":", 1)[1]
            hours = float(group["Metrics"]["UsageQuantity"]["Amount"])
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            entry = by_type.setdefault(instance_type,
                                       {"hours": 0.0, "cost": 0.0,
                                        "usage_type": usage_type})
            entry["hours"] += hours
            entry["cost"] += cost

    return {"available": True, "month": window, "window_hours": days * 24,
            "by_type": {k: {"hours": round(v["hours"], 1),
                            "cost": round(v["cost"], 2),
                            "usage_type": v["usage_type"]}
                        for k, v in by_type.items()}}


def apply_uptime(resources, hours_data):
    """
    Replace the 730-hour assumption with measured billed hours.

    Only applied when hours can be attributed to a single instance without
    dividing. Returns the number of resources adjusted.
    """
    if not hours_data.get("available"):
        return 0

    by_type = hours_data["by_type"]
    month = hours_data.get("month", "")
    # Uptime is hours-billed over hours-in-the-window, not over a 730-hour
    # month — the window is rolling and may not be a month long.
    window = hours_data.get("window_hours") or HOURS_PER_MONTH
    instances = [r for r in resources.get("EC2", [])
                 if r.get("state") == "running"]

    counts = {}
    for r in instances:
        counts[r.get("instance_type")] = counts.get(r.get("instance_type"), 0) + 1

    adjusted = 0
    unattributable = []
    too_new = []
    for r in instances:
        itype = r.get("instance_type")
        billed = by_type.get(itype)
        if not billed or not itype:
            continue
        actual = None

        # An instance younger than the window did not run the hours billed
        # against its type — a predecessor of the same type did. Inheriting a
        # terminated instance's uptime is exactly the kind of assumption this
        # tool refuses: it looked measured, but described a different machine.
        age = r.get("age_days")
        if isinstance(age, (int, float)) and age * 24 < window * 0.5:
            too_new.append(r.get("name") or r.get("id"))
            # The instance keeps its full-month list price, because that is the
            # only defensible figure for a machine with no history. But that
            # price is an ASSUMPTION, and any saving derived from it inherits
            # the assumption — so flag it. Rules downstream refuse to publish a
            # precise saving on a cost that rests on unmeasured uptime.
            r["uptime_unverified"] = True
            r["type_uptime_pct"] = round(
                min(1.0, billed["hours"] / window) * 100, 1)
            continue

        count = counts.get(itype, 1)
        full_window = count * window

        if count == 1:
            hours = billed["hours"]
            actual = billed.get("cost")
        elif billed["hours"] >= full_window * 0.98:
            # Every instance of this type ran the whole month, so the split is
            # unambiguous even though there is more than one.
            hours = window
            actual = (billed.get("cost") or 0.0) / count
        else:
            # N instances sharing M hours cannot be split without inventing a
            # distribution. Say so rather than assume an even split.
            unattributable.append(f"{count}x {itype}")
            continue

        uptime = min(1.0, hours / window)
        r["billed_hours"] = round(hours, 1)
        r["uptime_pct"] = round(uptime * 100, 1)
        r["uptime_month"] = month

        if uptime >= PART_TIME_THRESHOLD:
            continue

        # Cost Explorer reports what this usage actually cost, not just how many
        # hours ran. Prefer that over rate x uptime: it is billing data, so the
        # savings derived from it are Confirmed rather than Estimated.
        if actual and actual > 0:
            prov.set_cost(r, round(actual, 2), Basis(
                MEASURED,
                formula=(f"actual billed cost for {round(hours, 1)} hours "
                         f"({r['uptime_pct']}% uptime) over {month}"),
                provider="ce:GetCostAndUsage"))
            r["cost_source"] = "cost_explorer"
            adjusted += 1
        else:
            current = prov.cost_of(r)
            if current:
                prov.set_cost(r, round(current * uptime, 2), Basis(
                    DERIVED,
                    formula=(f"list rate x {r['uptime_pct']}% measured uptime "
                             f"({round(hours, 1)}h billed over {month})"),
                    provider="cost-explorer"))
                r["cost_source"] = "billed_hours"
                adjusted += 1

    for name in sorted(set(too_new)):
        gaps.add(
            category="Cost data",
            what=f"Uptime not measurable ({name})",
            why=("The instance is younger than the measurement window, so the "
                 "hours billed against its instance type were run by an earlier "
                 "instance, not by this one."),
            how_to_fix=("Re-run once the instance has a full window of history, "
                        "or enable a CUR with resource IDs for per-instance hours."),
            impact=("Priced at a full month. If it runs part-time, its cost and "
                    "any saving derived from it are overstated."),
            resource_type="EC2")

    for combo in sorted(set(unattributable)):
        gaps.add(
            category="Cost data",
            what=f"Uptime not attributable ({combo})",
            why=("Several instances share one instance type, and Cost Explorer "
                 "reports hours per type rather than per instance."),
            how_to_fix=("Enable a CUR with resource IDs, which reports hours "
                        "per instance."),
            impact=("These instances are priced at a full month. If any of them "
                    "runs part-time, its cost and savings are overstated."),
            resource_type="EC2")

    return adjusted


def detect_untracked(resources, hours_data):
    """
    Instance types billed last month that no longer exist in the inventory.

    The scan is point-in-time. An instance terminated before the run is real
    spend the report would otherwise omit in silence.
    """
    if not hours_data.get("available"):
        return []

    present = {r.get("instance_type") for r in resources.get("EC2", [])}
    month = hours_data.get("month", "")
    missing = []
    for itype, billed in sorted(hours_data["by_type"].items(),
                                key=lambda kv: -kv[1]["cost"]):
        if itype in present or billed["cost"] <= 0:
            continue
        missing.append({"instance_type": itype, "hours": billed["hours"],
                        "cost_usd": billed["cost"], "month": month})

    if missing:
        total = sum(m["cost_usd"] for m in missing)
        detail = ", ".join(f"{m['instance_type']} ({m['hours']}h, "
                           f"${m['cost_usd']:,.2f})" for m in missing[:6])
        gaps.add(
            category="Coverage",
            what=f"{len(missing)} instance type(s) billed but not in inventory",
            why=(f"{detail} were billed in {month} but no running or stopped "
                 f"instance of those types exists now."),
            how_to_fix=("These were terminated before the scan. Check whether "
                        "the workload moved, or was temporary."),
            impact=(f"${total:,.2f}/mo of {month} compute spend has no resource "
                    f"in this report, so no rule was applied to it."),
            resource_type="EC2")

    return missing
