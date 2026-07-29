"""
Savings Plan and Reserved Instance purchase recommendations, from AWS itself.

These come from Cost Explorer's own recommendation engine
(ce:GetSavingsPlansPurchaseRecommendation, ce:GetReservationPurchaseRecommendation),
which analyses the account's actual historical usage and returns AWS's own
estimated saving, commitment and utilisation.

That matters for accuracy: a Savings Plan is a dollar-per-hour commitment across
compute, so its value depends on the *shape* of usage over time — not on any
per-resource figure this tool can derive. Computing it ourselves from
inventory would be guesswork. AWS has the billing history and does the
analysis; we report its answer and say where it came from.

Read-only. Note that Cost Explorer bills roughly $0.01 per request, so the
number of calls is kept deliberately small.
"""

from analysis.provenance import gaps

# One call per (type, term, payment). Kept short on purpose — each is billable.
_SP_QUERIES = [
    ("COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT"),
    ("COMPUTE_SP", "THREE_YEARS", "NO_UPFRONT"),
]

_RI_SERVICES = ["Amazon Elastic Compute Cloud - Compute",
                "Amazon Relational Database Service"]


_CREDENTIAL_ERRORS = ("ExpiredToken", "InvalidClientTokenId", "UnrecognizedClient",
                      "AccessDenied", "SignatureDoesNotMatch", "TokenRefreshRequired")


def _is_credential_error(exc):
    text = str(exc)
    return any(k in text for k in _CREDENTIAL_ERRORS)


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# AWS offers SEVEN_DAYS, THIRTY_DAYS and SIXTY_DAYS. Thirty days is one month
# of behaviour, which is not enough to justify a commitment measured in years —
# a single busy month reads as a permanent baseline. Sixty is the longest AWS
# exposes, so it is the least-bad evidence available.
DEFAULT_LOOKBACK = "SIXTY_DAYS"


def savings_plan_recommendations(session, lookback=DEFAULT_LOOKBACK, status=None):
    """AWS's own Savings Plan purchase recommendations."""
    out = []
    status = status if status is not None else {}
    ce = session.client("ce", region_name="us-east-1")
    for sp_type, term, payment in _SP_QUERIES:
        try:
            resp = ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType=sp_type,
                TermInYears=term,
                PaymentOption=payment,
                LookbackPeriodInDays=lookback,
            )
        except Exception as e:
            if _is_credential_error(e):
                status["credentials_failed"] = True
            status["failed"] = True
            gaps.add(
                category="Commitments",
                what=f"Savings Plan recommendation ({sp_type} {term})",
                why=f"ce:GetSavingsPlansPurchaseRecommendation failed: {str(e)[:150]}",
                how_to_fix="Grant ce:GetSavingsPlansPurchaseRecommendation.",
                impact="Savings Plan opportunities are not reported.")
            continue

        rec = resp.get("SavingsPlansPurchaseRecommendation") or {}
        summary = rec.get("SavingsPlansPurchaseRecommendationSummary") or {}
        monthly = _f(summary.get("EstimatedMonthlySavingsAmount"))
        if not monthly:
            continue
        out.append({
            "type": sp_type,
            "term": term,
            "payment": payment,
            "lookback_days": rec.get("LookbackPeriodInDays", lookback),
            "hourly_commitment": _f(summary.get("HourlyCommitmentToPurchase")),
            "monthly_savings": monthly,
            "savings_pct": _f(summary.get("EstimatedSavingsPercentage")),
            "roi_pct": _f(summary.get("EstimatedROI")),
            "current_on_demand": _f(summary.get("CurrentOnDemandSpend")),
            "estimated_total_cost": _f(summary.get("EstimatedTotalCost")),
            "recommendation_count": summary.get("TotalRecommendationCount"),
            "source": "ce:GetSavingsPlansPurchaseRecommendation",
        })
    return out


def reservation_recommendations(session, lookback=DEFAULT_LOOKBACK, status=None):
    """AWS's own Reserved Instance purchase recommendations, per service."""
    out = []
    status = status if status is not None else {}
    ce = session.client("ce", region_name="us-east-1")
    for service in _RI_SERVICES:
        try:
            resp = ce.get_reservation_purchase_recommendation(
                Service=service,
                LookbackPeriodInDays=lookback,
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
            )
        except Exception as e:
            if _is_credential_error(e):
                status["credentials_failed"] = True
            status["failed"] = True
            gaps.add(
                category="Commitments",
                what=f"Reserved Instance recommendation ({service})",
                why=f"ce:GetReservationPurchaseRecommendation failed: {str(e)[:150]}",
                how_to_fix="Grant ce:GetReservationPurchaseRecommendation.",
                impact="Reservation opportunities are not reported for this service.")
            continue

        for rec in resp.get("Recommendations", []):
            summary = rec.get("RecommendationSummary") or {}
            monthly = _f(summary.get("TotalEstimatedMonthlySavingsAmount"))
            if not monthly:
                continue
            details = []
            for d in rec.get("RecommendationDetails", [])[:10]:
                inst = d.get("InstanceDetails") or {}
                spec = next((v for v in inst.values() if isinstance(v, dict)), {})
                details.append({
                    "instance_type": spec.get("InstanceType") or spec.get("NodeType", ""),
                    "region": spec.get("Region", ""),
                    "quantity": d.get("RecommendedNumberOfInstancesToPurchase"),
                    "monthly_savings": _f(d.get("EstimatedMonthlySavingsAmount")),
                    "savings_pct": _f(d.get("EstimatedSavingsPercentage")),
                    "utilisation_pct": _f(d.get("AverageUtilization")),
                    "break_even_months": d.get("EstimatedBreakEvenInMonths"),
                })
            out.append({
                "service": service,
                "term": "ONE_YEAR",
                "payment": "NO_UPFRONT",
                "monthly_savings": monthly,
                "savings_pct": _f(summary.get("TotalEstimatedMonthlySavingsPercentage")),
                "details": details,
                "source": "ce:GetReservationPurchaseRecommendation",
            })
    return out


def existing_coverage(session):
    """Utilisation and coverage of commitments the account already holds."""
    from datetime import timedelta
    from utils import utcnow

    ce = session.client("ce", region_name="us-east-1")
    end = utcnow().replace(day=1)
    start = (end - timedelta(days=32)).replace(day=1)
    period = {"Start": start.strftime("%Y-%m-%d"), "End": end.strftime("%Y-%m-%d")}
    result = {}

    try:
        resp = ce.get_savings_plans_utilization(TimePeriod=period)
        total = (resp.get("Total") or {}).get("Utilization") or {}
        if total:
            result["savings_plan_utilisation_pct"] = _f(total.get("UtilizationPercentage"))
    except Exception:
        pass

    try:
        resp = ce.get_reservation_utilization(TimePeriod=period)
        total = (resp.get("Total") or {})
        if total:
            result["reservation_utilisation_pct"] = _f(total.get("UtilizationPercentage"))
    except Exception:
        pass

    return result


def collect(session):
    """
    Everything AWS itself recommends buying, plus how well existing
    commitments are being used.
    """
    status = {}
    plans = savings_plan_recommendations(session, status=status)
    reservations = reservation_recommendations(session, status=status)
    coverage = existing_coverage(session)

    # "Could not check" must never read as "nothing to find". An expired SSO
    # token produced a $0 commitment figure indistinguishable from a clean
    # result, which is exactly the kind of silent wrongness this project bans.
    if status.get("credentials_failed"):
        gaps.add(
            category="Commitments",
            what="Savings Plan / Reserved Instance analysis",
            why="AWS credentials expired or were rejected during the run, so "
                "purchase recommendations could not be retrieved.",
            how_to_fix="Refresh credentials (aws sso login) and re-run.",
            impact="NOT ASSESSED - the $0 shown means 'not checked', not "
                   "'nothing available'.")

    best_plan = max(plans, key=lambda p: p["monthly_savings"], default=None)

    # A Compute Savings Plan and an EC2 Reserved Instance discount the SAME
    # compute hours, so adding them double-counts. Only the larger of the two
    # is counted. RDS/ElastiCache/Redshift reservations do not overlap a
    # Compute SP and are additive.
    #
    # (Summing everything would have reported $2,216.98/mo here when the real
    # non-overlapping figure is $1,466.06 — a 51% overstatement.)
    ec2_ri = next((r for r in reservations
                   if "Elastic Compute Cloud" in r["service"]), None)
    other_ri = [r for r in reservations if r is not ec2_ri]

    sp_amount = best_plan["monthly_savings"] if best_plan else 0.0
    ec2_ri_amount = ec2_ri["monthly_savings"] if ec2_ri else 0.0
    compute_choice = max(sp_amount, ec2_ri_amount)
    compute_via = ("Savings Plan" if sp_amount >= ec2_ri_amount else "Reserved Instances")
    total = compute_choice + sum(r["monthly_savings"] for r in other_ri)

    return {
        "available": not status.get("failed", False),
        "credentials_failed": status.get("credentials_failed", False),
        "savings_plans": plans,
        "best_savings_plan": best_plan,
        "reservations": reservations,
        "ec2_reservation": ec2_ri,
        "other_reservations": other_ri,
        "coverage": coverage,
        "compute_best_route": compute_via,
        "compute_monthly_savings": round(compute_choice, 2),
        "non_compute_monthly_savings": round(sum(r["monthly_savings"] for r in other_ri), 2),
        "total_monthly_savings": round(total, 2),
        "overlap_note": (
            f"A Compute Savings Plan (${sp_amount:,.2f}/mo) and EC2 Reserved "
            f"Instances (${ec2_ri_amount:,.2f}/mo) discount the same compute "
            f"hours, so only the larger is counted."
        ) if (sp_amount and ec2_ri_amount) else "",
    }


# Actions that shrink the compute baseline a commitment is priced against.
# Committing to capacity the same report tells you to remove is the classic
# way a Savings Plan turns into a loss.
_SHRINKING_ACTIONS = ("rightsize", "graviton", "terminate", "delete", "stop",
                      "downsize", "idle", "unused")


def assess_commitment_risk(purchase_data, monthly_costs=None, findings=None):
    """
    What could make a recommended commitment a bad decision.

    AWS's saving figure is sound arithmetic on the last N days. It is silent on
    whether those N days predict the next one-to-three YEARS, and silent on the
    fact that this same report recommends removing some of the very capacity
    being committed to. Both are stated here so the reader decides with them in
    view rather than after signing.
    """
    plan = (purchase_data or {}).get("best_savings_plan")
    if not plan:
        return {}

    years = 3 if plan.get("term") == "THREE_YEARS" else 1
    exposure = round(plan.get("hourly_commitment", 0.0) * 8760 * years, 2)

    warnings = []

    # 1. Is the evidence window long enough for the commitment length?
    lookback = str(plan.get("lookback_days", "")).replace("_", " ").lower()
    if "thirty" in lookback or "seven" in lookback:
        warnings.append(
            f"Built on {lookback} of history but commits for {years} year(s). "
            f"One busy month reads as a permanent baseline.")

    # 2. Is compute spend actually growing, flat, or falling?
    trend = _compute_trend(monthly_costs)
    if trend and trend["direction"] == "falling":
        warnings.append(
            f"Compute spend is falling ({trend['detail']}). A commitment is "
            f"priced against usage that is shrinking.")

    # 3. Does this report simultaneously recommend removing that capacity?
    conflicts = []
    for f in (findings or []):
        action = str(f.get("action", "")).lower()
        if any(word in action for word in _SHRINKING_ACTIONS) and f.get("saving_usd"):
            conflicts.append(f)
    if conflicts:
        total = sum(f["saving_usd"] for f in conflicts)
        warnings.append(
            f"This report also recommends {len(conflicts)} action(s) worth "
            f"${total:,.2f}/mo that reduce compute. Do those first — a "
            f"commitment locks in capacity you were about to remove.")

    return {
        "exposure_usd": exposure,
        "term_years": years,
        "trend": trend,
        "conflict_count": len(conflicts),
        "conflict_savings": round(sum(f["saving_usd"] for f in conflicts), 2),
        "warnings": warnings,
        # A three-year term on short or falling evidence is not defensible.
        "prefer_shorter_term": years > 1 and bool(warnings),
    }


def _compute_trend(monthly_costs):
    """Direction of compute spend across the months we have."""
    if not monthly_costs:
        return None
    by_month = {}
    for row in monthly_costs:
        if "compute" in row.get("service", "").lower():
            by_month[row["month"]] = by_month.get(row["month"], 0.0) + row["cost"]
    months = sorted(by_month)
    if len(months) < 2:
        return None

    first, last = by_month[months[0]], by_month[months[-1]]
    if first <= 0:
        return None
    change = (last - first) / first * 100
    direction = "falling" if change < -5 else "growing" if change > 5 else "flat"
    return {
        "direction": direction,
        "change_pct": round(change, 1),
        "detail": (f"{months[0]} ${first:,.2f} -> {months[-1]} ${last:,.2f}, "
                   f"{change:+.1f}%"),
        "months": len(months),
    }
