"""
Requirement-gated recommendation rules.

Every rule declares the inputs it needs. The engine checks them *before* the
rule runs, so a rule can never quote a dollar figure it lacks the data for —
it is simply not executed, and the reason is recorded as a gap.

That is the anti-fabrication mechanism. It is structural, not a convention:
there is no code path from "missing input" to "plausible-looking number".

Each finding carries:
    saving        float or None   (None means real action, unknown dollar impact)
    saving_basis  how the number was derived, or None
    confidence    Confirmed | Estimated | Unpriced
    evidence      the measured values the finding rests on
"""

from analysis import provenance as prov
from analysis.provenance import (
    Basis, DERIVED, CONFIRMED, ESTIMATED, UNPRICED, gaps, fetched_basis,
)

HOURS_PER_MONTH = 730


# ─── Requirement checks ──────────────────────────────────────────────────────

def _p30(ctx, key):
    return ((ctx["metrics"].get("periods") or {}).get("30d") or {}).get(key)


REQUIREMENTS = {
    "cost": (
        lambda ctx: prov.has_cost(ctx["resource"]),
        "a resolved monthly cost",
        "Enable a Cost and Usage Report, or grant pricing:GetProducts so list "
        "prices can be resolved.",
    ),
    "instance_type": (
        lambda ctx: bool(ctx["resource"].get("instance_type")),
        "the instance type",
        "",
    ),
    "cpu_30d": (
        lambda ctx: _p30(ctx, "cpu_avg_pct") is not None,
        "30-day average CPU",
        "The resource must have reported CPUUtilization to CloudWatch for the "
        "whole window.",
    ),
    "memory_30d": (
        lambda ctx: _p30(ctx, "mem_used_pct") is not None,
        "30-day average memory",
        "Install the CloudWatch Agent so memory is published to the CWAgent "
        "namespace. Without it, rightsizing rests on CPU alone.",
    ),
    "enough_datapoints": (
        lambda ctx: (ctx["min_datapoints"] == 0
                     or ctx["metrics"].get("datapoints", 0) >= ctx["min_datapoints"]),
        "enough CloudWatch history",
        "The resource must exist for the full observation window.",
    ),
    "invocations_30d": (
        lambda ctx: _p30(ctx, "invocations_total") is not None,
        "30-day Lambda invocation count",
        "",
    ),
    "size_gb": (
        lambda ctx: ctx["resource"].get("size_gb") not in (None, ""),
        "the volume size",
        "",
    ),
    "storage_gb": (
        lambda ctx: bool(ctx["resource"].get("storage_gb")),
        "allocated storage",
        "",
    ),
    "free_storage_30d": (
        lambda ctx: _p30(ctx, "free_storage_bytes") is not None,
        "30-day free storage",
        "",
    ),
    "traffic_30d": (
        lambda ctx: ctx["metrics"].get("total_gb_30d") is not None,
        "30-day traffic volume",
        "",
    ),
    "session": (
        lambda ctx: ctx.get("session") is not None,
        "an AWS session for live lifecycle lookup",
        "",
    ),
}


def _check(ctx, required):
    missing = []
    for name in required:
        check, description, fix = REQUIREMENTS[name]
        try:
            ok = check(ctx)
        except Exception:
            ok = False
        if not ok:
            missing.append((name, description, fix))
    return missing


# ─── Finding construction ────────────────────────────────────────────────────

def finding(action, phase, risk, saving=None, saving_basis=None, evidence=None,
            caveats=None, validation_steps=None, blockers=None,
            assumptions=None, cost_is_actual=False):
    confidence = (UNPRICED if saving is None
                  else prov.confidence_for(saving_basis, cost_is_actual))
    return {
        "action": action,
        "phase": phase,
        "risk": risk,
        "saving_usd": None if saving is None else round(saving, 2),
        "saving_basis": saving_basis.to_dict() if saving_basis else None,
        "confidence": confidence,
        "evidence": evidence or [],
        "caveats": caveats or [],
        "validation_steps": validation_steps or [],
        "blockers": blockers or [],
        "assumptions": assumptions or [],
    }


def not_evaluated(action, phase, missing):
    names = ", ".join(d for _, d, _ in missing)
    fixes = [f for _, _, f in missing if f]
    return {
        "action": f"Not evaluated: {action}",
        "phase": phase,
        "risk": "N/A",
        "saving_usd": None,
        "saving_basis": None,
        "confidence": UNPRICED,
        "evidence": [],
        "caveats": [f"Requires {names}, which is not available for this resource."],
        "validation_steps": fixes,
        "blockers": [],
        "assumptions": [],
    }


def _target_saving(current, target, label):
    """
    Saving from swapping to a priced target. Returns (amount, basis) or
    (None, None) when the target could not be priced — never a percentage.
    """
    if current is None or target is None:
        return None, None
    if target >= current:
        return 0.0, Basis(
            DERIVED,
            formula=f"{label}: ${current:,.2f}/mo -> ${target:,.2f}/mo (no saving)",
            provider="live pricing")
    return current - target, Basis(
        DERIVED,
        formula=f"{label}: ${current:,.2f}/mo -> ${target:,.2f}/mo",
        provider="live pricing")


# ─── Rule registry ───────────────────────────────────────────────────────────

RULES = []


def rule(name, applies_to, phase, requires=(), priced=True, aggregate=False):
    """
    Register a recommendation rule.

    priced=False declares that the rule never emits a dollar figure — it
    reports an action whose financial impact cannot be derived from read-only
    data. The engine enforces the declaration, so a rule cannot quietly start
    emitting savings it has not justified.

    aggregate=True means the finding is the same action repeated across many
    resources ("set a retention policy"). One row per resource buries the
    findings that carry money — two such rules produced 41 of 94 rows in a real
    report — so the report collapses them into a single summary row with
    measured totals. The per-resource detail stays on the service tab.
    """
    def decorator(fn):
        RULES.append({
            "name": name,
            "applies_to": set(applies_to) if not isinstance(applies_to, str) else {applies_to},
            "phase": phase,
            "requires": tuple(requires),
            "priced": priced,
            "aggregate": aggregate,
            "fn": fn,
        })
        return fn
    return decorator


def _ensure_rules_loaded():
    """
    Rules are defined in analysis/recommender.py via the @rule decorator, so the
    registry is empty until that module is imported. Without this guard,
    calling run_rules() first yields zero findings and no error — a silent
    "everything is fine" that would be indistinguishable from a clean account.

    Imported lazily to avoid a circular import (recommender imports this module).
    """
    if not RULES:
        from analysis import recommender  # noqa: F401


def run_rules(ctx):
    """Run every applicable rule for one resource, returning its findings."""
    _ensure_rules_loaded()
    rtype = ctx["resource"].get("type", "")
    out = []
    for spec in RULES:
        if rtype not in spec["applies_to"]:
            continue
        missing = _check(ctx, spec["requires"])
        if missing:
            result = spec["fn"](ctx, gated=True)
            if result is not None:
                out.append(not_evaluated(spec["name"], spec["phase"], missing))
                for _, description, fix in missing:
                    gaps.add(
                        category="Analysis",
                        what=f"{spec['name']} on {rtype}",
                        why=f"Missing {description}.",
                        how_to_fix=fix,
                        resource_id=ctx["resource"].get("id", ""),
                        resource_type=rtype,
                        region=ctx["resource"].get("region", ""),
                        impact="This optimisation was not assessed.")
            continue
        try:
            produced = spec["fn"](ctx, gated=False)
        except Exception as e:
            gaps.add(
                category="Analysis",
                what=f"{spec['name']} on {rtype}",
                why=f"Rule raised {type(e).__name__}: {e}",
                how_to_fix="This is a bug — please report it.",
                resource_id=ctx["resource"].get("id", ""),
                resource_type=rtype)
            continue
        if not produced:
            continue
        produced = produced if isinstance(produced, list) else [produced]
        for f in produced:
            f.setdefault("rule", spec["name"])
            f.setdefault("aggregate", spec.get("aggregate", False))
        if not spec.get("priced", True):
            # The rule declared it cannot quantify a saving. If it emitted one
            # anyway that is a bug, so strip it rather than publish an
            # unjustified figure.
            for f in produced:
                if f.get("saving_usd") is not None:
                    gaps.add(
                        category="Analysis",
                        what=f"{spec['name']} emitted a saving despite priced=False",
                        why="Rule declared it cannot quantify impact but returned a figure.",
                        how_to_fix="This is a bug — please report it.",
                        resource_type=rtype)
                    f["saving_usd"] = None
                    f["saving_basis"] = None
                    f["confidence"] = UNPRICED
        _gate_unverified_uptime(ctx, produced)
        out.extend(produced)
    return out


# Below this, the instance type's measured usage is far enough from a full
# month that pricing at 730 hours is a materially different answer.
_UPTIME_DIVERGENCE_PCT = 90.0


def _gate_unverified_uptime(ctx, produced):
    """
    Refuse to publish a precise saving that rests on unmeasured uptime.

    An instance too new to have its own billing history keeps a full-month list
    price, because that is the only defensible figure for it. But when the
    instance TYPE demonstrably runs part-time, a saving computed from 730 hours
    is not merely uncertain — it is wrong by the same multiple, and disclosing
    that in a footnote while printing a confident number is the failure this
    project exists to prevent.

    The saving becomes Unpriced, and the caveat gives the range instead.
    """
    r = ctx["resource"]
    if not r.get("uptime_unverified"):
        return
    type_uptime = r.get("type_uptime_pct")
    if type_uptime is None or type_uptime >= _UPTIME_DIVERGENCE_PCT:
        return

    for f in produced:
        saving = f.get("saving_usd")
        if not saving:
            continue
        lower = round(saving * type_uptime / 100.0, 2)
        f["saving_usd"] = None
        f["saving_basis"] = None
        f["confidence"] = UNPRICED
        f.setdefault("caveats", []).append(
            f"Saving NOT quantified: this instance is too new to have its own "
            f"billing history, and {r.get('instance_type', 'its type')} ran only "
            f"{type_uptime}% of the last window. At a full month the saving "
            f"would be ${saving:,.2f}/mo; at the type's measured uptime it "
            f"would be about ${lower:,.2f}/mo. Re-run once the instance has a "
            f"full window of history.")
        gaps.add(
            category="Analysis",
            what=f"Saving unquantified on {r.get('name') or r.get('id')}",
            why=(f"Cost rests on an assumed full month; the instance type ran "
                 f"{type_uptime}% of the window."),
            how_to_fix=("Re-run once the instance has 30 days of history, or "
                        "enable a CUR with resource IDs for per-instance hours."),
            resource_id=r.get("id", ""),
            resource_type=r.get("type", ""),
            impact=(f"A saving between ${lower:,.2f} and ${saving:,.2f}/mo is "
                    f"real but cannot be pinned down."))
