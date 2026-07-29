from datetime import timedelta
from collections import defaultdict

from utils import utcnow


def get_account_id(session):
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


def _paged_cost_and_usage(ce, **kwargs):
    """
    get_cost_and_usage has no boto3 paginator — it returns NextPageToken.
    Accounts with many services (especially DAILY granularity over 90 days)
    exceed one page, and ignoring the token silently truncates the totals.
    """
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
    for period in periods:
        month = period["TimePeriod"]["Start"][:7]
        for group in period.get("Groups", []):
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                results.append({"month": month, "service": service, "cost": round(cost, 4)})

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
