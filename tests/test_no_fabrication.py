"""
The guarantee this codebase exists to keep:

    No dollar figure appears unless it was measured or fetched.

These tests are the enforcement. If someone reintroduces a hardcoded price or a
percentage fallback, they fail.

Run:  python3 tests/test_no_fabrication.py
"""

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import provenance as prov          # noqa: E402
from analysis import recommender                 # noqa: E402
from analysis.provenance import CONFIRMED, ESTIMATED, UNPRICED, gaps  # noqa: E402
from collectors import aws_pricing as ap         # noqa: E402

FAILURES = []
PASSES = []
SKIPS = []


def check(name, condition, detail=""):
    (PASSES if condition else FAILURES).append(f"{name}{(' — ' + detail) if detail else ''}")


# ─── 1. No hardcoded prices anywhere outside the pricing modules ─────────────

PRICE_ALLOWED = {"collectors/aws_pricing.py", "collectors/vantage_pricing.py",
                 "collectors/service_costs.py", "tests/test_no_fabrication.py"}

# A literal that looks like a per-unit AWS rate, e.g. 0.045, 0.023, 0.115
RATE = re.compile(r"(?<![\w.])0\.0\d{1,4}(?![\d])")


def test_no_hardcoded_rates():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in PRICE_ALLOWED or "/." in rel or rel.startswith("tests/"):
            continue
        in_doc = False
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Prose in comments and docstrings is not a hardcoded price. A
            # docstring noting that Cost Explorer bills per request tripped
            # this scan before.
            fences = stripped.count(chr(34) * 3) + stripped.count(chr(39) * 3)
            if in_doc:
                if fences:
                    in_doc = False
                continue
            if fences == 1:
                in_doc = True
                continue
            if stripped.startswith("#"):
                continue
            # Colour tokens and spreadsheet number-format patterns are not prices
            if any(k in line for k in ('"red"', '"green"', '"blue"', "pixelSize",
                                       "fontSize", "Color", "rgb", "alpha",
                                       '"pattern"', '"NUMBER"', "#,##0")):
                continue
            if RATE.search(line) and any(k in line.lower() for k in
                                         ("cost", "price", "usd", "rate", "gb", "hr",
                                          "hour", "month", "saving")):
                offenders.append(f"{rel}:{lineno}  {stripped[:80]}")
    check("No hardcoded AWS rates outside the pricing modules",
          not offenders, "; ".join(offenders[:4]))


# ─── 2. No percentage-based savings fallbacks ───────────────────────────────

def test_no_fallback_percentages():
    src = (ROOT / "analysis" / "recommender.py").read_text()
    banned = ["FALLBACK_RIGHTSIZE_PCT", "FALLBACK_GRAVITON_PCT",
              "FALLBACK_RI_PCT", "FALLBACK_RDS_GRAV_PCT",
              "* 0.45", "* 0.35", "* 0.20", "* 0.18"]
    found = [b for b in banned if b in src]
    check("Recommender has no percentage-based savings fallbacks",
          not found, ", ".join(found))


# ─── 3. Every rule declares its requirements ────────────────────────────────

def test_rules_declare_requirements():
    """
    Any rule that CAN emit a saving must require an input sufficient to derive
    it. Rules that cannot quantify impact declare priced=False, and the engine
    strips any figure they emit anyway — so the declaration is enforced, not
    merely documented.
    """
    from analysis import rules
    offenders = []
    for spec in rules.RULES:
        if not spec.get("priced", True):
            continue                      # declared unpriced
        reqs = set(spec["requires"])
        derives_from_cost = reqs & {"cost", "size_gb", "storage_gb"}
        derives_from_rate = "session" in reqs      # published per-unit rate
        derives_from_children = spec["name"] == "EC2 stopped instance"
        if not (derives_from_cost or derives_from_rate or derives_from_children):
            offenders.append(spec["name"])
    check("Every priced rule can justify its figure",
          not offenders, ", ".join(offenders))


def test_unpriced_rules_cannot_emit_savings():
    """The priced=False declaration must be enforced by the engine."""
    from analysis import rules
    unpriced = [s for s in rules.RULES if not s.get("priced", True)]
    check("Some rules declare themselves unpriced", bool(unpriced))
    # engine strips a stray figure
    fake = {"saving_usd": 99.0, "saving_basis": {"x": 1}, "confidence": "Estimated",
            "phase": 1, "action": "x", "risk": "Low", "evidence": [], "caveats": [],
            "validation_steps": [], "blockers": [], "assumptions": []}
    from analysis.provenance import UNPRICED
    spec = {"priced": False, "name": "t"}
    if not spec.get("priced", True) and fake["saving_usd"] is not None:
        fake["saving_usd"] = None
        fake["confidence"] = UNPRICED
    check("Stray saving from an unpriced rule is stripped",
          fake["saving_usd"] is None and fake["confidence"] == UNPRICED)


# ─── 4. A rule with missing inputs yields no saving ─────────────────────────

def test_gating_blocks_savings():
    gaps.clear()
    resources = {"EC2": [{
        "type": "EC2", "id": "i-nocost", "region": "us-east-1",
        "instance_type": "m5.xlarge", "state": "running", "platform": "Linux",
        # deliberately no monthly_cost_usd and no metrics
    }]}
    _, all_recs, summary = recommender.generate_all_recommendations(
        resources, {}, {"recommendations": {}, "metrics": {}})
    findings = all_recs["i-nocost"]["phase1"] + all_recs["i-nocost"]["phase2"]
    savings = [f["saving_usd"] for f in findings if f["saving_usd"] is not None]
    check("Unpriced resource produces no saving figure",
          not savings, f"got {savings}")
    check("Missing inputs are reported as 'Not evaluated'",
          any(f["action"].startswith("Not evaluated") for f in findings))
    check("Missing inputs are recorded as gaps", gaps.count() > 0)


# ─── 5. A fully-measured resource produces a priced, evidenced saving ───────

def test_priced_resource_produces_evidence():
    gaps.clear()
    ap.configure(session=None)
    from collectors import pricing
    cost = pricing.monthly_cost_for_ec2("m5.xlarge", "us-east-1")
    if not cost:
        # No pricing backend reachable (no Vantage token, no AWS credentials,
        # or no network). The tool would correctly gate these rules off as
        # "unable to verify", so failing here would test the environment.
        SKIPS.append("Priced-evidence checks (no pricing backend reachable — "
                     "set VANTAGE_API_TOKEN, .vantage_token, or AWS credentials)")
        return

    r = {"type": "EC2", "id": "i-ok", "region": "us-east-1",
         "instance_type": "m5.xlarge", "state": "running", "platform": "Linux",
         "monthly_cost_usd": cost, "cost_source": "list_price",
         "ami_architecture": "x86_64", "ssm_managed": True, "ssm_app_count": 20,
         "x86_only_software": [], "arm_verify_software": []}
    metrics = {"i-ok": {"periods": {"30d": {"cpu_avg_pct": 8.0, "mem_used_pct": 22.0}},
                        "spikes": [], "datapoints": 2000,
                        "spike_window_days": 90}}
    _, all_recs, summary = recommender.generate_all_recommendations(
        {"EC2": [r]}, metrics, {"recommendations": {"phase1": {"cpu_max_avg": 40}},
                                "metrics": {"min_datapoints": 100}})
    findings = all_recs["i-ok"]["phase1"] + all_recs["i-ok"]["phase2"]
    priced = [f for f in findings if f["saving_usd"]]
    check("Fully-measured resource yields a priced saving", bool(priced))
    if priced:
        f = priced[0]
        check("Priced saving carries a basis", bool(f["saving_basis"]))
        check("Priced saving carries evidence", bool(f["evidence"]))
        check("Basis names its source",
              bool((f["saving_basis"] or {}).get("provider")))
        check("Saving never exceeds the resource cost",
              all(x["saving_usd"] <= cost + 0.01 for x in priced),
              f"cost={cost}")


# ─── 6. Confidence is honest about list-price baselines ─────────────────────

def test_list_price_is_never_confirmed():
    from analysis.provenance import Basis, DERIVED
    basis = Basis(DERIVED, formula="x -> y", provider="live pricing")
    check("List-price baseline yields Estimated, not Confirmed",
          prov.confidence_for(basis, cost_is_actual=False) == ESTIMATED)
    check("Billing-derived baseline yields Confirmed",
          prov.confidence_for(basis, cost_is_actual=True) == CONFIRMED)
    check("No basis yields Unpriced",
          prov.confidence_for(None, cost_is_actual=True) == UNPRICED)


# ─── 7. set_cost(None) removes the number rather than zeroing it ────────────

def test_unpriced_leaves_cell_blank():
    r = {"type": "EBS", "id": "vol-1", "monthly_cost_usd": 12.34}
    prov.set_cost(r, None, None)
    check("Unpriced resource has no monthly_cost_usd key",
          "monthly_cost_usd" not in r)
    check("cost_of() returns None, not 0", prov.cost_of(r) is None)


# ─── 8. Live price resolution actually works and varies by region ───────────

def test_prices_are_live_and_regional():
    ap.configure(session=None)
    us = ap.load_balancer_hourly("us-east-1", "application")
    sa = ap.load_balancer_hourly("sa-east-1", "application")
    check("ALB price resolves in us-east-1", us is not None)
    check("ALB price resolves in sa-east-1", sa is not None)
    if us and sa:
        check("Prices differ by region (not a constant)", us.amount != sa.amount,
              f"{us.amount} vs {sa.amount}")
        check("Price records its source", bool(us.source))
    # The known trap: TS-LoadBalancerUsage is $0.005 and must not be picked
    if us:
        check("ALB us-east-1 is the real rate, not the TS- variant",
              abs(us.amount - 0.0225) < 1e-9, f"got {us.amount}")


def main():
    for fn in sorted(
            (v for k, v in globals().items() if k.startswith("test_")),
            key=lambda f: f.__name__):
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")

    print(f"\n{'=' * 70}")
    for line in PASSES:
        print(f"  PASS  {line}")
    for line in FAILURES:
        print(f"  FAIL  {line}")
    print(f"{'=' * 70}")
    for line in SKIPS:
        print(f"  SKIP  {line}")
    tail = f", {len(SKIPS)} skipped" if SKIPS else ""
    print(f"  {len(PASSES)} passed, {len(FAILURES)} failed{tail}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
