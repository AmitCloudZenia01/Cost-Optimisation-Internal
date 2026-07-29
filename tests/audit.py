"""
Static audit for the bug classes that have actually occurred in this codebase.

Every check here exists because the corresponding bug shipped at least once:

  - metric key mismatch      spikes vs cpu_spikes; flat vs periods
  - column/row width drift   sheet headers out of step with row builders
  - unknown requirement      a rule requiring an input the engine cannot check
  - non-scalar to gspread    cost_basis dict blanked the EBSSnapshot tab
  - duplicate dict keys      METRICS_CONFIG silently shadowing service configs
  - unsafe None arithmetic   comparing a possibly-None cost against a threshold

Run:  python3 tests/audit.py
"""

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBLEMS = []
CHECKS = []


def ok(name, detail=""):
    CHECKS.append(f"{name}{(' — ' + detail) if detail else ''}")


def bad(name, detail=""):
    PROBLEMS.append(f"{name}{(' — ' + detail) if detail else ''}")


# ── 1. Metric keys used by sheets must be produced by the metric engine ──────

def check_metric_keys():
    from collectors.metrics_auto import METRICS_CONFIG

    produced = set()
    for cfg in METRICS_CONFIG.values():
        for _, _, friendly in cfg.get("metrics", []):
            produced.add(friendly)
        for peak in cfg.get("peak_metrics", []):
            produced.update(peak["stats"].values())
        for extra in cfg.get("extra_metrics", []):
            produced.add(extra["key"])
    # derived flags attached in _derive()
    produced |= {"total_gb_30d", "zero_traffic", "zero_activity", "zero_io",
                 "has_evictions", "evictions_30d", "credit_starved",
                 "cwagent_installed", "data_cost_30d", "datapoints",
                 "spikes", "spike_metric", "spike_window_days",
                 "monthly_fixed_cost", "data_processing_cost_30d",
                 "estimated_monthly_total"}

    src = (ROOT / "sheets" / "service_pages.py").read_text()
    used = set(re.findall(r'_p\(m,\s*\d+,\s*["\']([a-z0-9_]+)["\']\)', src))
    used |= set(re.findall(r'p30\.get\(["\']([a-z0-9_]+)["\']', src))
    used |= set(re.findall(r'm\.get\(["\']([a-z0-9_]+)["\']', src))

    # Structural containers, not metrics
    used -= {"periods", "spikes", "spike_metric", "datapoints"}
    unknown = sorted(used - produced)
    if unknown:
        bad("Sheet reads metric keys the engine never produces", ", ".join(unknown))
    else:
        ok(f"All {len(used)} metric keys used by sheets are produced")


# ── 2. Column count must equal row width, for every service ─────────────────

def check_column_parity():
    src = (ROOT / "sheets" / "service_pages.py").read_text()
    tree = ast.parse(src)
    cols = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SERVICE_COLUMNS":
            for k, v in zip(node.value.keys, node.value.values):
                cols[k.value] = len(v.elts)
    # aliases assigned after the dict literal
    for m in re.finditer(r'SERVICE_COLUMNS\["(\w+)"\]\s*=\s*SERVICE_COLUMNS\["(\w+)"\]', src):
        cols[m.group(1)] = cols.get(m.group(2))
    for m in re.finditer(r'SERVICE_COLUMNS\["(\w+)"\]\s*=\s*\[(.*?)\n\]', src, re.S):
        cols[m.group(1)] = len([x for x in m.group(2).split(",") if x.strip()])

    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    mapping = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ROW_BUILDERS":
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Name):
                    mapping[k.value] = v.id
                elif isinstance(v, ast.Lambda):
                    for sub in ast.walk(v):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                            mapping[k.value] = sub.func.id

    bad_ones = []
    checked = 0
    for svc, fn in mapping.items():
        if svc not in cols or fn not in funcs:
            continue
        widths = []
        for n in ast.walk(funcs[fn]):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "append" and n.args
                    and isinstance(n.args[0], ast.List)):
                widths.append(len(n.args[0].elts))
            if isinstance(n, ast.ListComp) and isinstance(n.elt, ast.List):
                widths.append(len(n.elt.elts))
        if widths:
            checked += 1
            if not all(w == cols[svc] for w in widths):
                bad_ones.append(f"{svc}: cols={cols[svc]} rows={widths}")
    if bad_ones:
        bad("Column/row width mismatch", "; ".join(bad_ones))
    else:
        ok(f"Column parity holds for all {checked} service tabs")


# ── 3. Every rule requirement must be resolvable by the engine ──────────────

def check_rule_requirements():
    from analysis import rules, recommender  # noqa: F401
    unknown = []
    for spec in rules.RULES:
        for req in spec["requires"]:
            if req not in rules.REQUIREMENTS:
                unknown.append(f"{spec['name']} -> {req}")
    if unknown:
        bad("Rule requires an unknown input", "; ".join(unknown))
    else:
        ok(f"All requirements across {len(rules.RULES)} rules are resolvable")


# ── 4. Nothing non-scalar may reach gspread ────────────────────────────────

def check_no_structured_values_in_sheets():
    from sheets.service_pages import build_generic_rows, ROW_BUILDERS, SERVICE_COLUMNS
    import inspect

    probe = {
        "type": "X", "id": "a", "name": "n", "region": "ap-south-1",
        "monthly_cost_usd": 1.5,
        "cost_basis": {"source": "derived", "description": "d"},
        "tags": {"Project": "p"}, "recommendations": {"phase1": [], "phase2": []},
        "some_list": ["a", "b"], "nested": {"k": "v"},
    }
    offenders = []
    _, rows = build_generic_rows([dict(probe)], {})
    for v in (rows[0] if rows else []):
        if isinstance(v, (dict, list, set, tuple)):
            offenders.append(f"generic:{type(v).__name__}")

    for svc, builder in ROW_BUILDERS.items():
        r = dict(probe, type=svc)
        try:
            n = len(inspect.signature(builder).parameters)
            out = builder([r], {}) if n >= 2 else builder([r])
        except Exception:
            continue          # exercised elsewhere; shape errors caught by parity
        for row in out or []:
            for v in row:
                if isinstance(v, (dict, list, set, tuple)):
                    offenders.append(f"{svc}:{type(v).__name__}")
    if offenders:
        bad("Non-scalar value would be sent to gspread", ", ".join(sorted(set(offenders))))
    else:
        ok("No builder emits a dict/list into a sheet cell")


# ── 5. Duplicate dict keys silently shadow earlier entries ─────────────────

def check_duplicate_keys():
    dupes = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                seen = {k for k in keys if keys.count(k) > 1}
                if seen:
                    dupes.append(f"{path.relative_to(ROOT)}:{node.lineno} {sorted(seen)}")
    if dupes:
        bad("Duplicate dict keys", "; ".join(dupes))
    else:
        ok("No duplicate dict keys anywhere")


# ── 6. Comparisons against a possibly-None cost ────────────────────────────

def check_none_comparisons():
    risky = []
    for path in (ROOT / "analysis").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            # ctx["cost"] may be None until the "cost" requirement is checked
            if re.search(r'ctx\["cost"\]\s*[<>]', line):
                risky.append(f"{path.name}:{lineno}")
    # Each of these must sit AFTER `if gated: return True`, which the engine
    # only reaches once the cost requirement passed.
    if risky:
        src = (ROOT / "analysis" / "recommender.py").read_text().splitlines()
        unguarded = []
        for entry in risky:
            lineno = int(entry.split(":")[1])
            window = "\n".join(src[max(0, lineno - 12):lineno])
            if "if gated:" not in window:
                unguarded.append(entry)
        if unguarded:
            bad("Cost compared before the gate could reject None", ", ".join(unguarded))
        else:
            ok(f"All {len(risky)} cost comparisons sit after the gate")
    else:
        ok("No direct cost comparisons found")


# ── 7. Every rule's applies_to should be a type something actually produces ─

def check_rule_types_exist():
    from analysis import rules, recommender  # noqa: F401
    from collectors.service_costs import _PRICERS, _USAGE_PRICED
    from collectors.discovery import RESOURCE_TYPE_MAP
    from collectors.metrics_auto import METRICS_CONFIG

    known = set(_PRICERS) | set(_USAGE_PRICED) | set(METRICS_CONFIG)
    known |= {t for _, _, t in RESOURCE_TYPE_MAP}
    known |= {"EC2", "RDS", "ElastiCache", "EBSSnapshot", "Account"}

    orphans = []
    for spec in rules.RULES:
        for t in spec["applies_to"]:
            if t not in known:
                orphans.append(f"{spec['name']} -> {t}")
    if orphans:
        bad("Rule targets a resource type nothing produces", "; ".join(orphans))
    else:
        ok("Every rule targets a type the pipeline can produce")


# ── 8. Public entry points must import and be callable ─────────────────────

def check_imports():
    modules = [
        "main", "utils",
        "collectors.aws_pricing", "collectors.vantage_pricing", "collectors.pricing",
        "collectors.service_costs", "collectors.commitments", "collectors.snapshots",
        "collectors.data_transfer", "collectors.cur_reader", "collectors.cur_discovery",
        "collectors.purchase_recommendations", "collectors.rds_pi",
        "collectors.metrics_auto", "collectors.resource_inventory", "collectors.discovery",
        "analysis.provenance", "analysis.rules", "analysis.recommender",
        "analysis.spike_detector", "analysis.lifecycle", "analysis.graviton_check",
        "sheets.writer", "sheets.summary_page", "sheets.service_pages",
        "sheets.recommendations_page", "sheets.data_gaps_page",
        "sheets.commitments_page", "sheets.data_transfer_page",
        "sheets.differential_page", "sheets.uncovered_services_page", "sheets.charts",
    ]
    failed = []
    for m in modules:
        try:
            __import__(m)
        except Exception as e:
            failed.append(f"{m}: {type(e).__name__}: {e}")
    if failed:
        bad("Module failed to import", "; ".join(failed))
    else:
        ok(f"All {len(modules)} modules import cleanly")


def check_no_tab_name_collisions():
    """
    A synthetic resource bucket must never share a name with an analysis page.
    `main.py` injects a "Commitments" holder for AWS's account-wide purchase
    recommendation; when the service-tab loop also rendered it, Sheets rejected
    the duplicate name and every report showed a spurious skipped tab.
    """
    src = (ROOT / "main.py").read_text()
    guard = re.search(r"reserved = \{(.*?)\}", src, re.S)
    if not guard:
        bad("Service-tab loop guards reserved names", "no `reserved` set found")
        return
    reserved = set(re.findall(r'"([^"]+)"', guard.group(1)))

    claimed = set()
    for f in (ROOT / "sheets").glob("*.py"):
        claimed |= set(re.findall(r'safe_add_worksheet\(\s*\w+,\s*"([^"]+)"',
                                  f.read_text()))
    injected = set(re.findall(r'resources\.setdefault\(\s*"([^"]+)"', src))
    missing = (injected & claimed) - reserved
    if missing:
        bad("Synthetic buckets collide with an analysis tab", sorted(missing))
    else:
        ok(f"No tab-name collisions across {len(claimed)} analysis pages")


def main():
    for fn in [check_imports, check_metric_keys, check_column_parity,
               check_rule_requirements, check_no_structured_values_in_sheets,
               check_duplicate_keys, check_none_comparisons, check_rule_types_exist,
               check_pipeline_all_types, check_saving_never_exceeds_cost,
               check_no_commitment_double_count, check_undefined_names,
               check_no_tab_name_collisions]:
        try:
            fn()
        except Exception as e:
            bad(f"{fn.__name__} crashed", f"{type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    for line in CHECKS:
        print(f"  OK    {line}")
    for line in PROBLEMS:
        print(f"  BUG   {line}")
    print("=" * 72)
    print(f"  {len(CHECKS)} checks passed, {len(PROBLEMS)} problems\n")
    return 1 if PROBLEMS else 0



# ── 9. Dynamic: every resource type through the whole pipeline ─────────────

def check_pipeline_all_types():
    """
    Push one synthetic resource of every known type through pricing, rules and
    every sheet row builder. Catches AttributeError/TypeError/KeyError that
    static analysis cannot see — the class of bug that left a tab empty.
    """
    from collectors import service_costs, aws_pricing
    from collectors.service_costs import _PRICERS, _USAGE_PRICED
    from analysis import recommender
    from analysis.provenance import gaps
    from sheets.service_pages import ROW_BUILDERS, build_generic_rows
    import inspect

    aws_pricing.configure(session=None)
    gaps.clear()

    types = sorted(set(_PRICERS) | set(_USAGE_PRICED) | set(ROW_BUILDERS)
                   | {"EC2", "RDS", "ElastiCache", "EBSSnapshot"})
    resources, metrics = {}, {}
    for t in types:
        rid = f"{t.lower()}-1"
        resources[t] = [{
            "type": t, "id": rid, "name": rid, "region": "ap-south-1",
            "arn": f"arn:aws:x:ap-south-1:1:{rid}", "tags": {"Project": "p"},
            "instance_type": {"EC2": "m5.large", "RDS": "db.m5.large",
                              "ElastiCache": "cache.m5.large"}.get(t, ""),
            "state": "running", "platform": "Linux", "engine": "mysql",
            "engine_version": "8.0.35", "size_gb": 100, "storage_gb": 100,
            "volume_type": "gp3", "num_nodes": 1, "stored_gb": 10,
            "volume_size_gb": 100, "rule_count": 2, "associations": 1,
            "image_count": 5, "lifecycle_policy": True, "ia_enabled": True,
            "rotation_enabled": True, "key_state": "Enabled",
            "unattached": False, "retention_days": 30, "lifecycle_rules_count": 1,
            "last_accessed": "2026-07-01", "days_since_access": 5,
            "ami_architecture": "x86_64", "ssm_managed": False,
            "x86_only_software": [], "arm_verify_software": [], "ssm_app_count": 0,
        }]
        metrics[rid] = {"periods": {"30d": {"cpu_avg_pct": 10.0, "cpu_p95_pct": 12.0,
                                            "cpu_max_pct": 30.0, "cpu_min_pct": 2.0}},
                        "spikes": [], "datapoints": 2000, "spike_window_days": 90}

    errors = []
    try:
        service_costs.price_all(resources, region_default="ap-south-1")
        service_costs.apply_metric_costs(resources, metrics)
    except Exception as e:
        errors.append(f"pricing: {type(e).__name__}: {e}")

    try:
        recommender.generate_all_recommendations(
            resources, metrics, {"recommendations": {}, "metrics": {}})
    except Exception as e:
        errors.append(f"rules: {type(e).__name__}: {e}")

    for t, items in resources.items():
        builder = ROW_BUILDERS.get(t)
        try:
            if builder:
                n = len(inspect.signature(builder).parameters)
                builder(items, metrics) if n >= 2 else builder(items)
            else:
                build_generic_rows(items, metrics)
        except Exception as e:
            errors.append(f"sheet[{t}]: {type(e).__name__}: {e}")

    if errors:
        bad("Pipeline raised on a synthetic resource", "; ".join(errors[:6]))
    else:
        ok(f"All {len(types)} resource types survive pricing, rules and sheets")


# ── 10. A saving must never exceed what the resource costs ─────────────────

def check_saving_never_exceeds_cost():
    from analysis import recommender
    from analysis.provenance import gaps
    from collectors import aws_pricing
    aws_pricing.configure(session=None)
    gaps.clear()

    r = {"type": "EC2", "id": "i-x", "region": "ap-south-1",
         "instance_type": "m5.xlarge", "state": "running", "platform": "Linux",
         "monthly_cost_usd": 147.46, "cost_source": "list_price",
         "ami_architecture": "x86_64", "ssm_managed": True, "ssm_app_count": 5,
         "x86_only_software": [], "arm_verify_software": []}
    m = {"i-x": {"periods": {"30d": {"cpu_avg_pct": 4.0, "cpu_p95_pct": 6.0,
                                     "cpu_max_pct": 20.0}},
                 "spikes": [], "datapoints": 2000, "spike_window_days": 90}}
    _, recs, _ = recommender.generate_all_recommendations(
        {"EC2": [r]}, m, {"recommendations": {}, "metrics": {}})
    over = [f["action"] for f in recs["i-x"]["phase1"] + recs["i-x"]["phase2"]
            if f["saving_usd"] and f["saving_usd"] > r["monthly_cost_usd"] + 0.01]
    if over:
        bad("A saving exceeds the resource's own cost", "; ".join(over))
    else:
        ok("No single saving exceeds the resource cost")



# ── 11. Commitment savings must never be counted twice ─────────────────────

def check_no_commitment_double_count():
    """
    AWS's account-wide Savings Plan / RI recommendation and our per-resource RI
    rules describe the SAME dollars. A real report claimed $2,626.03 when the
    non-overlapping figure was $2,211.52 — a 16% overstatement.
    """
    from analysis import recommender
    from collectors import aws_pricing, pricing
    aws_pricing.configure(session=None)

    r = {"type": "RDS", "id": "db1", "region": "ap-south-1",
         "instance_type": "db.m5.xlarge", "engine": "mysql",
         "engine_version": "8.0.35", "multi_az": False, "storage_gb": 200,
         "monthly_cost_usd": 369.38, "cost_source": "list_price", "tags": {"o": "x"}}
    m = {"db1": {"periods": {"30d": {"cpu_avg_pct": 9.0, "cpu_p95_pct": 12.0,
                                     "db_connections_avg": 40.0}},
                 "spikes": [], "datapoints": 2000}}

    _, recs, _ = recommender.generate_all_recommendations(
        {"RDS": [dict(r)]}, m, {"recommendations": {}, "metrics": {}},
        aws_commitment_recs=True)
    priced_ri = [f for f in recs["db1"]["phase2"]
                 if "Reserved" in f["action"] and f["saving_usd"]]
    if priced_ri:
        bad("Per-resource RI priced while AWS account figure is present",
            "; ".join(f["action"] for f in priced_ri))
    else:
        ok("Per-resource RI rules defer to the account-wide AWS figure")



# ── 12. Names used at runtime must be resolvable in scope ──────────────────

def check_undefined_names():
    """
    A NameError inside a broad `except` is invisible until something reports it.
    `timezone.utc` was used in cloudwatch_logs without importing timezone, so
    every log group's last-event date came back blank in every region and
    nothing said so.
    """
    import builtins
    offenders = []
    for path in sorted((ROOT / "collectors").glob("*.py")) + \
                sorted((ROOT / "analysis").glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue

        # Module-level dunders exist at runtime but are not AST-visible.
        module_names = set(dir(builtins)) | {
            '__file__', '__name__', '__doc__', '__package__', '__spec__'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_names |= {(a.asname or a.name) for a in node.names}
            elif isinstance(node, ast.Import):
                module_names |= {(a.asname or a.name.split(".")[0]) for a in node.names}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        module_names.add(t.id)
            elif isinstance(node, ast.Global):
                module_names |= set(node.names)

        # Nested functions close over their parent's scope. Checking them in
        # isolation reports every captured variable as undefined, so only
        # top-level functions are checked — ast.walk covers their nested
        # bodies anyway, with the enclosing bindings in scope.
        nested = set()
        for outer in ast.walk(tree):
            if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(outer):
                    if (inner is not outer
                            and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))):
                        nested.add(inner)

        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n not in nested]:
            local = set(module_names)
            local |= {a.arg for a in fn.args.args}
            local |= {a.arg for a in fn.args.kwonlyargs}
            if fn.args.vararg:
                local.add(fn.args.vararg.arg)
            if fn.args.kwarg:
                local.add(fn.args.kwarg.arg)
            for n in ast.walk(fn):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    local.add(n.name)
                elif isinstance(n, ast.ImportFrom):
                    local |= {(a.asname or a.name) for a in n.names}
                elif isinstance(n, ast.Import):
                    local |= {(a.asname or a.name.split(".")[0]) for a in n.names}
                elif isinstance(n, ast.Assign):
                    for t in ast.walk(n):
                        if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                            local.add(t.id)
                elif isinstance(n, (ast.For, ast.comprehension, ast.With,
                                    ast.ExceptHandler, ast.NamedExpr, ast.AugAssign)):
                    for t in ast.walk(n):
                        if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                            local.add(t.id)
                    if isinstance(n, ast.ExceptHandler) and n.name:
                        local.add(n.name)
                elif isinstance(n, ast.Lambda):
                    local |= {a.arg for a in n.args.args}
                    local |= {a.arg for a in n.args.kwonlyargs}

            # Parameters of nested defs are bound inside those defs, which
            # ast.walk descends into — so they belong in the visible set too.
            for n in ast.walk(fn):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
                    local |= {a.arg for a in n.args.args}
                    local |= {a.arg for a in n.args.kwonlyargs}
                    if n.args.vararg:
                        local.add(n.args.vararg.arg)
                    if n.args.kwarg:
                        local.add(n.args.kwarg.arg)

            for n in ast.walk(fn):
                if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                        and n.id not in local):
                    offenders.append(f"{path.name}:{n.lineno} {n.id}")

    if offenders:
        bad("Name used but not defined in scope", "; ".join(sorted(set(offenders))[:6]))
    else:
        ok("No undefined names in collectors or analysis")


if __name__ == "__main__":
    sys.exit(main())
