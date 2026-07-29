from datetime import timedelta
from collections import defaultdict

from analysis.provenance import gaps
from utils import utcnow


def get_account_id(session):
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


# Cost Explorer returns Usage, Credit, Refund, Tax and RI/SP fee rows together.
# Summing them nets promotional credits against real usage, which is wrong for
# an optimisation report: you optimise the resources you consume, and credits
# expire. On a fully credit-covered account this made every service net to
# roughly zero, and the `cost > 0` guard downstream then discarded whichever
# services went negative — leaving an arbitrary positive residue as "spend".
USAGE_ONLY = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}


def _paged_cost_and_usage(ce, record_types=USAGE_ONLY, **kwargs):
    """
    get_cost_and_usage has no boto3 paginator — it returns NextPageToken.
    Accounts with many services (especially DAILY granularity over 90 days)
    exceed one page, and ignoring the token silently truncates the totals.

    Restricted to RECORD_TYPE=Usage by default. Pass record_types=None to read
    credits, refunds and tax — see get_credit_coverage().
    """
    if record_types is not None:
        existing = kwargs.get("Filter")
        kwargs["Filter"] = ({"And": [existing, record_types]} if existing
                            else record_types)
    results = []
    next_token = None
    while True:
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp.get("ResultsByTime", []))
        next_token = resp.get("NextPageToken")
        if not next_token:
            return results


def get_monthly_costs(session, months=3):
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow().replace(day=1)
    start = (end - timedelta(days=months * 31)).replace(day=1)

    periods = _paged_cost_and_usage(
        ce,
        TimePeriod={
            "Start": start.strftime("%Y-%m-%d"),
            "End": end.strftime("%Y-%m-%d"),
        },
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    results = []
    negatives = 0.0
    for period in periods:
        month = period["TimePeriod"]["Start"][:7]
        for group in period.get("Groups", []):
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                results.append({"month": month, "service": service, "cost": round(cost, 4)})
            elif cost < 0:
                negatives += cost

    # With RECORD_TYPE=Usage a negative should be impossible. If one appears the
    # filter is not doing what we think, and dropping it silently would understate
    # spend exactly as it did before — so say so instead.
    if negatives < -0.01:
        gaps.add(
            category="Cost data",
            what="Negative cost rows discarded",
            why=(f"${abs(negatives):,.2f} of negative service cost survived the "
                 f"RECORD_TYPE=Usage filter and was excluded from the totals."),
            how_to_fix="Check for refunds or a credit type not classified as Credit.",
            impact="Reported spend may be understated by that amount.")

    return results


def get_daily_costs(session, days=90):
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow()
    start = end - timedelta(days=days)

    periods = _paged_cost_and_usage(
        ce,
        TimePeriod={
            "Start": start.strftime("%Y-%m-%d"),
            "End": end.strftime("%Y-%m-%d"),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    results = []
    for period in periods:
        date = period["TimePeriod"]["Start"]
        for group in period.get("Groups", []):
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                results.append({"date": date, "service": service, "cost": round(cost, 4)})

    return results


def get_costs_by_tag(session, tag_key="Project", months=3):
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow().replace(day=1)
    start = (end - timedelta(days=months * 31)).replace(day=1)

    try:
        periods = _paged_cost_and_usage(
            ce,
            TimePeriod={
                "Start": start.strftime("%Y-%m-%d"),
                "End": end.strftime("%Y-%m-%d"),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "TAG", "Key": tag_key}],
        )
    except Exception:
        return []

    results = []
    for period in periods:
        month = period["TimePeriod"]["Start"][:7]
        for group in period.get("Groups", []):
            tag_value = group["Keys"][0].replace(f"{tag_key}$", "")
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                results.append({"month": month, "tag": tag_value or "Untagged", "cost": round(cost, 4)})

    return results


def get_service_totals(monthly_costs):
    totals = defaultdict(float)
    for row in monthly_costs:
        totals[row["service"]] += row["cost"]
    return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))


def get_cost_trend(monthly_costs):
    by_month = defaultdict(float)
    for row in monthly_costs:
        by_month[row["month"]] += row["cost"]
    return dict(sorted(by_month.items()))


def get_credit_coverage(session, months=3):
    """
    How much of the bill is paid by credits, and what the true usage is.

    This is deliberately the one query that does NOT filter RECORD_TYPE. An
    account running entirely on promotional credits looks free in every netted
    view, and its owner is often unaware how much real spend is underneath —
    until the credits expire and the bill arrives at full price. That is the
    single most important fact about such an account, so the report states it.
    """
    ce = session.client("ce", region_name="us-east-1")
    end = utcnow().replace(day=1)
    start = (end - timedelta(days=months * 31)).replace(day=1)

    try:
        periods = _paged_cost_and_usage(
            ce, record_types=None,
            TimePeriod={"Start": start.strftime("%Y-%m-%d"),
                        "End": end.strftime("%Y-%m-%d")},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        )
    except Exception as e:
        gaps.add(
            category="Cost data",
            what="Credit coverage",
            why=f"Cost Explorer RECORD_TYPE grouping failed: {str(e)[:150]}",
            how_to_fix="Grant ce:GetCostAndUsage.",
            impact="Cannot tell whether spend is being paid by expiring credits.")
        return {"available": False}

    by_month = {}
    for period in periods:
        month = period["TimePeriod"]["Start"][:7]
        entry = by_month.setdefault(month, {"usage": 0.0, "credit": 0.0, "tax": 0.0})
        for group in period.get("Groups", []):
            kind = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if kind == "Usage":
                entry["usage"] += amount
            elif kind in ("Credit", "Refund"):
                entry["credit"] += amount          # negative
            elif kind == "Tax":
                entry["tax"] += amount

    if not by_month:
        return {"available": False}

    latest = max(by_month)
    usage = by_month[latest]["usage"]
    covered = -by_month[latest]["credit"]
    pct = round(covered / usage * 100, 1) if usage > 0 else 0.0

    if pct >= 5:
        gaps.add(
            category="Cost data",
            what=f"{pct}% of usage is paid by credits",
            why=(f"In {latest}, ${usage:,.2f} of usage was offset by "
                 f"${covered:,.2f} of credits, so the invoice was "
                 f"${max(0.0, usage - covered):,.2f}."),
            how_to_fix=("Confirm the credit expiry date. Savings in this report "
                        "are computed against real usage, which is what you pay "
                        "once credits run out."),
            impact="Net spend today understates what this account will cost.")

    return {
        "available": True,
        "month": latest,
        "usage_usd": round(usage, 2),
        "credit_usd": round(covered, 2),
        "invoiced_usd": round(max(0.0, usage - covered), 2),
        "covered_pct": pct,
        "by_month": {m: {k: round(v, 2) for k, v in d.items()}
                     for m, d in sorted(by_month.items())},
    }


def get_usage_type_costs(session, contains, days=35):
    """
    Actual billed cost per usage type for the latest complete month.

    Used where a charge must be attributed but counting resources and
    multiplying by a list rate would over-attribute — public IPv4 being the
    case that prompted this. AWS's own figure cannot exceed AWS's own bill.
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
    except Exception:
        return {}

    latest = ""
    for p in periods:
        latest = max(latest, p["TimePeriod"]["Start"][:7])
    out = {}
    for p in periods:
        if p["TimePeriod"]["Start"][:7] != latest:
            continue
        for g in p.get("Groups", []):
            name = g["Keys"][0]
            if contains.lower() not in name.lower():
                continue
            out[name] = out.get(name, 0.0) + float(
                g["Metrics"]["UnblendedCost"]["Amount"])
    return {k: round(v, 4) for k, v in out.items()}
