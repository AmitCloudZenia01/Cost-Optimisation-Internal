"""
Recommendation rules and the orchestrator that runs them.

Every saving here is the difference between two live prices, or it is None.
There are no fallback percentages: the old 45% rightsize / 20% Graviton /
35% RI multipliers produced confident-looking numbers that were, for most
accounts and most regions, simply wrong.

Rules declare their inputs (see analysis/rules.py). A rule whose inputs are
missing does not run, and the report says so.
"""

import re

from analysis import provenance as prov
from analysis.provenance import (
    Basis, DERIVED, CONFIRMED, ESTIMATED, UNPRICED, gaps,
)
from analysis.rules import (
    RULES, rule, run_rules, finding, _target_saving, HOURS_PER_MONTH,
)
from analysis.spike_detector import has_safe_utilization, peak_utilisation
from analysis.lifecycle import (
    check_eks_version, check_rds_version, check_elasticache_version,
    rds_graviton_compatible,
)
from analysis.graviton_check import (
    assess as graviton_assess, RESULT_ALREADY_GRAVITON, RESULT_BLOCKED, RESULT_WINDOWS,
)
from collectors import pricing, aws_pricing as ap, commitments, vantage_pricing

GRAVITON_MAP = {
    "t3": "t4g",  "t3a": "t4g",  "t2": "t4g",
    "m5": "m7g",  "m5a": "m7g",  "m6i": "m7g",  "m6a": "m7g",  "m4": "m7g",
    "c5": "c7g",  "c5a": "c7g",  "c6i": "c7g",  "c6a": "c7g",  "c4": "c7g",
    "r5": "r7g",  "r5a": "r7g",  "r6i": "r7g",  "r6a": "r7g",  "r4": "r7g",
    "i3": "im4gn",
}

RDS_GRAVITON_MAP = {
    "db.m5": "db.m7g", "db.m6i": "db.m7g", "db.m4": "db.m7g",
    "db.r5": "db.r7g", "db.r6i": "db.r7g", "db.t3": "db.t4g",
}

RIGHTSIZE_MAP = {
    "small": "micro",   "medium": "small",    "large": "medium",
    "xlarge": "large",  "2xlarge": "xlarge",  "4xlarge": "2xlarge",
    "8xlarge": "4xlarge", "12xlarge": "8xlarge", "16xlarge": "8xlarge",
    "24xlarge": "12xlarge", "32xlarge": "16xlarge", "48xlarge": "24xlarge",
}


# Superseded families and their current-generation equivalent. Recommending
# t2.medium -> t2.small keeps the customer two generations behind: t3.small is
# both cheaper and faster than t2.small, so the smaller-size-same-family answer
# is never the best one when the family itself is obsolete.
MODERN_FAMILY = {
    "t2": "t3", "m3": "m5", "m4": "m5", "c3": "c5", "c4": "c5",
    "r3": "r5", "r4": "r5", "i2": "i3", "p2": "p3",
}


def _split(instance_type):
    parts = str(instance_type).split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (instance_type, "")


def _memory_gb(instance_type):
    """Memory in GiB from the live spec source, or None if unavailable."""
    try:
        from collectors import vantage_pricing
        _vcpu, memory = vantage_pricing.ec2_specs(instance_type)
        return float(memory) if memory else None
    except Exception:
        return None


def _best_target(family, smaller_size, region):
    """
    The instance we should actually move to.

    Prefers the current-generation equivalent of an obsolete family, but only
    if AWS actually offers it in this region at that size — verified, never
    assumed. Falls back to the same family when it does not.
    """
    same_family = f"{family}.{smaller_size}"
    modern = MODERN_FAMILY.get(family)
    if not modern:
        return same_family, None

    candidate = f"{modern}.{smaller_size}"
    if pricing.instance_type_exists(candidate, region) is False:
        return same_family, None
    return candidate, (f"{family} is a previous-generation family; "
                       f"{modern} is the current equivalent and is both "
                       f"cheaper and faster at the same size.")


def _ev(ctx, *pairs):
    """Evidence strings: the measured values a finding rests on."""
    out = []
    for label, value in pairs:
        if value is not None and value != "":
            out.append(f"{label}: {value}")
    return out


def _specs_for(service, instance_type, region=None):
    if service == "EC2":
        return pricing.instance_specs(instance_type, region)
    if service == "RDS":
        return pricing.rds_specs(instance_type)
    if service == "ElastiCache":
        return pricing.cache_specs(instance_type)
    return None, None


def _fits_peak(current_type, target_type, metrics, ceiling_pct=75, region=None,
               service="EC2"):
    """
    Would the observed peak still fit on the smaller instance — on BOTH CPU
    and memory?

    (fits: True/False/None, explanation). None means "unable to verify"; the
    caller must never treat that as a yes.

    Downsizing halves vCPU *and* RAM, so the same workload doubles in relative
    terms on both axes. An m5.xlarge (4 vCPU, 16 GiB) at 60% CPU needs 2.4
    vCPU — too much for an m5.large's 2. Memory is checked the same way: a
    workload at 60% of 16 GiB is 9.6 GiB and will not fit in 8 GiB, which
    matters because memory exhaustion kills a process outright while CPU
    saturation merely slows it.
    """
    cur_vcpu, cur_mem = _specs_for(service, current_type, region)
    tgt_vcpu, tgt_mem = _specs_for(service, target_type, region)

    notes = []
    verdicts = []

    # ── CPU ──
    cpu_peak, how = peak_utilisation(metrics, "cpu")
    if cpu_peak is None:
        notes.append("peak CPU not reported")
        verdicts.append(None)
    elif not cur_vcpu or not tgt_vcpu:
        notes.append("vCPU counts unavailable")
        verdicts.append(None)
    else:
        projected = cpu_peak * (float(cur_vcpu) / float(tgt_vcpu))
        notes.append(f"{how} CPU {cpu_peak:.1f}% on {cur_vcpu} vCPU projects to "
                     f"{projected:.1f}% on {target_type}'s {tgt_vcpu} vCPU")
        verdicts.append(projected <= ceiling_pct)

    # ── Memory ──
    mem_peak, mem_how = peak_utilisation(metrics, "mem")
    if mem_peak is None:
        p30 = ((metrics or {}).get("periods") or {}).get("30d") or {}
        mem_peak = p30.get("mem_used_pct")
        mem_how = "average" if mem_peak is not None else ""
    if mem_peak is None:
        notes.append("memory not reported (CloudWatch Agent absent)")
        verdicts.append(None)
    elif not cur_mem or not tgt_mem:
        notes.append("memory sizes unavailable")
        verdicts.append(None)
    else:
        projected = mem_peak * (float(cur_mem) / float(tgt_mem))
        notes.append(f"{mem_how} memory {mem_peak:.1f}% of {cur_mem} GiB projects "
                     f"to {projected:.1f}% of {target_type}'s {tgt_mem} GiB")
        verdicts.append(projected <= ceiling_pct)

    text = "; ".join(notes)
    if False in verdicts:
        return False, text + f" — exceeds the {ceiling_pct}% ceiling"
    if True in verdicts:
        # At least one axis verified as fitting and none exceeded.
        unverified = verdicts.count(None)
        suffix = (f" — fits within {ceiling_pct}%"
                  + (" (the other axis could not be verified)" if unverified else ""))
        return True, text + suffix
    return None, text + " — headroom could not be verified"


def _covered(ctx, service, instance_type):
    """Is this resource already covered by a Reserved Instance or Savings Plan?"""
    coverage = ctx.get("coverage")
    if not coverage:
        return False, ""
    return commitments.is_covered(coverage, service, ctx["region"], instance_type)


# ─── EC2 ─────────────────────────────────────────────────────────────────────

@rule("EC2 stopped instance", ["EC2"], phase=1)
def ec2_stopped(ctx, gated):
    r = ctx["resource"]
    if r.get("state") != "stopped":
        return None
    if gated:
        return True

    # A stopped instance is not billed for compute, so the saving is the cost
    # of the EBS volumes it still holds. Those are already priced from live
    # rates, so this is a measured figure rather than "see the EBS tab".
    volumes = ctx.get("volumes_by_instance", {}).get(r["id"], [])
    volume_cost = sum(prov.cost_of(v) or 0 for v in volumes)
    saving, basis = None, None
    if volumes and volume_cost:
        saving = volume_cost
        basis = Basis(DERIVED,
                      formula=(f"{len(volumes)} attached EBS volume(s) "
                               f"totalling ${volume_cost:,.2f}/mo"),
                      provider="live pricing")

    return finding(
        action="Instance is STOPPED — evaluate for termination",
        phase=1, risk="Low",
        saving=saving, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("State", "stopped"),
                     ("Attached EBS volumes", len(volumes) or r.get("ebs_volume_count")),
                     ("EBS cost", f"${volume_cost:,.2f}/mo" if volume_cost else None)),
        caveats=["Stopped instances incur no compute charge — the ongoing cost "
                 "is the attached EBS volumes and any Elastic IP.",
                 ("The saving shown is the measured cost of the attached volumes."
                  if saving else
                  "Attached volume costs could not be resolved, so no saving is claimed.")],
        validation_steps=["Confirm with the workload owner that it is not needed",
                          "Create an AMI before terminating",
                          "Delete the attached EBS volumes — they bill while stopped",
                          "Release any associated Elastic IP"])


@rule("EC2 rightsize", ["EC2"], phase=1,
      requires=("instance_type", "cost", "cpu_30d", "enough_datapoints"))
def ec2_rightsize(ctx, gated):
    r = ctx["resource"]
    if r.get("state") == "stopped":
        return None
    if gated:
        return True

    safe, reason = has_safe_utilization(
        ctx["metrics"], ctx["cpu_threshold"], ctx["mem_threshold"],
        ctx["min_datapoints"], ctx["peak_threshold"])
    p30 = (ctx["metrics"].get("periods") or {}).get("30d", {})
    if not safe:
        return finding(
            action=f"Cannot rightsize {r['instance_type']}: {reason}",
            phase=1, risk="N/A", saving=None,
            evidence=_ev(ctx, ("CPU 30d", p30.get("cpu_avg_pct")),
                         ("Memory 30d", p30.get("mem_used_pct")),
                         ("Spikes", len(ctx["metrics"].get("spikes") or []))),
            caveats=[reason])

    family, size = _split(r["instance_type"])
    smaller_size = RIGHTSIZE_MAP.get(size)
    if not smaller_size:
        return None
    target, generation_note = _best_target(family, smaller_size, ctx["region"])
    if pricing.instance_type_exists(target, ctx["region"]) is False:
        return finding(
            action=f"{r['instance_type']} is the smallest size in the {family} family",
            phase=1, risk="N/A", saving=None,
            caveats=[f"{target} is not offered in {ctx['region']}."],
            validation_steps=["Consider a different instance family"])

    # Headroom: the peak must still fit on the smaller instance.
    fits, headroom_note = _fits_peak(r["instance_type"], target, ctx["metrics"],
                                     ctx["peak_ceiling"], ctx["region"], "EC2")
    if fits is False:
        return finding(
            action=f"Do NOT rightsize {r['instance_type']} -> {target}: peak load would not fit",
            phase=1, risk="N/A", saving=None,
            evidence=_ev(ctx, ("CPU 30d avg", p30.get("cpu_avg_pct")),
                         ("CPU p95", p30.get("cpu_p95_pct")),
                         ("CPU peak", p30.get("cpu_max_pct"))),
            caveats=[headroom_note,
                     "The 30-day average suggests spare capacity, but the peak "
                     "does not fit the smaller instance."],
            validation_steps=["Consider a same-size Graviton move instead",
                              "Or reduce peak demand before resizing"])

    target_cost = pricing.monthly_cost_for_ec2(target, ctx["region"],
                                               r.get("platform", "Linux"))
    saving, basis = _target_saving(ctx["cost"], target_cost,
                                   f"{r['instance_type']} -> {target}")
    ram = p30.get("mem_used_pct")
    caveats = [f"CPU 30d average {p30.get('cpu_avg_pct')}% is below the "
               f"{ctx['cpu_threshold']}% threshold",
               f"No utilization spikes in the "
               f"{ctx['metrics'].get('spike_window_days', 90)}-day window"]
    if ram is not None:
        caveats.append(f"Memory 30d average {ram:.1f}%")
    else:
        # "Memory unverified" understates it when the resize halves RAM. State
        # the actual GB delta so the reader can judge whether the workload
        # survives it, rather than discovering the answer in production.
        current_gb = _memory_gb(r["instance_type"])
        target_gb = _memory_gb(target)
        delta = (f" — RAM drops {current_gb:g} GB to {target_gb:g} GB"
                 if current_gb and target_gb else "")
        caveats.append(
            f"Memory NOT measured (no CloudWatch Agent){delta}. "
            f"Verify the workload's memory footprint before resizing; CPU "
            f"alone cannot tell you whether it fits.")
    if generation_note:
        caveats.append(generation_note)
    if saving is None:
        caveats.append(f"{target} could not be priced, so no saving is claimed.")

    return finding(
        action=f"Rightsize {r['instance_type']} -> {target}",
        phase=1, risk="Low" if ram is not None else "Medium",
        saving=saving, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("CPU 30d", p30.get("cpu_avg_pct")),
                     ("Memory 30d", ram), ("Current cost", ctx["cost"]),
                     ("Target cost", target_cost)),
        caveats=caveats,
        validation_steps=[f"Load-test the application on {target} in staging",
                          "Monitor CPU, memory and disk for 72h after the change",
                          "Verify auto-scaling policies still behave correctly"])


@rule("EC2 Graviton migration", ["EC2"], phase=2,
      requires=("instance_type", "cost"))
def ec2_graviton(ctx, gated):
    r = ctx["resource"]
    if r.get("state") == "stopped":
        return None
    if gated:
        return True

    assessment = graviton_assess(r)
    if assessment["result"] == RESULT_ALREADY_GRAVITON:
        return None
    if assessment["result"] in (RESULT_WINDOWS, RESULT_BLOCKED):
        return finding(
            action=f"Graviton blocked: {assessment['result']}",
            phase=2, risk="N/A", saving=None,
            caveats=assessment["caveats"], blockers=assessment["blockers"],
            validation_steps=assessment["validation_steps"])

    # If a rightsize was recommended, Graviton must be priced from THAT
    # instance, not the current one. Rightsizing and a Graviton move both
    # change the instance class, so quoting each against the original size
    # counts the same dollars twice — a real report claimed a rightsize and a
    # Graviton saving on one database that no single path could deliver.
    base_type = ctx.get("rightsize_target") or r["instance_type"]
    base_cost = ctx.get("cost_after_rightsize") or ctx["cost"]
    compounded = base_type != r["instance_type"]

    family, size = _split(base_type)
    if ctx["graviton_families"] and family not in ctx["graviton_families"]:
        return None
    target_family = GRAVITON_MAP.get(family)
    if not target_family:
        return finding(
            action=f"No Graviton equivalent for {r['instance_type']}",
            phase=2, risk="N/A", saving=None,
            caveats=[f"Family '{family}' has no mapped Graviton equivalent."])

    target = f"{target_family}.{size}"
    if pricing.instance_type_exists(target, ctx["region"]) is False:
        return finding(
            action=f"Graviton equivalent {target} is not available in {ctx['region']}",
            phase=2, risk="N/A", saving=None,
            blockers=[f"{target} unavailable in {ctx['region']}"])

    target_cost = pricing.monthly_cost_for_ec2(target, ctx["region"],
                                               r.get("platform", "Linux"))
    saving, basis = _target_saving(base_cost, target_cost,
                                   f"{base_type} -> {target}")
    caveats = [f"Compatibility: {assessment['result']}"] + assessment["caveats"]
    if compounded:
        caveats.insert(0, f"Priced as an ADDITIONAL saving after rightsizing to "
                          f"{base_type}, so it does not double-count that finding.")
    if pricing.is_arm_instance_type(target):
        caveats.append(f"{target} confirmed ARM64 — an ARM64 AMI is required.")

    return finding(
        action=(f"Migrate to Graviton: {base_type} -> {target}"
                + (" (after rightsizing)" if compounded else "")),
        phase=2, risk=assessment["risk"],
        saving=saving, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("AMI architecture", r.get("ami_architecture")),
                     ("SSM managed", r.get("ssm_managed")),
                     ("Apps scanned", r.get("ssm_app_count")),
                     ("Current cost", ctx["cost"]), ("Target cost", target_cost)),
        caveats=caveats, blockers=assessment["blockers"],
        validation_steps=assessment["validation_steps"])


@rule("EC2 Reserved Instance", ["EC2"], phase=2,
      requires=("instance_type", "cost"))
def ec2_reserved(ctx, gated):
    r = ctx["resource"]
    # Applicability checks that need no gated input come first — reading
    # ctx["cost"] before the gate would crash on an unpriced resource.
    if r.get("state") == "stopped":
        return None
    if gated:
        return True
    if ctx["cost"] < ctx["min_ri_cost"]:
        return None

    # Never recommend buying a reservation for capacity the customer already
    # owns. Without this check the saving is counted twice: once by the
    # customer who bought the RI, once by us.
    # AWS's account-wide Savings Plan / RI recommendation already covers this
    # instance's commitment opportunity. Emitting a per-resource figure as well
    # counts the same dollars twice — it inflated a real report by $414.51.
    if ctx.get("aws_commitment_recs"):
        return finding(
            action=f"Commitment opportunity for {r['instance_type']} is covered by "
                   f"the account-wide recommendation",
            phase=2, risk="N/A", saving=None,
            evidence=["See the account-level Savings Plan / Reserved Instance "
                      "finding, which AWS computed across all usage"],
            caveats=["A per-resource figure is not shown here because it would "
                     "double-count the account-wide recommendation.",
                     "AWS's account-wide figure accounts for usage overlap "
                     "between instances; a per-resource sum cannot."])

    covered, why = _covered(ctx, "EC2", r["instance_type"])
    if covered:
        return finding(
            action=f"Already covered — no new Reserved Instance needed for {r['instance_type']}",
            phase=2, risk="N/A", saving=None,
            evidence=[why],
            caveats=["An existing commitment already applies to this instance.",
                     "Buying another reservation would not reduce spend."],
            validation_steps=["Review the Commitments tab for expiry dates"])

    ri_hourly = pricing.get_ec2_reserved_hourly(
        r["instance_type"], ctx["region"], r.get("platform", "Linux"))
    if not ri_hourly:
        return finding(
            action=f"Evaluate a 1-year Reserved Instance for {r['instance_type']}",
            phase=2, risk="Low", saving=None,
            caveats=["The published RI rate could not be resolved, so no saving "
                     "is claimed here.",
                     "Check the AWS console for current Reserved Instance pricing."])

    ri_monthly = ri_hourly * HOURS_PER_MONTH
    on_demand = pricing.monthly_cost_for_ec2(r["instance_type"], ctx["region"],
                                             r.get("platform", "Linux"))
    if not on_demand:
        return None
    discount = 1 - (ri_monthly / on_demand)
    # Applied to whatever the instance ends up as after a Graviton move, so the
    # two Phase 2 findings do not claim the same dollars twice.
    base = ctx.get("cost_after_graviton") or ctx["cost"]
    saving = base * discount
    basis = Basis(DERIVED,
                  formula=(f"${base:,.2f}/mo x {discount * 100:.1f}% RI discount "
                           f"(${ri_hourly:,.4f}/hr vs ${on_demand / HOURS_PER_MONTH:,.4f}/hr)"),
                  provider="live pricing")

    caveats = ["A Reserved Instance is a 1- or 3-year commitment — confirm the "
               "workload is long-lived",
               "Standard RIs cannot move between instance families"]
    if ctx.get("cost_after_graviton"):
        caveats.append("Calculated on the post-Graviton cost so it does not "
                       "double-count the Graviton finding.")

    return finding(
        action=f"Purchase a 1-year Reserved Instance for {r['instance_type']}",
        phase=2, risk="Low", saving=saving, saving_basis=basis,
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("On-demand", f"${on_demand:,.2f}/mo"),
                     ("Reserved", f"${ri_monthly:,.2f}/mo"),
                     ("Discount", f"{discount * 100:.1f}%")),
        caveats=caveats,
        validation_steps=["Confirm 12+ months of continuous running",
                          "Compare against Compute Savings Plans, which are more flexible"])


# ─── EBS ─────────────────────────────────────────────────────────────────────

@rule("EBS unattached volume", ["EBS"], phase=1, requires=("cost",))
def ebs_unattached(ctx, gated):
    r = ctx["resource"]
    if r.get("state") != "available":
        return None
    if gated:
        return True
    return finding(
        action="Delete unattached EBS volume",
        phase=1, risk="Low", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Full volume cost ${ctx['cost']:,.2f}/mo is eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("State", "available (not attached)"),
                     ("Size", f"{r.get('size_gb')} GB"),
                     ("Snapshots", r.get("snapshot_count"))),
        caveats=["The volume is not attached to any instance and bills in full."],
        validation_steps=["Snapshot it if the data may still be needed",
                          "Confirm with the owner, then delete"])


@rule("EBS gp2 to gp3", ["EBS"], phase=1, requires=("size_gb",))
def ebs_gp3(ctx, gated):
    r = ctx["resource"]
    if r.get("volume_type") != "gp2":
        return None
    if gated:
        return True

    region, size_gb = ctx["region"], float(r["size_gb"])
    gp2 = ap.ebs_gb_month(region, "gp2")
    gp3 = ap.ebs_gb_month(region, "gp3")
    if not gp2 or not gp3:
        return finding(
            action=f"Convert gp2 volume to gp3 ({size_gb:,.0f} GB)",
            phase=1, risk="Low", saving=None,
            caveats=["gp3 is cheaper per GB and includes 3000 baseline IOPS free.",
                     "The gp2/gp3 rates for this region could not be resolved, "
                     "so no saving is claimed."],
            validation_steps=["Modify the volume type in the EC2 console — the "
                              "change is online with no downtime"])

    saving = (gp2.amount - gp3.amount) * size_gb
    return finding(
        action=f"Convert gp2 volume to gp3 ({size_gb:,.0f} GB)",
        phase=1, risk="Low", saving=saving,
        saving_basis=Basis(DERIVED,
                           formula=(f"{size_gb:,.0f} GB x (${gp2.amount:,.4f} gp2 "
                                    f"- ${gp3.amount:,.4f} gp3)/GB-month"),
                           provider=gp3.source),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Volume type", "gp2"), ("Size", f"{size_gb:,.0f} GB"),
                     ("gp2 rate", f"${gp2.amount}/GB-mo"),
                     ("gp3 rate", f"${gp3.amount}/GB-mo")),
        caveats=["Conversion is online — no downtime and no snapshot required.",
                 "gp3 includes 3000 IOPS and 125 MB/s at no extra charge."],
        validation_steps=["Modify the volume type in the EC2 console or via CLI",
                          "Verify IOPS and throughput still meet the workload"])


# ─── Elastic IP ──────────────────────────────────────────────────────────────

@rule("Unattached Elastic IP", ["ElasticIP", "ElasticIPs"], phase=1,
      requires=("cost",))
def eip_unattached(ctx, gated):
    if not ctx["resource"].get("unattached"):
        return None
    if gated:
        return True
    return finding(
        action="Release unattached Elastic IP",
        phase=1, risk="Low", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Idle address charge ${ctx['cost']:,.2f}/mo is eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Association", "none")),
        caveats=["AWS bills every public IPv4 address that is not associated "
                 "with a running resource."],
        validation_steps=["Confirm the address is not referenced in DNS or an allow-list",
                          "Release the allocation"])


# ─── CloudWatch Logs ─────────────────────────────────────────────────────────

@rule("Log group retention", ["CWLogGroup", "CWLogGroups"], phase=1,
      requires=("cost",), priced=False, aggregate=True)
def logs_retention(ctx, gated):
    r = ctx["resource"]
    if r.get("retention_days"):
        return None
    if gated:
        return True
    # The saving depends on how much of the stored data predates the chosen
    # retention window, which we cannot measure from DescribeLogGroups. State
    # the current spend and let the operator choose, rather than assuming.
    return finding(
        action="Set a retention policy on this log group",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Retention", "Never expire"),
                     ("Stored", f"{r.get('stored_gb')} GB"),
                     ("Current cost", f"${ctx['cost']:,.2f}/mo")),
        caveats=["Retention is set to 'Never expire', so stored volume and cost "
                 "grow indefinitely.",
                 "The saving depends on the age distribution of the stored data, "
                 "which the CloudWatch Logs API does not expose — no figure is "
                 "claimed here."],
        validation_steps=["Agree a retention period with the owner (30/90/365 days)",
                          "Apply it with `aws logs put-retention-policy`",
                          "Re-run this report next month to measure the actual reduction"])


# ─── NAT Gateway ─────────────────────────────────────────────────────────────

@rule("Idle NAT Gateway", ["NATGateway"], phase=1,
      requires=("cost", "traffic_30d"))
def nat_idle(ctx, gated):
    if ctx["metrics"].get("total_gb_30d") not in (0, 0.0):
        return None
    if gated:
        return True
    return finding(
        action="NAT Gateway processed no traffic in 30 days — evaluate for removal",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Full gateway cost ${ctx['cost']:,.2f}/mo is eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Traffic 30d", "0 GB"),
                     ("Monthly cost", f"${ctx['cost']:,.2f}")),
        caveats=["A NAT Gateway bills hourly whether or not it carries traffic.",
                 "Confirm no private subnet depends on it before removing."],
        validation_steps=["Check route tables for subnets pointing at this gateway",
                          "Confirm no workload is merely idle rather than retired",
                          "Delete the gateway and release its Elastic IP"])


# ─── Lambda ──────────────────────────────────────────────────────────────────

@rule("Lambda unused", ["Lambda"], phase=1, requires=("invocations_30d",), priced=False)
def lambda_unused(ctx, gated):
    if ((ctx["metrics"].get("periods") or {}).get("30d") or {}).get("invocations_total"):
        return None
    if gated:
        return True
    return finding(
        action="Zero invocations in 30 days — evaluate for removal",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Invocations 30d", 0)),
        caveats=["The function may be driven by an infrequent trigger.",
                 "Lambda bills per invocation, so an idle function costs nothing "
                 "to keep — the benefit is reduced attack surface, not spend."],
        validation_steps=["Confirm with the team that it is no longer used",
                          "Check EventBridge rules referencing this function",
                          "Delete it to clean up its IAM role and log group"])


@rule("Lambda arm64", ["Lambda"], phase=2, requires=("invocations_30d",), priced=False)
def lambda_arm(ctx, gated):
    r = ctx["resource"]
    if r.get("architecture") != "x86_64":
        return None
    if not ((ctx["metrics"].get("periods") or {}).get("30d") or {}).get("invocations_total"):
        return None
    if gated:
        return True
    return finding(
        action="Switch Lambda architecture to arm64 (Graviton)",
        phase=2, risk="Low", saving=None,
        evidence=_ev(ctx, ("Architecture", "x86_64"),
                     ("Memory", f"{r.get('memory_mb')} MB"),
                     ("Invocations 30d",
                      ((ctx["metrics"].get("periods") or {}).get("30d") or {})
                      .get("invocations_total"))),
        caveats=["arm64 Lambda is billed at a lower rate per GB-second.",
                 "The saving depends on GB-seconds consumed, which requires "
                 "billing data to compute — no figure is claimed from inventory alone."],
        validation_steps=["Check for native binary dependencies in the package",
                          "Rebuild any layers for arm64",
                          "Deploy to a staging alias and compare duration and errors"])


# ─── RDS ─────────────────────────────────────────────────────────────────────

@rule("RDS rightsize", ["RDS"], phase=1,
      requires=("instance_type", "cost", "cpu_30d", "enough_datapoints"))
def rds_rightsize(ctx, gated):
    if gated:
        return True
    r = ctx["resource"]
    safe, reason = has_safe_utilization(
        ctx["metrics"], ctx["cpu_threshold"], ctx["mem_threshold"],
        ctx["min_datapoints"], ctx["peak_threshold"])
    p30 = (ctx["metrics"].get("periods") or {}).get("30d", {})
    if not safe:
        return finding(
            action=f"Cannot rightsize {r['instance_type']}: {reason}",
            phase=1, risk="N/A", saving=None, caveats=[reason])

    # Performance Insights: CPU average alone is a weak basis for RDS. A
    # database at low CPU but high IO-wait is not over-provisioned on CPU, and
    # downsizing worsens the actual bottleneck.
    pi = r.get("performance_insights") or {}
    if pi.get("pi_data_available") and pi.get("bottleneck") in ("IO-bound", "Lock-contention"):
        return finding(
            action=f"Do NOT rightsize {r['instance_type']}: workload is {pi['bottleneck']}, not CPU-bound",
            phase=1, risk="N/A", saving=None,
            evidence=_ev(ctx, ("CPU 30d", p30.get("cpu_avg_pct")),
                         ("PI bottleneck", pi.get("bottleneck")),
                         ("IO wait", f"{pi.get('io_wait_pct')}%"),
                         ("Lock wait", f"{pi.get('lock_wait_pct')}%"),
                         ("Top waits", ", ".join(pi.get("top_wait_events", [])[:3]))),
            caveats=[f"Performance Insights shows the database is {pi['bottleneck']}, "
                     f"so low CPU does not mean spare capacity.",
                     "Reducing the instance class would reduce IO throughput and "
                     "memory, making the real bottleneck worse."],
            validation_steps=["Address the IO or lock contention first",
                              "Re-evaluate rightsizing once the bottleneck shifts to CPU"])

    family, size = _split(r["instance_type"].replace("db.", ""))
    smaller = RIGHTSIZE_MAP.get(size)
    if not smaller:
        return None
    target = f"db.{family}.{smaller}"
    if pricing.db_instance_type_exists(target, ctx["region"], r.get("engine")) is False:
        return finding(
            action=f"{r['instance_type']} is the smallest class in the db.{family} family",
            phase=1, risk="N/A", saving=None,
            caveats=[f"{target} is not an RDS instance class.",
                     "Consider a burstable db.t4g class if the workload allows."])

    fits, headroom = _fits_peak(r["instance_type"], target, ctx["metrics"],
                                ctx["peak_ceiling"], ctx["region"], "RDS")
    if fits is False:
        return finding(
            action=f"Do NOT rightsize {r['instance_type']} -> {target}: peak load would not fit",
            phase=1, risk="N/A", saving=None,
            evidence=_ev(ctx, ("CPU 30d avg", p30.get("cpu_avg_pct")),
                         ("CPU p95", p30.get("cpu_p95_pct")),
                         ("CPU peak", p30.get("cpu_max_pct"))),
            caveats=[headroom,
                     "The average suggests spare capacity, but the peak does not fit."],
            validation_steps=["Consider a same-size Graviton move instead"])

    target_cost = pricing.monthly_cost_for_rds(
        target, r.get("engine", ""), ctx["region"], r.get("multi_az", False))
    saving, basis = _target_saving(ctx["cost"], target_cost,
                                   f"{r['instance_type']} -> {target}")
    free_mem_gb = (p30.get("freeable_memory_bytes") or 0) / (1024 ** 3)
    return finding(
        action=f"Rightsize {r['instance_type']} -> {target}",
        phase=1, risk="Low" if r.get("multi_az") else "Medium",
        saving=saving, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("CPU 30d", p30.get("cpu_avg_pct")),
                     ("Freeable memory 30d", f"{free_mem_gb:,.1f} GB"),
                     ("Current cost", ctx["cost"]), ("Target cost", target_cost)),
        caveats=["Multi-AZ: failover-based, ~60s interruption" if r.get("multi_az")
                 else "Single-AZ: expect 5-15 minutes of downtime",
                 "Verify the connection pool tolerates a brief reconnect"],
        validation_steps=["Schedule during a low-traffic maintenance window",
                          "Test connectivity and query performance afterwards",
                          "Rightsize read replicas to match"])


@rule("RDS Graviton migration", ["RDS"], phase=2,
      requires=("instance_type", "cost"))
def rds_graviton(ctx, gated):
    r = ctx["resource"]
    ok, reason = rds_graviton_compatible(r.get("engine", ""), r.get("engine_version", ""))
    if not ok:
        if gated:
            return True
        return finding(action=f"Graviton not available: {reason}", phase=2,
                       risk="N/A", saving=None, blockers=[reason])
    base_type = ctx.get("rightsize_target") or r["instance_type"]
    base_cost = ctx.get("cost_after_rightsize") or ctx["cost"]
    compounded = base_type != r["instance_type"]
    db_family = ".".join(str(base_type).split(".")[:2])
    target_family = RDS_GRAVITON_MAP.get(db_family)
    if not target_family:
        return None
    if gated:
        return True

    target = f"{target_family}.{str(base_type).split('.')[-1]}"
    target_cost = pricing.monthly_cost_for_rds(
        target, r.get("engine", ""), ctx["region"], r.get("multi_az", False))
    saving, basis = _target_saving(base_cost, target_cost,
                                   f"{base_type} -> {target}")
    return finding(
        action=(f"Migrate RDS to Graviton: {base_type} -> {target}"
                + (" (after rightsizing)" if compounded else "")),
        phase=2, risk="Low", saving=saving, saving_basis=basis,
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Engine", f"{r.get('engine')} {r.get('engine_version')}"),
                     ("Current cost", ctx["cost"]), ("Target cost", target_cost)),
        caveats=["RDS is managed — no application recompilation is required.",
                 "AWS validates engine compatibility before allowing the change.",
                 "Multi-AZ: failover-based, ~60s" if r.get("multi_az")
                 else "Single-AZ: ~5-15 minutes of downtime"],
        validation_steps=["Modify the instance class in the RDS console",
                          "Compare query performance with Performance Insights"])


@rule("RDS over-allocated storage", ["RDS"], phase=2,
      requires=("storage_gb", "free_storage_30d"))
def rds_storage(ctx, gated):
    r = ctx["resource"]
    storage_gb = float(r["storage_gb"])
    free_gb = (((ctx["metrics"].get("periods") or {}).get("30d") or {})
               .get("free_storage_bytes") or 0) / (1024 ** 3)
    if free_gb <= storage_gb * 0.5:
        return None
    if gated:
        return True

    reclaim = int(storage_gb * 0.5)
    price = ap.rds_storage_gb_month(
        ctx["region"], (r.get("storage_type") or "gp3").lower(), r.get("multi_az", False))
    if not price:
        return finding(
            action=f"Reduce allocated storage: {storage_gb:,.0f}GB -> ~{reclaim}GB",
            phase=2, risk="Medium", saving=None,
            caveats=[f"{free_gb:,.0f}GB of {storage_gb:,.0f}GB is free.",
                     "The RDS storage rate for this region could not be resolved."])

    return finding(
        action=f"Reduce allocated storage: {storage_gb:,.0f}GB -> ~{reclaim}GB",
        phase=2, risk="Medium", saving=price.amount * reclaim,
        saving_basis=Basis(DERIVED,
                           formula=f"{reclaim} GB x ${price.amount:,.4f}/GB-month",
                           provider=price.source),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Allocated", f"{storage_gb:,.0f} GB"),
                     ("Free 30d", f"{free_gb:,.0f} GB"),
                     ("Rate", f"${price.amount}/GB-mo")),
        caveats=["RDS cannot shrink storage in place — this needs snapshot and restore.",
                 "Storage autoscaling must be disabled first."],
        validation_steps=["Snapshot the database",
                          "Restore to a new instance with smaller storage",
                          "Validate data integrity, then repoint the endpoint"],
        blockers=["No in-place storage reduction — snapshot + restore required"])


@rule("RDS Reserved Instance", ["RDS"], phase=2,
      requires=("instance_type", "cost"))
def rds_reserved(ctx, gated):
    r = ctx["resource"]
    if gated:
        return True
    if ctx["cost"] < ctx["min_ri_cost"]:
        return None

    # AWS's account-wide Savings Plan / RI recommendation already covers this
    # instance's commitment opportunity. Emitting a per-resource figure as well
    # counts the same dollars twice — it inflated a real report by $414.51.
    if ctx.get("aws_commitment_recs"):
        return finding(
            action=f"Commitment opportunity for {r['instance_type']} is covered by "
                   f"the account-wide recommendation",
            phase=2, risk="N/A", saving=None,
            evidence=["See the account-level Savings Plan / Reserved Instance "
                      "finding, which AWS computed across all usage"],
            caveats=["A per-resource figure is not shown here because it would "
                     "double-count the account-wide recommendation.",
                     "AWS's account-wide figure accounts for usage overlap "
                     "between instances; a per-resource sum cannot."])

    covered, why = _covered(ctx, "RDS", r["instance_type"])
    if covered:
        return finding(
            action=f"Already covered — no new Reserved DB Instance needed for {r['instance_type']}",
            phase=2, risk="N/A", saving=None,
            evidence=[why],
            caveats=["An existing reservation already applies to this database."],
            validation_steps=["Review the Commitments tab for expiry dates"])

    ri_hourly = pricing.get_rds_reserved_hourly(
        r["instance_type"], r.get("engine", ""), ctx["region"], r.get("multi_az", False))
    on_demand = pricing.monthly_cost_for_rds(
        r["instance_type"], r.get("engine", ""), ctx["region"], r.get("multi_az", False))
    if not ri_hourly or not on_demand:
        return finding(
            action=f"Evaluate a 1-year Reserved DB Instance for {r['instance_type']}",
            phase=2, risk="Low", saving=None,
            caveats=["The published Reserved DB Instance rate could not be "
                     "resolved, so no saving is claimed."])
    discount = 1 - ((ri_hourly * HOURS_PER_MONTH) / on_demand)
    base = ctx.get("cost_after_graviton") or ctx["cost"]
    return finding(
        action=f"Purchase a 1-year Reserved DB Instance for {r['instance_type']}",
        phase=2, risk="Low", saving=base * discount,
        saving_basis=Basis(DERIVED,
                           formula=f"${base:,.2f}/mo x {discount * 100:.1f}% RI discount",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Discount", f"{discount * 100:.1f}%"),
                     ("RI rate", f"${ri_hourly:,.4f}/hr")),
        caveats=["Reserved DB Instances commit to an instance class.",
                 "Multi-AZ reservations are priced separately from Single-AZ."],
        validation_steps=["Confirm 12+ months of continuous running"])


@rule("RDS extended support charge", ["RDS"], phase=1,
      requires=("instance_type", "session"))
def rds_extended_support(ctx, gated):
    r = ctx["resource"]
    engine, version = r.get("engine", ""), r.get("engine_version", "")
    if not engine or not version:
        return None
    info = check_rds_version(engine, version, ctx["session"], ctx["region"])
    if info["status"] not in ("extended_support", "expiring_soon", "end_of_life"):
        return None
    if gated:
        return True

    # Extended support is billed per vCPU-hour and starts automatically at end
    # of standard support, so it is a real recurring charge — not a projection.
    rate = ap.rds_extended_support_vcpu_hour(ctx["region"], engine, version)
    vcpu = pricing.get_rds_vcpu(r["instance_type"])
    saving, basis = None, None
    if rate and vcpu:
        monthly = rate.amount * vcpu * HOURS_PER_MONTH
        if r.get("multi_az"):
            monthly *= 2
        saving = monthly
        basis = Basis(DERIVED,
                      formula=(f"${rate.amount:,.4f}/vCPU-hour x {vcpu} vCPU x "
                               f"{HOURS_PER_MONTH} hr"
                               + (" x 2 (Multi-AZ)" if r.get("multi_az") else "")),
                      provider=rate.source)

    caveats = [info.get("warning", "")]
    if saving is None:
        caveats.append("AWS has not yet published an extended-support rate for "
                       "this engine version, so no charge is quantified here.")
    return finding(
        action=f"Upgrade {engine} {version} to avoid extended support charges",
        phase=1, risk=info.get("urgency", "Medium"),
        saving=saving, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Engine", f"{engine} {version}"),
                     ("Support status", info["status"]),
                     ("vCPU", vcpu), ("Source", info.get("source"))),
        caveats=[c for c in caveats if c],
        validation_steps=["Plan a major-version upgrade in a maintenance window",
                          "Test the application against the target version first",
                          "Upgrade a read replica or restored snapshot to rehearse"])


# ─── EKS ─────────────────────────────────────────────────────────────────────

@rule("EKS version support", ["EKS"], phase=1, requires=("session",))
def eks_version(ctx, gated):
    r = ctx["resource"]
    if not r.get("k8s_version"):
        return None
    if gated:
        return True

    extended = ap.eks_extended_support_hourly(ctx["region"])
    info = check_eks_version(r["k8s_version"], ctx["session"], ctx["region"],
                             extended.amount if extended else None)
    if info["status"] not in ("extended_support", "expiring_soon", "end_of_life"):
        return None

    cost_mo = info.get("extended_cost_mo")
    basis = None
    if cost_mo and extended:
        basis = Basis(DERIVED,
                      formula=(f"${extended.amount:,.4f}/hr extended support "
                               f"x {HOURS_PER_MONTH} hr"),
                      provider=extended.source)
    return finding(
        action=f"Upgrade EKS cluster from {r['k8s_version']} to a supported version",
        phase=1, risk=info.get("urgency", "Medium"),
        saving=cost_mo, saving_basis=basis, cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Cluster version", r["k8s_version"]),
                     ("Support status", info["status"]),
                     ("Source", info.get("source"))),
        caveats=[info.get("warning", ""),
                 "EKS upgrades must step through one minor version at a time.",
                 "Node groups upgrade separately from the control plane."],
        validation_steps=["Review the Kubernetes deprecation guide for your version",
                          "Test the upgrade in a non-production cluster",
                          "Upgrade the control plane one minor version at a time",
                          "Update managed node groups, then verify add-ons"],
        blockers=["Minor versions cannot be skipped",
                  "Deprecated Kubernetes APIs must be migrated first"])


# ─── ElastiCache ─────────────────────────────────────────────────────────────

@rule("ElastiCache rightsize", ["ElastiCache"], phase=1,
      requires=("instance_type", "cost", "cpu_30d", "enough_datapoints"))
def elasticache_rightsize(ctx, gated):
    if gated:
        return True
    r = ctx["resource"]
    safe, reason = has_safe_utilization(
        ctx["metrics"], ctx["cpu_threshold"], ctx["mem_threshold"],
        ctx["min_datapoints"], ctx["peak_threshold"])
    if safe and ctx["metrics"].get("has_evictions"):
        safe = False
        reason = (f"{ctx['metrics'].get('evictions_30d', 0):,.0f} evictions in 30 "
                  f"days — the cache is memory-constrained")
    if not safe:
        return finding(action=f"Cannot rightsize {r['instance_type']}: {reason}",
                       phase=1, risk="N/A", saving=None, caveats=[reason])

    family, size = _split(str(r["instance_type"]).replace("cache.", ""))
    smaller = RIGHTSIZE_MAP.get(size)
    if not smaller:
        return None
    target = f"cache.{family}.{smaller}"
    if pricing.cache_instance_type_exists(target) is False:
        return None

    fits, headroom = _fits_peak(r["instance_type"], target, ctx["metrics"],
                                ctx["peak_ceiling"], ctx["region"], "ElastiCache")
    if fits is False:
        return finding(
            action=f"Do NOT rightsize {r['instance_type']} -> {target}: peak load would not fit",
            phase=1, risk="N/A", saving=None,
            caveats=[headroom,
                     "Cache memory pressure causes evictions, so a tight fit is "
                     "worse here than on a general compute node."],
            validation_steps=["Review eviction and hit-rate trends before resizing"])

    nodes = r.get("num_nodes") or 1
    hourly = pricing.get_cache_hourly_price(target, ctx["region"], r.get("engine", "redis"))
    target_cost = hourly * HOURS_PER_MONTH * nodes if hourly else None
    saving, basis = _target_saving(ctx["cost"], target_cost,
                                   f"{r['instance_type']} -> {target}")
    return finding(
        action=f"Rightsize {r['instance_type']} -> {target}",
        phase=1, risk="Medium", saving=saving, saving_basis=basis,
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Nodes", nodes), ("Evictions 30d", 0),
                     ("Current cost", ctx["cost"]), ("Target cost", target_cost)),
        caveats=["No evictions in the 30-day window.",
                 "ElastiCache cannot resize in place — the cluster is replaced.",
                 "Expect a cache-miss spike during the transition."],
        validation_steps=["Schedule during the lowest-traffic window",
                          "Monitor hit rate for an hour afterwards"],
        blockers=["No in-place resize — cluster replacement required"])


# ─── Orchestrator ────────────────────────────────────────────────────────────

def _settings(config):
    recs = (config or {}).get("recommendations", {}) or {}
    p1 = recs.get("phase1", {}) or {}
    p2 = recs.get("phase2", {}) or {}
    metrics = (config or {}).get("metrics", {}) or {}
    return {
        "cpu_threshold": p1.get("cpu_max_avg", 40),
        # Peak gates. cpu_peak_max is the p95 ceiling above which downsizing is
        # refused; peak_ceiling is the projected utilisation the target must
        # stay under after the resize.
        "peak_threshold": p1.get("cpu_peak_max", 70),
        "peak_ceiling": p1.get("target_max_utilisation", 75),
        "mem_threshold": p1.get("memory_max_avg", 50),
        "min_ri_cost": p2.get("min_monthly_cost_for_ri", 200),
        "graviton_families": set(p2.get("graviton_eligible_families") or []),
        "min_datapoints": metrics.get("min_datapoints", 0) or 0,
    }


def _lifecycle_warnings(resource, session, region):
    warnings = []
    rtype = resource.get("type")
    if rtype == "RDS":
        info = check_rds_version(resource.get("engine", ""),
                                 resource.get("engine_version", ""),
                                 session, region)
        if info.get("warning"):
            warnings.append(info["warning"])
    elif rtype == "ElastiCache":
        info = check_elasticache_version(resource.get("engine", ""),
                                         resource.get("engine_version", ""))
        if info.get("warning"):
            warnings.append(info["warning"])
    return warnings


def generate_all_recommendations(resources, all_metrics, config, session=None,
                                 coverage=None, has_commitments=False,
                                 aws_commitment_recs=False):
    """
    Run the rule engine over every resource.

    Returns (resources, all_recs). Savings are reported per confidence tier so
    the headline figure never mixes measured savings with estimates.
    """
    settings = _settings(config)

    # Index EBS volumes by the instance they are attached to, so a stopped
    # instance can be costed from its real volumes rather than deferred.
    volumes_by_instance = {}
    for vol in resources.get("EBS", []) or []:
        attached = vol.get("attached_to")
        if attached:
            volumes_by_instance.setdefault(attached, []).append(vol)

    all_recs = {}
    totals = {CONFIRMED: 0.0, ESTIMATED: 0.0}
    unpriced_findings = 0

    for service, items in resources.items():
        for r in items:
            rid = r.get("id")
            if not rid:
                continue
            region = r.get("region") or "us-east-1"
            if region == "global":
                region = "us-east-1"

            ctx = {
                "resource": r,
                "metrics": all_metrics.get(rid) or {},
                "config": config,
                "session": session,
                "region": region,
                "cost": prov.cost_of(r),
                # A list-price baseline is provably wrong when the account holds
                # commitments, so such savings can never be Confirmed.
                "cost_is_actual": prov.is_actual_cost(r) and not has_commitments,
                "coverage": coverage,
                # When AWS supplies an account-wide commitment recommendation,
                # per-resource RI rules must stand down or the same saving is
                # counted twice.
                "aws_commitment_recs": aws_commitment_recs,
                "volumes_by_instance": volumes_by_instance,
                **settings,
            }

            # Three passes so overlapping actions compound instead of summing:
            #   1. rightsize            -> the instance you end up on
            #   2. Graviton, priced from that instance
            #   3. Reserved Instance, priced from the post-Graviton cost
            # Each pass narrows the base, so no two findings claim the same
            # dollars.
            findings = run_rules(ctx)

            rightsize = next((f for f in findings
                              if f["action"].startswith("Rightsize")
                              and f["saving_usd"]), None)
            if rightsize and ctx["cost"]:
                match = re.search(r"->\s*(\S+)", rightsize["action"])
                if match:
                    ctx["rightsize_target"] = match.group(1)
                    ctx["cost_after_rightsize"] = max(
                        0.0, ctx["cost"] - rightsize["saving_usd"])
                    findings = run_rules(ctx)

            graviton = next((f for f in findings
                             if "Graviton" in f["action"] and f["saving_usd"]), None)
            if graviton:
                base = ctx.get("cost_after_rightsize") or ctx["cost"]
                if base:
                    ctx["cost_after_graviton"] = max(0.0, base - graviton["saving_usd"])
                    findings = run_rules(ctx)

            recs = {
                "phase1": [f for f in findings if f["phase"] == 1],
                "phase2": [f for f in findings if f["phase"] == 2],
                "lifecycle_warnings": _lifecycle_warnings(r, session, region),
                "savings_phase1_usd": 0.0,
                "savings_phase2_usd": 0.0,
                "savings_by_confidence": {CONFIRMED: 0.0, ESTIMATED: 0.0},
                "unpriced_actions": 0,
            }

            for f in findings:
                saving = f["saving_usd"]
                if saving is None:
                    recs["unpriced_actions"] += 1
                    unpriced_findings += 1
                    continue
                key = "savings_phase1_usd" if f["phase"] == 1 else "savings_phase2_usd"
                recs[key] += saving
                if f["confidence"] in totals:
                    recs["savings_by_confidence"][f["confidence"]] += saving
                    totals[f["confidence"]] += saving

            recs["savings_phase1_usd"] = round(recs["savings_phase1_usd"], 2)
            recs["savings_phase2_usd"] = round(recs["savings_phase2_usd"], 2)
            r["recommendations"] = recs
            all_recs[rid] = recs

    summary = {
        "confirmed_savings_usd": round(totals[CONFIRMED], 2),
        "estimated_savings_usd": round(totals[ESTIMATED], 2),
        "unpriced_actions": unpriced_findings,
        "has_commitments": has_commitments,
    }
    return resources, all_recs, summary


# ─── Idle and waste rules ────────────────────────────────────────────────────
#
# These fire on signals the collectors already measure but nothing acted on.
# Every one rests on a measured fact (zero traffic, zero I/O, a deleted source
# volume), so no assumption is introduced — only evidence that was being
# thrown away.

@rule("Burstable instance out of CPU credits", ["EC2"], phase=1,
      requires=("instance_type",), priced=False)
def ec2_credit_starved(ctx, gated):
    r = ctx["resource"]
    family = _split(r.get("instance_type", ""))[0]
    if not family.startswith(("t2", "t3", "t4")):
        return None
    if not ctx["metrics"].get("credit_starved"):
        return None
    if gated:
        return True
    p30 = (ctx["metrics"].get("periods") or {}).get("30d", {})
    surplus = p30.get("cpu_surplus_charged")
    # This is a cost INCREASE warning, not a saving: surplus credits are billed
    # on top of the instance rate, and downsizing would make it worse.
    return finding(
        action=f"{r['instance_type']} is exhausting its CPU credits — do NOT downsize",
        phase=1, risk="High", saving=None,
        evidence=_ev(ctx, ("7d credit balance",
                           p30 and (ctx["metrics"].get("periods") or {})
                           .get("7d", {}).get("cpu_credit_balance")),
                     ("30d credit balance", p30.get("cpu_credit_balance")),
                     ("Surplus credits charged 30d", surplus)),
        caveats=["The credit balance is falling steeply, so the instance is "
                 "running on borrowed capacity.",
                 "Surplus credits are billed on top of the instance rate, so "
                 "this may already be costing more than the class suggests.",
                 "Downsizing would reduce the credit earn rate and make it worse."],
        validation_steps=["Compare against a fixed-performance family (m/c/r)",
                          "Or accept T Unlimited and budget for surplus charges",
                          "Check whether the workload is genuinely bursty"])


@rule("Attached EBS volume with no I/O", ["EBS"], phase=1, requires=("cost",))
def ebs_zero_io(ctx, gated):
    r = ctx["resource"]
    if r.get("state") == "available":
        return None                      # handled by the unattached rule
    if not ctx["metrics"].get("zero_io"):
        return None
    if gated:
        return True
    return finding(
        action="Attached EBS volume recorded no I/O in 30 days",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Full volume cost ${ctx['cost']:,.2f}/mo if removed",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Read ops 30d", 0), ("Write ops 30d", 0),
                     ("Attached to", r.get("attached_to")),
                     ("Size", f"{r.get('size_gb')} GB")),
        caveats=["Zero I/O over 30 days means nothing read or wrote to this volume.",
                 "It is still attached, so it must be detached before deletion.",
                 "A volume can be legitimately idle (cold standby, DR) — confirm first."],
        validation_steps=["Identify what the volume is mounted as on the instance",
                          "Confirm with the owner it is not a standby or archive",
                          "Snapshot, detach, then delete"])


@rule("Snapshot of a deleted volume", ["EBSSnapshot"], phase=1, requires=("cost",))
def snapshot_orphaned(ctx, gated):
    r = ctx["resource"]
    if not r.get("orphaned"):
        return None
    if gated:
        return True
    return finding(
        action="Snapshot's source volume no longer exists — review for deletion",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Snapshot storage ${ctx['cost']:,.2f}/mo if deleted",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Source volume", r.get("volume_id")),
                     ("Source volume exists", "no"),
                     ("Age", f"{r.get('age_days')} days"),
                     ("Size", f"{r.get('volume_size_gb')} GB")),
        caveats=["Nothing deletes snapshots automatically, so they accumulate "
                 "long after the volume they came from is gone.",
                 "The cost shown is an upper bound — AWS bills incremental "
                 "blocks and exposes no per-snapshot incremental size.",
                 "The snapshot may still be a deliberate archive or an AMI backing store."],
        validation_steps=["Check whether an AMI references this snapshot",
                          "Confirm the retention requirement with the owner",
                          "Consider the Archive tier instead of deleting"])


@rule("Load balancer with no requests", ["ELB", "ALB", "NLB", "ELBClassic"],
      phase=1, requires=("cost",))
def elb_idle(ctx, gated):
    requests = ((ctx["metrics"].get("periods") or {}).get("30d") or {}) \
        .get("request_count_30d")
    if requests is None or requests > 0:
        return None
    if gated:
        return True
    r = ctx["resource"]
    return finding(
        action="Load balancer served no requests in 30 days — evaluate for removal",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Load balancer hourly charge ${ctx['cost']:,.2f}/mo",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Requests 30d", 0),
                     ("Healthy targets", r.get("healthy_targets")),
                     ("Type", r.get("lb_type"))),
        caveats=["A load balancer bills hourly whether or not it serves traffic.",
                 "It may front a disaster-recovery or seasonal workload."],
        validation_steps=["Check DNS records pointing at this load balancer",
                          "Confirm with the owner it is not a standby",
                          "Delete the load balancer and its target groups"])


@rule("Database with no connections", ["RDS"], phase=1, requires=("cost",))
def rds_idle(ctx, gated):
    conns = ((ctx["metrics"].get("periods") or {}).get("30d") or {}) \
        .get("db_connections_avg")
    if conns is None or conns > 0:
        return None
    if gated:
        return True
    r = ctx["resource"]
    return finding(
        action="Database recorded no connections in 30 days — evaluate for removal",
        phase=1, risk="High", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Instance cost ${ctx['cost']:,.2f}/mo if retired",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Avg connections 30d", 0),
                     ("Engine", f"{r.get('engine')} {r.get('engine_version')}"),
                     ("Multi-AZ", r.get("multi_az"))),
        caveats=["Zero average connections over 30 days is strong evidence of "
                 "disuse, but not proof — a monthly batch job would not show here.",
                 "Risk is High because deleting a database is irreversible."],
        validation_steps=["Take a final snapshot and verify it restores",
                          "Confirm with the application owner",
                          "Stop the instance first and wait before deleting"])


@rule("S3 bucket without a lifecycle policy", ["S3"], phase=1, priced=False,
      aggregate=True)
def s3_no_lifecycle(ctx, gated):
    r = ctx["resource"]
    if r.get("lifecycle_rules_count"):
        return None
    if not (r.get("size_gb") or 0):
        return None
    if gated:
        return True
    # Saving depends on the age distribution of objects, which S3 does not
    # expose without an inventory report — so no figure is claimed.
    return finding(
        action="Bucket has no lifecycle policy — objects are never tiered or expired",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Lifecycle rules", 0),
                     ("Standard storage", f"{r.get('size_gb')} GB"),
                     ("Versioning", r.get("versioning")),
                     ("Current cost", f"${prov.cost_of(r) or 0:,.2f}/mo")),
        caveats=["Without a lifecycle rule every object stays in Standard "
                 "storage and is never expired.",
                 "The saving depends on how old the objects are, which S3 does "
                 "not report without an inventory — so no figure is claimed here.",
                 "Versioning without expiry means noncurrent versions bill forever."
                 if r.get("versioning") == "Enabled" else ""],
        validation_steps=["Enable S3 Storage Lens or an Inventory report to see age",
                          "Add a transition rule to Standard-IA or Glacier",
                          "Add a noncurrent-version expiry rule if versioning is on"])


# ─── Services that had no rules at all ───────────────────────────────────────

@rule("Transfer Family endpoint idle", ["TransferFamily"], phase=1,
      requires=("cost",))
def transfer_idle(ctx, gated):
    if ctx["metrics"].get("zero_activity") is not True:
        return None
    if gated:
        return True
    return finding(
        action="Transfer Family server moved no files in 30 days — evaluate for removal",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Endpoint hours ${ctx['cost']:,.2f}/mo eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Files in 30d", 0), ("Files out 30d", 0),
                     ("Users", ctx["resource"].get("user_count")),
                     ("Protocols", ctx["resource"].get("protocols"))),
        caveats=["A Transfer Family endpoint bills per hour whether or not it "
                 "moves data — it is one of the most expensive idle resources in AWS.",
                 "Partners may connect infrequently; confirm before deleting."],
        validation_steps=["Check CloudWatch logs for the last successful session",
                          "Confirm with partners that the endpoint is unused",
                          "Delete the server (users and keys are removed with it)"])


@rule("WAF Web ACL protecting nothing", ["WAF"], phase=1, requires=("cost",))
def waf_unassociated(ctx, gated):
    r = ctx["resource"]
    if (r.get("associations") or 0) != 0:
        return None
    if gated:
        return True
    return finding(
        action="Web ACL is not associated with any resource — evaluate for removal",
        phase=1, risk="Low", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Web ACL + rule charges ${ctx['cost']:,.2f}/mo eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Associated resources", 0),
                     ("Rules", r.get("rule_count")),
                     ("Capacity (WCU)", r.get("capacity_wcu"))),
        caveats=["A Web ACL bills monthly per ACL and per rule even when it is "
                 "attached to nothing, so it is protecting no traffic.",
                 "It may be staged ahead of a planned association."],
        validation_steps=["Confirm no ALB, CloudFront or API Gateway needs it",
                          "Delete the Web ACL and its rules"])


@rule("Secret never accessed", ["SecretsManager"], phase=1, requires=("cost",))
def secret_unused(ctx, gated):
    r = ctx["resource"]
    never = str(r.get("last_accessed", "")).lower() == "never"
    stale = (r.get("days_since_access") or 0) >= 180
    if not (never or stale):
        return None
    if gated:
        return True
    detail = "never accessed" if never else f"not accessed for {r.get('days_since_access')} days"
    return finding(
        action=f"Secret {detail} — evaluate for removal",
        phase=1, risk="Medium", saving=ctx["cost"],
        saving_basis=Basis(DERIVED,
                           formula=f"Secret charge ${ctx['cost']:,.2f}/mo eliminated",
                           provider="live pricing"),
        cost_is_actual=ctx["cost_is_actual"],
        evidence=_ev(ctx, ("Last accessed", r.get("last_accessed")),
                     ("Days since access", r.get("days_since_access")),
                     ("Rotation enabled", r.get("rotation_enabled"))),
        caveats=["AWS reports last-accessed date at day granularity; a secret "
                 "read once a year would still show as stale.",
                 "Deleting a secret in use breaks the consumer immediately."],
        validation_steps=["Search code and IaC for references to this secret name",
                          "Schedule deletion with a 30-day recovery window rather "
                          "than deleting outright"])


@rule("ECR repository without a lifecycle policy", ["ECR"], phase=1,
      priced=False, aggregate=True)
def ecr_no_lifecycle(ctx, gated):
    r = ctx["resource"]
    if r.get("lifecycle_policy") or not (r.get("image_count") or 0):
        return None
    if gated:
        return True
    return finding(
        action="Repository has no lifecycle policy — images accumulate indefinitely",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Images", r.get("image_count")),
                     ("Untagged images", r.get("untagged_images")),
                     ("Size", f"{r.get('size_gb')} GB"),
                     ("Current cost", f"${prov.cost_of(r) or 0:,.2f}/mo")),
        caveats=["Without a lifecycle policy every image build is retained forever.",
                 "The saving depends on how many images are obsolete, which "
                 "cannot be judged from the registry alone — no figure is claimed."],
        validation_steps=["Add a rule expiring untagged images after N days",
                          "Add a rule keeping only the last N tagged images"])


@rule("EFS without Infrequent Access lifecycle", ["EFS"], phase=1,
      priced=False, aggregate=True)
def efs_no_ia(ctx, gated):
    r = ctx["resource"]
    if r.get("ia_enabled") or not (r.get("size_gb") or 0):
        return None
    if gated:
        return True
    return finding(
        action="File system has no lifecycle policy — nothing moves to Infrequent Access",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Size", f"{r.get('size_gb')} GB"),
                     ("Throughput mode", r.get("throughput_mode")),
                     ("Current cost", f"${prov.cost_of(r) or 0:,.2f}/mo")),
        caveats=["EFS Infrequent Access storage is roughly an order of magnitude "
                 "cheaper per GB than Standard.",
                 "The saving depends on what share of the data is cold, which EFS "
                 "does not report — no figure is claimed."],
        validation_steps=["Enable a lifecycle policy (e.g. move to IA after 30 days)",
                          "Confirm the workload tolerates first-byte latency on IA"])


@rule("KMS key rotation disabled", ["KMS"], phase=2, priced=False, aggregate=True)
def kms_no_rotation(ctx, gated):
    r = ctx["resource"]
    if r.get("rotation_enabled") or r.get("key_state") != "Enabled":
        return None
    if gated:
        return True
    return finding(
        action="Customer-managed key has automatic rotation disabled",
        phase=2, risk="Medium", saving=None,
        evidence=_ev(ctx, ("Key state", r.get("key_state")),
                     ("Rotation enabled", "No"),
                     ("Aliases", r.get("aliases") or "none")),
        caveats=["This is a security and compliance finding, not a cost saving — "
                 "rotation does not change the monthly key charge.",
                 "Included because unrotated keys frequently indicate a key that "
                 "is no longer owned by anyone."],
        validation_steps=["Enable automatic annual rotation, or",
                          "Confirm the key is still required and schedule deletion if not"])


# ─── Account hygiene that limits this tool's own accuracy ────────────────────

@rule("CloudWatch Agent not installed", ["EC2"], phase=1,
      priced=False, aggregate=True)
def ec2_no_cwagent(ctx, gated):
    r = ctx["resource"]
    if r.get("state") == "stopped":
        return None
    if ctx["metrics"].get("cwagent_installed"):
        return None
    if not ctx["metrics"].get("periods"):
        return None
    if gated:
        return True
    # This is the single largest limit on rightsizing confidence, so it is
    # reported as a finding rather than buried in the Data Gaps tab.
    return finding(
        action="Instance publishes no memory metrics — rightsizing cannot be fully verified",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("CloudWatch Agent", "not detected"),
                     ("CPU 30d", ((ctx["metrics"].get("periods") or {})
                                  .get("30d") or {}).get("cpu_avg_pct"))),
        caveats=["Without the CloudWatch Agent, memory utilisation is invisible, "
                 "so any rightsizing here rests on CPU alone.",
                 "Memory exhaustion is the most common cause of a failed downsize."],
        validation_steps=["Install the CloudWatch Agent via SSM Quick Setup",
                          "Publish mem_used_percent to the CWAgent namespace",
                          "Re-run this report once 30 days of data exists"])


@rule("Resource has no cost-allocation tags", ["EC2", "RDS", "EBS", "S3", "ELB"],
      phase=1, priced=False, aggregate=True)
def untagged_resource(ctx, gated):
    r = ctx["resource"]
    tags = r.get("tags") or {}
    meaningful = {k: v for k, v in tags.items() if not k.startswith("aws:")}
    if meaningful:
        return None
    if gated:
        return True
    return finding(
        action="Resource carries no cost-allocation tags",
        phase=1, risk="Low", saving=None,
        evidence=_ev(ctx, ("Tags", "none"),
                     ("Current cost", f"${prov.cost_of(r) or 0:,.2f}/mo")),
        caveats=["Untagged spend cannot be attributed to a team, product or "
                 "environment, which blocks showback and makes ownership unclear.",
                 "Tagging changes no cost by itself — it makes the rest of the "
                 "spend attributable."],
        validation_steps=["Agree a minimum tag set (Owner, Environment, Project)",
                          "Apply tags, then activate them as cost-allocation tags "
                          "in the Billing console"])


@rule("EBS burst balance depleting", ["EBS"], phase=1, priced=False)
def ebs_burst_low(ctx, gated):
    balance = ((ctx["metrics"].get("periods") or {}).get("30d") or {}) \
        .get("burst_balance_avg")
    if balance is None or balance >= 40:
        return None
    if gated:
        return True
    r = ctx["resource"]
    return finding(
        action=f"gp2 volume burst balance averaging {balance:.0f}% — throughput is being throttled",
        phase=1, risk="Medium", saving=None,
        evidence=_ev(ctx, ("Burst balance 30d avg", f"{balance:.1f}%"),
                     ("Volume type", r.get("volume_type")),
                     ("Size", f"{r.get('size_gb')} GB")),
        caveats=["A depleted burst balance means the volume is running at its "
                 "baseline IOPS and the workload is being throttled.",
                 "Moving to gp3 provides 3000 baseline IOPS with no burst "
                 "mechanic — often cheaper AND faster.",
                 "This is a performance finding; the gp2 to gp3 conversion rule "
                 "carries the cost figure."],
        validation_steps=["Convert to gp3 and set IOPS to match the observed need",
                          "Or increase the gp2 volume size to raise baseline IOPS"])


# ─── Account-level commitment findings ───────────────────────────────────────

def commitment_findings(purchase_data, has_existing_commitments=False, risk=None):
    """
    Turn AWS's own purchase recommendations into findings.

    These are account-wide rather than per-resource, so they bypass the rule
    engine — but they follow the same evidence rules. The saving is AWS's own
    figure, computed from the account's real billing history, so it is MEASURED
    and therefore Confirmed rather than Estimated.
    """
    from analysis.provenance import MEASURED
    out = []
    if not purchase_data:
        return out

    plan = purchase_data.get("best_savings_plan")
    ec2_ri = purchase_data.get("ec2_reservation")
    route = purchase_data.get("compute_best_route")

    if plan and route == "Savings Plan":
        term = plan["term"].replace("_", " ").title()
        out.append(finding(
            action=(f"Purchase a {term} Compute Savings Plan at "
                    f"${plan['hourly_commitment']:.2f}/hour"),
            # A binding multi-year spend commitment is not low risk just because
            # the arithmetic is certain. Risk here is about the DECISION.
            phase=2, risk=("High" if risk and risk.get("prefer_shorter_term")
                           else "Medium" if risk and risk.get("warnings")
                           else "Low"),
            saving=plan["monthly_savings"],
            saving_basis=Basis(MEASURED,
                               formula=(f"AWS estimate from {plan['lookback_days']} "
                                        f"of billing history: {plan['savings_pct']:.1f}% "
                                        f"off ${plan['current_on_demand']:,.2f} on-demand"),
                               provider=plan["source"]),
            cost_is_actual=True,
            evidence=[f"Hourly commitment: ${plan['hourly_commitment']:.4f}",
                      f"Estimated ROI: {plan['roi_pct']:.1f}%",
                      f"Current on-demand spend in scope: ${plan['current_on_demand']:,.2f}",
                      f"Source: {plan['source']}"],
            caveats=["Computed by AWS from this account's actual usage, not from "
                     "list prices — the arithmetic is sound.",
                     # The saving is reliable; the DECISION is not the same
                     # thing. AWS is silent on whether N days of history
                     # predicts N years, and on the fact that this same report
                     # may recommend removing the capacity being committed to.
                     (f"Total exposure: ${risk['exposure_usd']:,.2f} over "
                      f"{risk['term_years']} year(s), non-refundable."
                      if risk else ""),
                     f"A {term} commitment is binding; the saving assumes usage "
                     f"stays at or above the committed level.",
                     *(risk.get("warnings", []) if risk else []),
                     ("Commit LAST. Eliminate idle resources and rightsize "
                      "first, then commit to what remains."
                      if risk and risk.get("conflict_count") else ""),
                     (f"Consider the one-year term instead — same discount "
                      f"structure at a third of the exposure."
                      if risk and risk.get("prefer_shorter_term") else ""),
                     purchase_data.get("overlap_note", "")],
            validation_steps=["Review in Billing -> Savings Plans -> Recommendations",
                              "Start with a commitment below the recommendation if "
                              "usage may fall",
                              "Compare 1-year and 3-year options for cash-flow fit"]))

    if ec2_ri and route == "Reserved Instances":
        out.append(finding(
            action=f"Purchase EC2 Reserved Instances ({len(ec2_ri['details'])} types recommended)",
            phase=2, risk="Low",
            saving=ec2_ri["monthly_savings"],
            saving_basis=Basis(MEASURED,
                               formula=f"AWS estimate: {ec2_ri['savings_pct']:.1f}% off on-demand",
                               provider=ec2_ri["source"]),
            cost_is_actual=True,
            evidence=[f"{d['instance_type']} x{d['quantity']} in {d['region']}: "
                      f"${d['monthly_savings']:,.2f}/mo (util {d['utilisation_pct']:.0f}%)"
                      for d in ec2_ri["details"][:6]],
            caveats=["Computed by AWS from actual usage.",
                     purchase_data.get("overlap_note", "")],
            validation_steps=["Review in Billing -> Reserved Instances -> Recommendations"]))

    for ri in purchase_data.get("other_reservations", []):
        name = ri["service"].replace("Amazon ", "").replace(" Service", "")
        out.append(finding(
            action=f"Purchase {name} Reserved Instances",
            phase=2, risk="Low",
            saving=ri["monthly_savings"],
            saving_basis=Basis(MEASURED,
                               formula=f"AWS estimate: {ri['savings_pct']:.1f}% off on-demand",
                               provider=ri["source"]),
            cost_is_actual=True,
            evidence=[f"{d['instance_type']} x{d['quantity']} in {d['region']}: "
                      f"${d['monthly_savings']:,.2f}/mo (util {d['utilisation_pct']:.0f}%)"
                      for d in ri["details"][:6]],
            caveats=["Computed by AWS from actual usage.",
                     "Does not overlap a Compute Savings Plan, so it is additive."],
            validation_steps=[f"Review in Billing -> Reserved Instances -> Recommendations"]))

    for f in out:
        f["rule"] = "AWS purchase recommendation"
        f["aggregate"] = False
    return out
