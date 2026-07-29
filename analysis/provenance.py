"""
Provenance for every number the report prints.

The rule this module enforces:

    A dollar figure may only exist if we can say where it came from.
    If we cannot, the value is None and the gap is recorded — never a guess.

Four legal sources. Anything else is a bug:

    MEASURED    read from an AWS API (an instance type, a CloudWatch datapoint,
                a CUR line item)
    FETCHED     a published price pulled at runtime, per region
    DERIVED     arithmetic over MEASURED/FETCHED inputs, with the formula stated
    REFERENCE   dated static data that has no API (ElastiCache/Lambda EOL).
                Always carries an as_of date and is excluded from savings totals.

Everything the pipeline could not resolve lands in the gap registry and is
published as the "Data Gaps" tab, so a missing number is visible rather than
silently rendered as zero.
"""

from datetime import date

MEASURED = "measured"
FETCHED = "fetched"
DERIVED = "derived"
REFERENCE = "reference"

# Confidence tiers, used to keep estimates out of the headline total.
CONFIRMED = "Confirmed"      # every input MEASURED or FETCHED
ESTIMATED = "Estimated"      # complete, but rests on list price or partial data
UNPRICED = "Unpriced"        # action is real, dollar impact unknown


class Basis:
    """Why a number is what it is. Attached next to every cost and saving."""

    __slots__ = ("source", "formula", "unit_price", "unit", "provider", "as_of", "note")

    def __init__(self, source, formula="", unit_price=None, unit="",
                 provider="", as_of=None, note=""):
        self.source = source
        self.formula = formula
        self.unit_price = unit_price
        self.unit = unit
        self.provider = provider
        self.as_of = as_of
        self.note = note

    def describe(self):
        bits = []
        if self.formula:
            bits.append(self.formula)
        if self.unit_price is not None:
            bits.append(f"@ ${self.unit_price:,.6f}/{self.unit}".rstrip("/"))
        if self.provider:
            bits.append(f"source: {self.provider}")
        if self.as_of:
            bits.append(f"as of {self.as_of}")
        if self.note:
            bits.append(self.note)
        return " · ".join(bits)

    def to_dict(self):
        return {
            "source": self.source,
            "formula": self.formula,
            "unit_price": self.unit_price,
            "unit": self.unit,
            "provider": self.provider,
            "as_of": str(self.as_of) if self.as_of else "",
            "note": self.note,
            "description": self.describe(),
        }


def fetched_basis(price, formula="", note=""):
    """Build a Basis from a collectors.aws_pricing.Price."""
    if price is None:
        return None
    return Basis(FETCHED, formula=formula, unit_price=price.amount,
                 unit=price.unit, provider=price.source, note=note)


def derived_basis(formula, provider="", note=""):
    return Basis(DERIVED, formula=formula, provider=provider, note=note)


def reference_basis(as_of, note=""):
    return Basis(REFERENCE, as_of=as_of, provider="static reference table", note=note)


# ─── Gap registry ────────────────────────────────────────────────────────────

class GapRegistry:
    """
    Everything we could not price or measure, with the reason and the fix.

    This is a first-class output, not a debug log: it becomes the Data Gaps tab
    and tells the operator exactly what to enable to make the next report
    stronger.
    """

    def __init__(self):
        self._gaps = []
        self._seen = set()

    def add(self, category, what, why, how_to_fix="", resource_id="",
            resource_type="", region="", impact=""):
        key = (category, what, resource_type, region, resource_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self._gaps.append({
            "category": category,
            "what": what,
            "why": why,
            "how_to_fix": how_to_fix,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "region": region,
            "impact": impact,
        })

    def add_price_gap(self, resource_type, region, what, how_to_fix=""):
        self.add(
            category="Pricing",
            what=what,
            why=f"No published price could be resolved for {resource_type} in {region}.",
            how_to_fix=how_to_fix or (
                "Grant pricing:GetProducts (included in ReadOnlyAccess) so the "
                "Price List Query API can be used."),
            resource_type=resource_type,
            region=region,
            impact="Cost and any savings for these resources are omitted.",
        )

    def all(self):
        return list(self._gaps)

    def by_category(self):
        grouped = {}
        for gap in self._gaps:
            grouped.setdefault(gap["category"], []).append(gap)
        return grouped

    def count(self):
        return len(self._gaps)

    def clear(self):
        self._gaps.clear()
        self._seen.clear()


# One registry per run.
gaps = GapRegistry()


# ─── Attaching costs to resources ────────────────────────────────────────────

def set_cost(resource, monthly_usd, basis):
    """
    Record a monthly cost together with its basis.

    Passing None is the correct way to say "we could not price this" — it
    leaves monthly_cost_usd absent so the sheet renders an empty cell rather
    than a misleading $0.00.
    """
    if monthly_usd is None or basis is None:
        resource.pop("monthly_cost_usd", None)
        resource["cost_basis"] = None
        resource["cost_source"] = "unavailable"
        return resource

    resource["monthly_cost_usd"] = round(float(monthly_usd), 4)
    resource["cost_basis"] = basis.to_dict()
    resource["cost_source"] = basis.provider or basis.source
    return resource


def cost_of(resource):
    """Monthly cost if it was resolvable, else None. Never defaults to 0."""
    value = resource.get("monthly_cost_usd")
    return float(value) if value not in (None, "") else None


def has_cost(resource):
    return cost_of(resource) is not None


def basis_text(resource):
    basis = resource.get("cost_basis")
    return basis.get("description", "") if isinstance(basis, dict) else ""


def is_actual_cost(resource):
    """True when the figure came from billing data rather than a list price."""
    return resource.get("cost_source") in ("cost_explorer", "cur")


def confidence_for(saving_basis, cost_is_actual):
    """
    Confidence tier for a saving.

    CONFIRMED needs both a real target price and a cost baseline taken from
    billing. A list-price baseline can still be materially wrong for a customer
    holding Reserved Instances or Savings Plans, so it is only ever ESTIMATED.
    """
    if saving_basis is None:
        return UNPRICED
    # MEASURED means the figure came from billing data itself — AWS's own
    # Savings Plan/RI recommendations are computed from the account's real
    # usage history, which is stronger evidence than anything we derive.
    if saving_basis.source == MEASURED:
        return CONFIRMED
    if saving_basis.source == DERIVED and cost_is_actual:
        return CONFIRMED
    return ESTIMATED


def today():
    return date.today().isoformat()
