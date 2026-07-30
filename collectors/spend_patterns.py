"""
The time axis: daily spend per usage type, and what it reveals.

Every accuracy mechanism in this tool answers "is this number right?" for a
snapshot. A hand-analysis of the same account found three things the snapshot
could not show — credits running out mid-month, weekly resize bursts, an
egress spike pattern — because all three exist only as CHANGE over time.

This module is the generic version of the questions that analyst asked. One
Cost Explorer query (daily granularity, grouped by usage type, usage records
only) and five shape detectors that know nothing about any specific service:

    spikes    a day far above the account's own baseline, with its drivers
    bursts    a usage type active on scattered days at a regular cadence —
              the signature of a scheduled job
    growing   a usage type whose second-half spend is far above its first
    started   material spend that began mid-window — something new is billing
    stopped   material spend that ended mid-window — something was turned off

Everything is measured billed cost. No detector claims a saving; each states
what the account is DOING, which is the question the snapshot cannot answer.
"""

from datetime import date, timedelta

from analysis.provenance import gaps
from collectors.cost_explorer import _paged_cost_and_usage
from utils import utcnow

# Floors below which a shape is noise, not a finding. Absolute, not relative:
# a $2 daily wiggle is invisible on any bill, while a $20 one is worth a row
# regardless of account size.
SPIKE_MIN_USD = 20.0        # a day must exceed baseline by at least this
SPIKE_RATIO = 1.8           # ...and by at least this multiple of the median day
MATERIAL_USD = 10.0         # per-usage-type floor over the window
EDGE_DAYS = 3               # started/stopped must be clear of the window edges


def collect(session, days=35):
    """Daily billed cost per usage type. One Cost Explorer query."""
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow()
    start = end - timedelta(days=days)
    try:
        periods = _paged_cost_and_usage(
            ce,
            TimePeriod={"Start": start.strftime("%Y-%m-%d"),
                        "End": end.strftime("%Y-%m-%d")},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    except Exception as e:
        gaps.add(category="Cost data", what="Daily spend patterns",
                 why=f"Cost Explorer daily query failed: {str(e)[:150]}",
                 how_to_fix="Grant ce:GetCostAndUsage.",
                 impact="Spikes, bursts and trends are not analysed.")
        return {"available": False}

    by_usage = {}
    daily_total = {}
    for period in periods:
        day = period["TimePeriod"]["Start"]
        for group in period.get("Groups", []):
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost <= 0:
                continue
            usage_type = group["Keys"][0]
            by_usage.setdefault(usage_type, {})[day] = cost
            daily_total[day] = daily_total.get(day, 0.0) + cost

    if not daily_total:
        return {"available": False}
    return {"available": True, "by_usage": by_usage,
            "daily_total": dict(sorted(daily_total.items())),
            "window_days": days}


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _cadence_days(day_strings):
    """'~weekly' when the gaps between active days cluster around seven."""
    if len(day_strings) < 3:
        return ""
    dates = sorted(date.fromisoformat(d) for d in day_strings)
    intervals = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 1]
    if len(intervals) >= 2 and sum(5 <= g <= 9 for g in intervals) >= len(intervals) - 1:
        return "~weekly"
    return ""


def analyze(data):
    """The five shape detectors. Pure function of collect()'s output."""
    if not data.get("available"):
        return {"available": False}
    by_usage = data["by_usage"]
    daily_total = data["daily_total"]
    all_days = sorted(daily_total)
    if len(all_days) < 10:
        return {"available": False}

    baseline = _median(list(daily_total.values()))

    # ── spikes: days far above the account's own median ─────────────────────
    spikes = []
    for day in all_days:
        total = daily_total[day]
        if total > baseline * SPIKE_RATIO and total - baseline > SPIKE_MIN_USD:
            drivers = []
            for usage_type, series in by_usage.items():
                spent = series.get(day, 0.0)
                usual = _median([series.get(d, 0.0) for d in all_days])
                if spent - usual > SPIKE_MIN_USD / 4:
                    drivers.append((usage_type, round(spent, 2)))
            drivers.sort(key=lambda t: -t[1])
            spikes.append({"date": day, "total": round(total, 2),
                           "baseline": round(baseline, 2),
                           "drivers": drivers[:4]})

    # ── per-usage-type shapes ────────────────────────────────────────────────
    bursts, growing, started, stopped = [], [], [], []
    half = len(all_days) // 2
    first_days, second_days = all_days[:half], all_days[half:]
    window_start = date.fromisoformat(all_days[0])
    window_end = date.fromisoformat(all_days[-1])

    for usage_type, series in by_usage.items():
        total = sum(series.values())
        if total < MATERIAL_USD:
            continue
        active = sorted(series)

        cadence = _cadence_days(active)
        if cadence and len(active) <= len(all_days) * 0.5:
            bursts.append({"usage_type": usage_type, "cost": round(total, 2),
                           "days": [d[5:] for d in active], "cadence": cadence})

        first = sum(series.get(d, 0.0) for d in first_days)
        second = sum(series.get(d, 0.0) for d in second_days)
        if first > 1 and second > first * 1.5 and second - first > MATERIAL_USD:
            growing.append({"usage_type": usage_type,
                            "first_half": round(first, 2),
                            "second_half": round(second, 2),
                            "growth_pct": round((second - first) / first * 100)})

        first_seen = date.fromisoformat(active[0])
        last_seen = date.fromisoformat(active[-1])
        if (first_seen - window_start).days > EDGE_DAYS:
            started.append({"usage_type": usage_type, "first_day": active[0],
                            "cost": round(total, 2)})
        if (window_end - last_seen).days > EDGE_DAYS:
            stopped.append({"usage_type": usage_type, "last_day": active[-1],
                            "cost": round(total, 2)})

    for bucket in (bursts, growing, started, stopped):
        bucket.sort(key=lambda x: -x.get("cost", x.get("second_half", 0)))

    return {"available": True,
            "baseline_daily": round(baseline, 2),
            "spikes": spikes[:10], "bursts": bursts[:10],
            "growing": growing[:10], "started": started[:10],
            "stopped": stopped[:10]}
