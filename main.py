#!/usr/bin/env python3
"""
AWS Cost Optimization Report
Single command: python main.py --config config.yaml --gcp-sa service-account.json
"""

import argparse
import json
import os
import sys

import boto3
import yaml
from rich.console import Console
from rich.panel import Panel

from utils import utcnow
from collectors import (
    cost_explorer, resource_inventory, pricing, aws_pricing, service_costs,
    cur_discovery, commitments, cur_reader, snapshots, data_transfer,
    instance_hours,
    rds_pi, purchase_recommendations,
)
from collectors.metrics_auto import collect_all_metrics
from analysis import recommender, differential, reconciliation
from analysis.provenance import gaps, CONFIRMED, ESTIMATED
from analysis.service_registry import classify_services
from sheets import writer, summary_page, service_pages, recommendations_page, differential_page, charts
from sheets.uncovered_services_page import build_uncovered_services_page
from sheets.data_gaps_page import build_data_gaps_page
from sheets.commitments_page import build_commitments_page
from sheets.data_transfer_page import build_data_transfer_page
from sheets.reconciliation_page import build_reconciliation_page

console = Console()


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def build_aws_session(profile=None, role_arn=None,
                      access_key=None, secret_key=None, session_token=None):
    if access_key and secret_key:
        base = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token or None,
        )
    elif profile:
        base = boto3.Session(profile_name=profile)
    else:
        base = boto3.Session()

    if role_arn:
        sts = base.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="CostReport")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    return base


def run(config_path, gcp_sa_path, aws_profile=None, role_arn=None, dry_run=False,
        aws_access_key=None, aws_secret_key=None, aws_session_token=None,
        log_fn=None):
    config = load_config(config_path)
    regions = config["aws"]["regions"]
    snapshots_dir = config["snapshots"]["directory"]

    console.print(Panel("[bold cyan]AWS Cost Optimization Report[/bold cyan]", expand=False))

    # AWS session — read-only
    console.print("\n[bold]Step 1/5[/bold] Connecting to AWS (read-only)...")
    _log = log_fn or (lambda msg: None)
    _log(("step", 1, 5, "Connecting to AWS (read-only)..."))
    session = build_aws_session(aws_profile, role_arn, aws_access_key, aws_secret_key, aws_session_token)
    account_id = config["aws"].get("account_id") or cost_explorer.get_account_id(session)
    console.print(f"  Account: [green]{account_id}[/green]")
    console.print(f"  Regions: {', '.join(regions)}")
    _log(("info", f"Account {account_id} | Regions: {', '.join(regions)}"))

    # Collect billing data
    console.print("\n[bold]Step 2/5[/bold] Fetching billing data from Cost Explorer...")
    _log(("step", 2, 5, "Fetching billing data from Cost Explorer..."))
    history_days = config["aws"]["cost_history_days"]
    months = max(3, history_days // 30)

    monthly_costs = cost_explorer.get_monthly_costs(session, months=months)
    daily_costs = cost_explorer.get_daily_costs(session, days=history_days)
    console.print(f"  [green]✓[/green] {len(monthly_costs)} monthly records, {len(daily_costs)} daily records")

    # Credits are excluded from the totals above (RECORD_TYPE=Usage), so an
    # account running on promotional credit shows its real usage. Say so
    # explicitly — otherwise the owner sees a bill of $0 and a report of $1,400.
    credits = cost_explorer.get_credit_coverage(session, months=months)
    if credits.get("available") and credits.get("covered_pct", 0) >= 5:
        console.print(f"  [yellow]! {credits['covered_pct']}% of "
                      f"${credits['usage_usd']:,.2f} usage in {credits['month']} "
                      f"is paid by credits — invoiced ${credits['invoiced_usd']:,.2f}. "
                      f"Savings below are against real usage.[/yellow]")
    _log(("info", f"✓  {len(monthly_costs)} monthly cost records, {len(daily_costs)} daily records"))

    # Existing Reserved Instances / Savings Plans. Two accuracy controls:
    # they stop us recommending a reservation the customer already owns, and
    # their presence proves the list-price baseline is wrong.
    console.print("  Checking existing Reserved Instances and Savings Plans...")
    commitment_data = commitments.collect(session, regions)
    coverage = commitments.build_coverage(commitment_data["items"])
    if commitment_data["has_commitments"]:
        detail = ", ".join(f"{n} {k}" for k, n in commitment_data["by_type"].items())
        console.print(f"  [yellow]! {len(commitment_data['items'])} active "
                      f"commitment(s) found ({detail}) — list-price savings are "
                      f"overstated for covered resources[/yellow]")
    else:
        console.print("  [green]OK[/green] No active commitments — on-demand list "
                      "price is the correct baseline")

    # AWS's own Savings Plan / Reserved Instance recommendations, computed from
    # this account's real billing history. More reliable than anything derivable
    # from inventory, so they are reported as Confirmed.
    console.print("  Fetching AWS purchase recommendations (Savings Plans / RIs)...")
    purchase_data = purchase_recommendations.collect(session)
    if purchase_data.get("credentials_failed"):
        console.print("  [red]! Credentials expired or were rejected — commitment "
                      "savings were NOT assessed. The $0 below means 'not checked', "
                      "not 'nothing available'. Run `aws sso login` and re-run."
                      "[/red]")
    elif purchase_data["total_monthly_savings"]:
        console.print(f"  [green]OK[/green] AWS recommends commitments worth "
                      f"${purchase_data['total_monthly_savings']:,.2f}/mo "
                      f"(via {purchase_data['compute_best_route']} for compute)")

    # How good is our cost data? Determines whether savings can be Confirmed.
    cost_quality = cur_discovery.detect(session)
    console.print(f"  Cost data: [cyan]{cost_quality['message']}[/cyan]")
    _log(("info", cost_quality["message"]))

    # Read actual billed cost when a CUR exists. This is what upgrades savings
    # from Estimated (list price) to Confirmed (measured).
    cur_data = {"available": False}
    if cost_quality.get("available") and cost_quality.get("best"):
        console.print("  Reading Cost and Usage Report (read-only)...")
        cur_data = cur_reader.read(session, cost_quality["best"])
        if cur_data.get("available"):
            pct = cur_data.get("discount_pct")
            console.print(f"  [green]OK[/green] Actual cost for "
                          f"{len(cur_data['by_resource'])} resources"
                          + (f" | effective discount vs list: {pct}%" if pct else ""))
        else:
            console.print(f"  [yellow]CUR not readable: "
                          f"{cur_data.get('reason', 'unknown')}[/yellow]")
            cur_discovery.finalise_quality(cost_quality, cur_data)

    # Data transfer is billed usage with no resource to discover, so an
    # inventory-driven scan misses it entirely. Sourced straight from billing.
    console.print("  Analysing data transfer spend...")
    transfer = data_transfer.collect(session, days=60)
    if transfer.get("available") and transfer["total_usd"]:
        console.print(f"  [green]OK[/green] ${transfer['total_usd']:,.2f}/mo of "
                      f"data transfer across {len(transfer['buckets'])} categories")

    # Classify active services from billing data
    covered_services, billing_only_services, _skipped = classify_services(monthly_costs)
    console.print(f"  Active services detected: {len(covered_services) + len(billing_only_services)} "
                  f"({len(covered_services)} with full analysis, {len(billing_only_services)} billing-only)")

    # Collect resource inventory — driven by CE billing data
    console.print("\n[bold]Step 3/5[/bold] Collecting resource inventory (CE-driven)...")
    _log(("step", 3, 5, "Scanning all resources across selected regions..."))
    resources = resource_inventory.collect_all(session, regions, monthly_costs=monthly_costs)
    total_resources = sum(len(v) for v in resources.values())
    console.print(f"  [green]✓[/green] {total_resources} total resources found")
    _log(("info", f"✓  {total_resources} resources found across {len(resources)} service types"))

    # EBS snapshots: never collected before, so their spend sat unexplained
    # inside the "EC2 - Other" line item.
    snaps = snapshots.get_snapshots(session, regions, account_id)
    if snaps:
        snapshots.mark_orphans(snaps, resources.get("EBS", []))
        resources["EBSSnapshot"] = snaps
        total_resources = sum(len(v) for v in resources.values())
        orphans = sum(1 for s_ in snaps if s_.get("orphaned"))
        console.print(f"  [green]OK[/green] {len(snaps)} EBS snapshots "
                      f"({orphans} orphaned - source volume deleted)")

    # Pricing. Every rate is fetched live for the resource's own region — the
    # session MUST be passed through, or a bare boto3 client would ignore
    # runtime-supplied credentials and silently return no prices at all.
    console.print("  Fetching live prices...")
    aws_pricing.configure(session=session)
    vantage_token = (config.get("pricing", {}) or {}).get("vantage_token")
    resources = pricing.enrich_with_pricing(resources, session=session,
                                            vantage_token=vantage_token)
    # AWS's own public IPv4 charge, used as a ceiling so the per-resource
    # attribution can never exceed what was actually billed.
    ipv4_actual = sum(cost_explorer.get_usage_type_costs(
        session, "PublicIPv4").values()) or None
    priced = service_costs.price_all(resources, region_default=regions[0],
                                     public_ipv4_actual=ipv4_actual)
    if priced.get("public_ipv4_resources"):
        console.print(f"  [green]OK[/green] Public IPv4 charged on "
                      f"{priced['public_ipv4_resources']} resource(s) beyond "
                      f"Elastic IPs (auto-assigned, NAT, load balancers)")
    if resources.get("EBSSnapshot"):
        snapshots.price_snapshots(resources["EBSSnapshot"])

    # Billed hours replace the 730-hour assumption. An instance that ran a
    # fifth of the month was priced at five times its real cost, and every
    # saving derived from it inherited the same multiple. Applied BEFORE the
    # CUR pass so that actual billed cost, where available, still wins.
    hours_data = instance_hours.collect(session, days=30)
    if hours_data.get("available"):
        adjusted = instance_hours.apply_uptime(resources, hours_data)
        untracked = instance_hours.detect_untracked(resources, hours_data)
        if adjusted:
            console.print(f"  [green]OK[/green] {adjusted} instance(s) repriced "
                          f"on measured running hours, not a full month")
        if untracked:
            lost = sum(u["cost_usd"] for u in untracked)
            console.print(f"  [yellow]! {len(untracked)} instance type(s) billed "
                          f"${lost:,.2f} but no longer exist — terminated before "
                          f"this scan[/yellow]")

    # Actual billed cost overwrites list price wherever CUR supplied it.
    if cur_data.get("available"):
        applied = cur_reader.apply_actual_costs(resources, cur_data)
        console.print(f"  [green]OK[/green] Actual billed cost applied to "
                      f"{applied} resources (overrides list price)")
    provider = pricing.active_provider()
    console.print(f"  Instance pricing: [cyan]{provider}[/cyan]")
    console.print(f"  [green]✓[/green] {priced['priced']} resources priced, "
                  f"{priced['unpriced']} could not be priced")
    _log(("info", f"Pricing source: {provider}"))

    # Generic CloudWatch metrics — works for any discovered resource type
    console.print("\n[bold]Step 4/5[/bold] Fetching CloudWatch metrics (generic, all services)...")
    _log(("step", 4, 5, "Fetching CloudWatch metrics (CPU, memory, network, errors)..."))
    all_metrics = collect_all_metrics(session, resources, config=config)
    console.print(f"  [green]✓[/green] Metrics collected for {len(all_metrics)} resources")
    _log(("info", f"✓  Metrics collected for {len(all_metrics)} resources"))

    # Usage-driven cost (NAT data processing, Transfer volume) needs metrics
    resources = service_costs.apply_metric_costs(resources, all_metrics)

    # RDS Performance Insights — distinguishes an idle database from one that is
    # IO-bound, which CPU average alone cannot do.
    if resources.get("RDS"):
        rds_pi.enrich_rds_with_pi(session, resources["RDS"])
        with_pi = sum(1 for r in resources["RDS"]
                      if (r.get("performance_insights") or {}).get("pi_data_available"))
        console.print(f"  Performance Insights available for {with_pi}/"
                      f"{len(resources['RDS'])} databases")

    # Recommendations. Rules are requirement-gated: one that lacks its inputs
    # does not run, so no saving is ever invented to fill a gap.
    resources, all_recs, savings = recommender.generate_all_recommendations(
        resources, all_metrics, config, session=session,
        coverage=coverage, has_commitments=commitment_data["has_commitments"],
        aws_commitment_recs=bool(purchase_data.get("total_monthly_savings")))

    # AWS's account-level commitment recommendations, appended to the findings.
    # Every per-resource finding produced so far, so the commitment check can
    # see whether this report also recommends removing the capacity in question.
    prior = [f for rec in all_recs.values()
             for f in rec.get("phase1", []) + rec.get("phase2", [])]
    commit_risk = purchase_recommendations.assess_commitment_risk(
        purchase_data, monthly_costs, prior)
    commit_findings = recommender.commitment_findings(
        purchase_data, commitment_data["has_commitments"], risk=commit_risk)
    if commit_risk.get("warnings"):
        console.print(f"  [yellow]! Commitment exposure "
                      f"${commit_risk['exposure_usd']:,.2f} over "
                      f"{commit_risk['term_years']}y — "
                      f"{len(commit_risk['warnings'])} caution(s) attached[/yellow]")
    if commit_findings:
        holder = {"type": "Account", "id": account_id, "name": "Account-wide",
                  "region": "all", "recommendations": {
                      "phase1": [], "phase2": commit_findings,
                      "lifecycle_warnings": [], "savings_phase1_usd": 0,
                      "savings_phase2_usd": sum(f["saving_usd"] or 0 for f in commit_findings)}}
        resources.setdefault("Commitments", []).append(holder)
        all_recs[account_id] = holder["recommendations"]
        savings["confirmed_savings_usd"] = round(
            savings["confirmed_savings_usd"]
            + sum(f["saving_usd"] or 0 for f in commit_findings), 2)

    total_p1 = sum(r.get("savings_phase1_usd", 0) or 0 for r in all_recs.values())
    total_p2 = sum(r.get("savings_phase2_usd", 0) or 0 for r in all_recs.values())
    confirmed = savings["confirmed_savings_usd"]
    estimated = savings["estimated_savings_usd"]
    console.print(f"\n  [green]Confirmed savings: ${confirmed:,.2f}/mo[/green]  "
                  f"(measured cost + live target price)")
    console.print(f"  [yellow]Estimated savings: ${estimated:,.2f}/mo[/yellow]  "
                  f"(list price basis)")
    console.print(f"  [dim]{savings['unpriced_actions']} actions with no "
                  f"quantifiable saving[/dim]")
    console.print(f"  Data gaps recorded: {gaps.count()}")
    _log(("savings", total_p1, total_p2))

    # Save snapshot and compute diff
    snapshot_data = {
        "account_id": account_id,
        "resources": {k: v for k, v in resources.items()},
        "monthly_costs": monthly_costs,
        "daily_costs": daily_costs,
        "metrics": all_metrics,
    }
    current_snapshot_path = differential.save_snapshot(snapshot_data, snapshots_dir, account_id)
    console.print(f"\n  Snapshot saved: {current_snapshot_path}")

    previous_snapshot = differential.load_latest_snapshot(snapshots_dir, account_id, exclude_path=current_snapshot_path)
    current_snapshot = {"timestamp": utcnow().strftime("%Y-%m-%dT%H-%M-%S"), "data": snapshot_data}
    diff = differential.compute_diff(current_snapshot, previous_snapshot)

    if diff.get("has_previous"):
        console.print(f"  Diff vs previous report ({diff['previous_timestamp']}): "
                      f"[{'green' if diff['total_delta'] <= 0 else 'red'}]${diff['total_delta']:+,.2f}[/]")
    else:
        console.print("  No previous snapshot found — this is the baseline.")

    differential.prune_old_snapshots(snapshots_dir, account_id, config["snapshots"]["keep_last"])

    # Prove the report against the bill before writing a single tab. Everything
    # else in this pipeline checks itself; this is the only check that compares
    # the output to what AWS actually charged.
    # Only same-period figures may offset the bill. Data transfer is measured
    # on the same calendar month; billed instance hours are a rolling 30-day
    # window, so folding those in would mix periods to flatter the number —
    # the same class of error as everything else fixed here. They stay in the
    # unexplained portion, where the Coverage gap already names them.
    recon = reconciliation.reconcile(
        monthly_costs, resources, transfer.get("top_usage_types"),
        explained={"Data transfer (own tab)": transfer.get("total_usd")
                   if transfer.get("month") == reconciliation._latest_month(monthly_costs)
                   else 0.0})
    if recon.get("available"):
        colour = "green" if recon["coverage_pct"] >= 90 else "yellow"
        console.print(f"  [{colour}]Reconciliation: ${recon['attributed_usd']:,.2f} "
                      f"of ${recon['billed_usd']:,.2f} billed attributed to "
                      f"resources ({recon['coverage_pct']}%)[/{colour}]")

    if dry_run:
        console.print("\n[yellow]Dry run — skipping Google Sheets creation.[/yellow]")
        console.print(json.dumps(snapshot_data, indent=2, default=str)[:2000])
        return

    # Build Google Sheet
    console.print(f"\n[bold]Step 5/5[/bold] Building Google Sheet...")
    _log(("step", 5, 5, "Building Google Sheet with all service tabs..."))
    gc = writer.connect(gcp_sa_path)

    report_date = utcnow().strftime("%Y-%m-%d")
    sheet_name_fmt = config["google_sheets"]["sheet_name_format"]
    sheet_name = sheet_name_fmt.format(account_id=account_id, date=report_date)
    share_with = config["google_sheets"].get("share_with", "")

    sh = writer.create_spreadsheet(gc, sheet_name, share_with or None)
    console.print(f"  Created: [cyan]{sheet_name}[/cyan]")

    # The summary page does not render a logo, so uploading one only created a
    # world-readable Drive file for nothing. Left out deliberately.
    summary_ws = summary_page.build_summary_page(
        sh, account_id, monthly_costs, daily_costs, diff, report_date,
        total_p1=total_p1, total_p2=total_p2, total_resources=total_resources,
        confirmed_savings=confirmed, estimated_savings=estimated,
        cost_quality=cost_quality, gap_count=gaps.count(),
    )
    console.print("  Summary page done")

    charts.add_service_pie_chart(sh, summary_ws, monthly_costs)
    console.print("  Charts added")

    # Build service pages only for services that actually have resources
    # Order: compute → db → storage → network → security → devops → observability
    preferred_order = [
        "EC2", "EKS", "Lambda",
        "RDS", "ElastiCache",
        "S3", "EBS",
        "ELB", "NATGateway", "ElasticIPs", "CloudFront",
        "TransferFamily",
        "WAF", "KMS", "SecretsManager",
        "ECR", "CodeBuild",
        "CWLogGroups",
        "Route53",
    ]
    # Only build tabs for services we have sheet definitions for
    # Skip infrastructure noise: subnets, security groups, network interfaces, etc.
    SKIP_TYPES = {
        "Subnet", "VPC", "SecurityGroup", "NetworkInterface", "RouteTable",
        "InternetGateway", "NetworkACL", "LaunchTemplate", "CustomerGateway",
        "VPNConnection", "VPNGateway", "EKSPod", "EKSAddon", "EKSAccessEntry",
        "EKSNodegroup", "RDSSubnetGroup", "ElastiCacheParameterGroup",
        "ElastiCacheSubnetGroup", "ELBListener", "ELBListenerRule",
        "ELBTargetGroup", "IAMInstanceProfile", "IAMOIDCProvider", "IAMPolicy",
        "CloudFormationStack", "CloudWatchAlarm", "CodeStarConnection",
        "ACMCertificate", "WAFRegional", "PaymentInstrument",
    }

    # Also skip any type that contains "/" (raw RGTA types we didn't map)
    all_collected = set(k for k, v in resources.items() if v)
    ordered_services = [s for s in preferred_order if s in all_collected]
    # Add any extra types we have sheet definitions for (not just preferred order)
    ordered_services += [
        s for s in sorted(all_collected)
        if s not in preferred_order
        and s not in SKIP_TYPES
        and "/" not in s
    ]

    # Analysis tabs are built BEFORE the long tail of per-service tabs.
    # Each tab costs ~3 Sheets API writes and Google allows 60/minute; a large
    # account produced 26 service tabs, exhausted the quota, and the run died
    # before writing Recommendations, All Services and Data Gaps — i.e. it
    # dropped exactly the tabs the report exists for. Front-loading them means
    # a quota stall degrades the appendix, never the conclusions.
    recommendations_page.build_recommendations_page(sh, resources, all_recs)
    console.print("  Recommendations page done")

    build_data_gaps_page(
        sh, gaps.all(), cost_quality=cost_quality,
        pricing_provider=pricing.active_provider(),
        unresolved_prices=aws_pricing.unresolved_prices())
    console.print(f"  Data Gaps page — {gaps.count()} gaps recorded")

    if build_reconciliation_page(sh, recon):
        console.print(f"  Reconciliation page — {recon['coverage_pct']}% of the "
                      f"bill accounted for")

    if build_data_transfer_page(sh, transfer):
        console.print(f"  Data Transfer page — ${transfer['total_usd']:,.2f}/mo attributed")

    build_commitments_page(sh, commitment_data, purchase_data)
    console.print(f"  Commitments page — {len(commitment_data['items'])} active commitment(s)")

    build_uncovered_services_page(sh, monthly_costs, billing_only_services, covered_services)
    console.print(f"  All Services (Billing) page — {len(billing_only_services)} billing-only services flagged")

    if diff.get("has_previous"):
        differential_page.build_differential_page(sh, diff)
        console.print("  Changes (differential) page done")

    # Names already claimed by the analysis pages above. "Commitments" is not a
    # real inventory bucket — it is a synthetic holder for AWS's account-wide
    # purchase recommendation, and its findings already appear on the
    # Commitments and Recommendations pages. Without this guard the loop tried
    # to add a second tab of the same name, which Sheets rejects, and the
    # report showed a spurious "1 service tab(s) skipped" warning.
    reserved = {"Summary", "Recommendations", "Data Gaps", "Data Transfer",
                "Commitments", "All Services (Billing)", "Changes"}

    failed_pages = []
    for service in ordered_services:
        items = resources.get(service, [])
        if not items or service in reserved:
            continue
        try:
            service_pages.build_service_page(sh, service, items, all_metrics)
            console.print(f"  {service} page ({len(items)} resources)")
        except Exception as e:
            # A per-service tab failing must not discard the whole report.
            failed_pages.append(service)
            console.print(f"  [yellow]{service} page skipped: {e}[/yellow]")
            gaps.add(category="Report",
                     what=f"{service} tab",
                     why=f"Sheets API error while writing the tab: {e}",
                     how_to_fix="Re-run — this is usually the 60 writes/minute quota.",
                     resource_type=service,
                     impact="Resource detail for this service is missing from the report.")
    if failed_pages:
        console.print(f"  [yellow]{len(failed_pages)} service tab(s) skipped: "
                      f"{', '.join(failed_pages)}[/yellow]")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
    _log(("done", sheet_url, total_p1, total_p2, total_resources))
    console.print(Panel(
        f"[bold green]Report complete[/bold green]\n\n"
        f"[link={sheet_url}]{sheet_url}[/link]\n\n"
        f"Phase 1 savings: [green]${total_p1:,.2f}/mo[/green]\n"
        f"Phase 2 savings: [blue]${total_p2:,.2f}/mo[/blue]\n"
        f"Total potential: [bold]${total_p1 + total_p2:,.2f}/mo[/bold]",
        title="AWS Cost Report",
        expand=False,
    ))

    return sh.id


def main():
    parser = argparse.ArgumentParser(description="AWS Cost Optimization Report Generator")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--gcp-sa", default="service-account.json", help="Path to GCP service account JSON")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    parser.add_argument("--role-arn", default=None, help="IAM role ARN to assume (for cross-account)")
    parser.add_argument("--dry-run", action="store_true", help="Collect data only, skip Sheets creation")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        console.print(f"[red]Config not found: {args.config}[/red]")
        sys.exit(1)

    if not args.dry_run and not os.path.exists(args.gcp_sa):
        console.print(f"[red]GCP service account not found: {args.gcp_sa}[/red]")
        console.print("Run [bold]./setup.sh[/bold] first to set up GCP credentials.")
        sys.exit(1)

    run(
        config_path=args.config,
        gcp_sa_path=args.gcp_sa,
        aws_profile=args.profile,
        role_arn=args.role_arn,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
