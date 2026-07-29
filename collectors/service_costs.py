"""
Live cost for every resource type.

This module exists so that no collector ever contains a price. Collectors
describe *what exists*; this computes *what it costs*, using prices fetched at
runtime for the resource's own region.

Anything that cannot be priced gets `monthly_cost_usd` removed and a gap
recorded — never a fallback constant. A blank cell with a stated reason is a
correct report; a plausible-looking wrong number is not.
"""

from analysis import provenance as prov
from analysis.provenance import Basis, DERIVED, fetched_basis, gaps
from collectors import aws_pricing as ap

HOURS_PER_MONTH = 730


def _hourly_to_monthly(resource, price, label):
    if price is None:
        return False
    monthly = price.amount * HOURS_PER_MONTH
    basis = fetched_basis(
        price,
        formula=f"{label}: ${price.amount:,.6f}/hr x {HOURS_PER_MONTH} hr",
    )
    prov.set_cost(resource, monthly, basis)
    return True


def _per_gb(resource, price, gb, label):
    if price is None or gb is None:
        return False
    monthly = price.amount * float(gb)
    basis = fetched_basis(
        price,
        formula=f"{label}: {float(gb):,.4f} GB x ${price.amount:,.6f}/GB-month",
    )
    prov.set_cost(resource, monthly, basis)
    return True


def _unpriced(resource, reason, how_to_fix=""):
    prov.set_cost(resource, None, None)
    gaps.add(
        category="Pricing",
        what=f"{resource.get('type', 'resource')} {resource.get('id', '')}",
        why=reason,
        how_to_fix=how_to_fix,
        resource_id=resource.get("id", ""),
        resource_type=resource.get("type", ""),
        region=resource.get("region", ""),
        impact="Excluded from cost totals and from any savings calculation.",
    )


# ─── Per-type pricing ────────────────────────────────────────────────────────

def _price_ebs(r, region):
    volume_type = r.get("volume_type") or ""
    size_gb = r.get("size_gb")
    if not volume_type or size_gb in (None, ""):
        return _unpriced(r, "Volume type or size missing from the EC2 API response.")

    gb_price = ap.ebs_gb_month(region, volume_type)
    if gb_price is None:
        return _unpriced(
            r, f"No published price for EBS {volume_type} in {region}.",
            "EBS prices come from the Price List Query API (the EC2 bulk file is "
            "~480 MB). Grant pricing:GetProducts, included in ReadOnlyAccess.")

    monthly = gb_price.amount * float(size_gb)
    parts = [f"{float(size_gb):,.0f} GB x ${gb_price.amount:,.6f}/GB-month"]

    # gp3 bills provisioned IOPS above 3000 and throughput above 125 MB/s.
    if volume_type == "gp3":
        iops = r.get("iops") or 0
        extra_iops = max(0, int(iops) - 3000) if iops else 0
        if extra_iops:
            iops_price = ap.ebs_iops_month(region, "gp3")
            if iops_price:
                monthly += iops_price.amount * extra_iops
                parts.append(f"{extra_iops:,} extra IOPS x ${iops_price.amount:,.6f}")
        throughput = r.get("throughput_mbps") or 0
        extra_tp = max(0, int(throughput) - 125) if throughput else 0
        if extra_tp:
            tp_price = ap.ebs_throughput_month(region, "gp3")
            if tp_price:
                monthly += tp_price.amount * extra_tp
                parts.append(f"{extra_tp:,} extra MB/s x ${tp_price.amount:,.6f}")
    elif volume_type in ("io1", "io2"):
        iops = r.get("iops") or 0
        if iops:
            iops_price = ap.ebs_iops_month(region, volume_type)
            if iops_price:
                monthly += iops_price.amount * int(iops)
                parts.append(f"{int(iops):,} provisioned IOPS x ${iops_price.amount:,.6f}")

    prov.set_cost(r, monthly, Basis(
        DERIVED, formula=" + ".join(parts),
        unit_price=gb_price.amount, unit="GB-month", provider=gb_price.source))


def _price_s3(r, region):
    tiers = [
        ("standard", r.get("size_gb")),
        ("ia", r.get("ia_size_gb")),
        ("glacier", r.get("glacier_size_gb")),
    ]
    total = 0.0
    parts = []
    provider = ""
    resolved_any = False
    for tier, gb in tiers:
        if not gb:
            continue
        price = ap.s3_storage_gb_month(region, tier)
        if price is None:
            gaps.add_price_gap("S3", region, f"S3 {tier} storage price ({region})")
            continue
        total += price.amount * float(gb)
        parts.append(f"{tier} {float(gb):,.2f} GB x ${price.amount:,.6f}")
        provider = price.source
        resolved_any = True

    if not resolved_any:
        # Buckets legitimately can be empty; that is $0 storage, not a gap.
        if all(not gb for _, gb in tiers):
            prov.set_cost(r, 0.0, Basis(
                DERIVED, formula="No objects reported by CloudWatch storage metrics",
                provider="cloudwatch"))
        else:
            _unpriced(r, f"No published S3 storage price for {region}.")
        return

    prov.set_cost(r, total, Basis(
        DERIVED, formula=" + ".join(parts), provider=provider,
        note="Storage only — requests and data transfer are not included"))


def _price_load_balancer(r, region):
    kind = {"ELB": "application", "ALB": "application", "NLB": "network",
            "GWLB": "gateway", "ELBClassic": "classic"}.get(r.get("type"), "application")
    price = ap.load_balancer_hourly(region, kind)
    if not _hourly_to_monthly(r, price, f"{kind} load balancer"):
        return _unpriced(r, f"No published load balancer price for {region}.")
    r["cost_basis"]["note"] = "Hourly charge only — LCU usage is billed on top"


def _price_nat(r, region):
    hourly = ap.nat_gateway_hourly(region)
    if hourly is None:
        return _unpriced(
            r, f"No published NAT Gateway price for {region}.",
            "NAT prices come from the Price List Query API. Grant "
            "pricing:GetProducts, included in ReadOnlyAccess.")
    monthly = hourly.amount * HOURS_PER_MONTH
    parts = [f"${hourly.amount:,.6f}/hr x {HOURS_PER_MONTH} hr"]

    # Data processing needs 30d traffic from CloudWatch; added by apply_metric_costs.
    prov.set_cost(r, monthly, Basis(
        DERIVED, formula=" + ".join(parts), unit_price=hourly.amount,
        unit="hour", provider=hourly.source,
        note="Hourly charge only until traffic metrics are applied"))
    r["_nat_hourly_price"] = hourly.amount
    r["_nat_price_source"] = hourly.source


def _price_elastic_ip(r, region):
    """
    Every public IPv4 address is billed, attached or not.

    Since 1 February 2024 AWS charges for ALL public IPv4 addresses — the old
    "free while associated with a running instance" rule is gone. Zeroing
    attached addresses hid the entire charge: on one account it left three
    Elastic IPs at $0.00 against a real $13.86, and made "terminate this
    instance" worth $2.01 when it was worth $5.61.

    Two SKUs, same rate: IdleAddress when nothing is running behind it,
    InUseAddress otherwise. The distinction matters because only an idle
    address is releasable without touching a live workload.
    """
    idle = r.get("unattached") or r.get("attached_to_stopped")
    price = ap.eip_idle_hourly(region) if idle else ap.eip_in_use_hourly(region)
    label = ("Idle public IPv4 address" if idle
             else "In-use public IPv4 address")
    if not _hourly_to_monthly(r, price, label):
        _unpriced(r, f"No published public IPv4 price for {region}.")


def _price_log_group(r, region):
    price = ap.cw_logs_storage_gb_month(region)
    if not _per_gb(r, price, r.get("stored_gb"), "CloudWatch Logs stored data"):
        _unpriced(r, f"No published CloudWatch Logs storage price for {region}.")


def _price_kms(r, region):
    price = ap.kms_key_month(region)
    if price is None:
        return _unpriced(r, f"No published KMS key price for {region}.")
    prov.set_cost(r, price.amount, fetched_basis(
        price, formula="1 customer-managed key x monthly key charge"))


def _price_secret(r, region):
    price = ap.secret_month(region)
    if price is None:
        return _unpriced(r, f"No published Secrets Manager price for {region}.")
    prov.set_cost(r, price.amount, fetched_basis(
        price, formula="1 secret x monthly secret charge"))


def _price_ecr(r, region):
    price = ap.ecr_gb_month(region)
    if not _per_gb(r, price, r.get("size_gb"), "ECR image storage"):
        _unpriced(r, f"No published ECR storage price for {region}.")


def _price_efs(r, region):
    price = ap.efs_gb_month(region, infrequent_access=False)
    if not _per_gb(r, price, r.get("size_gb"), "EFS standard storage"):
        _unpriced(r, f"No published EFS storage price for {region}.")


def _price_waf(r, region):
    acl = ap.waf_web_acl_month(region)
    rule = ap.waf_rule_month(region)
    if acl is None:
        return _unpriced(r, f"No published WAF Web ACL price for {region}.")
    rule_count = r.get("rule_count") or 0
    total = acl.amount
    parts = [f"1 Web ACL x ${acl.amount:,.2f}/month"]
    if rule and rule_count:
        total += rule.amount * rule_count
        parts.append(f"{rule_count} rules x ${rule.amount:,.2f}/month")
    prov.set_cost(r, total, Basis(
        DERIVED, formula=" + ".join(parts), provider=acl.source,
        note="Excludes per-request charges"))


def _price_eks(r, region):
    price = ap.eks_cluster_hourly(region)
    if not _hourly_to_monthly(r, price, "EKS control plane"):
        return _unpriced(r, f"No published EKS control plane price for {region}.")
    r["cost_basis"]["note"] = "Control plane only — node groups are billed as EC2"


def _price_transfer(r, region):
    protocols = (r.get("protocols") or "").upper()
    domain = (r.get("domain") or "S3").upper()
    proto = "SFTP" if "SFTP" in protocols else (
        "FTPS" if "FTPS" in protocols else ("FTP" if "FTP" in protocols else "SFTP"))
    price = ap.transfer_protocol_hourly(region, f"{proto}:{domain}")
    if price is None:
        price = ap.transfer_protocol_hourly(region, "SFTP:S3")
    if not _hourly_to_monthly(r, price, f"Transfer Family {proto} endpoint"):
        return _unpriced(r, f"No published Transfer Family price for {region}.")
    r["cost_basis"]["note"] = "Endpoint hours only until transfer volume is applied"


def _price_route53(r, region):
    price = ap.route53_hosted_zone_month()
    if price is None:
        return _unpriced(
            r, "No published Route 53 hosted-zone price could be resolved.",
            "Route 53 is billed globally; grant pricing:GetProducts so the "
            "Query API can resolve the hosted-zone SKU.")
    prov.set_cost(r, price.amount, fetched_basis(
        price, formula="1 hosted zone x monthly zone charge"))


# Types priced elsewhere: EC2/RDS/ElastiCache come from collectors.pricing
# (instance rates via Vantage or the Price List API).
_PRICERS = {
    "EBS": _price_ebs,
    "S3": _price_s3,
    "ELB": _price_load_balancer,
    "ALB": _price_load_balancer,
    "NLB": _price_load_balancer,
    "GWLB": _price_load_balancer,
    "ELBClassic": _price_load_balancer,
    "NATGateway": _price_nat,
    "ElasticIP": _price_elastic_ip,
    "ElasticIPs": _price_elastic_ip,
    "CWLogGroup": _price_log_group,
    "CWLogGroups": _price_log_group,
    "KMS": _price_kms,
    "SecretsManager": _price_secret,
    "ECR": _price_ecr,
    "EFS": _price_efs,
    "WAF": _price_waf,
    "EKS": _price_eks,
    "TransferFamily": _price_transfer,
    "Route53": _price_route53,
}

# Types we knowingly cannot price without usage data. Recorded once each, so
# the report states the limitation instead of leaving a silent blank.
_USAGE_PRICED = {
    "DynamoDB":   "billed per request/capacity unit",
    "SQS":        "billed per request",
    "SNS":        "billed per request",
    "Lambda":     "billed per GB-second and per request",
    "APIGateway": "billed per request",
    "Kinesis":    "billed per shard-hour and per PUT unit",
    "CloudFront": "billed per GB and per request, varying by edge location",
    "CodeBuild":  "billed per build-minute",
}


def _link_eips_to_instance_state(resources):
    """
    Mark Elastic IPs whose instance is stopped.

    AWS bills such an address at the IDLE rate even though DescribeAddresses
    reports it as associated — the association survives the stop. It is also
    the only kind that can be released without touching a live workload, so it
    is the one worth recommending.
    """
    stopped = {r.get("id") for r in resources.get("EC2", [])
               if r.get("state") == "stopped"}
    if not stopped:
        return
    for key in ("ElasticIP", "ElasticIPs"):
        for eip in resources.get(key, []):
            if eip.get("attached_to") in stopped:
                eip["attached_to_stopped"] = True


def price_all(resources, region_default="us-east-1"):
    """
    Attach a live monthly cost to every resource that can have one.

    Never overwrites a cost already sourced from billing data (Cost Explorer or
    CUR) — an actual charge always beats a list price.
    """
    priced = 0
    unpriced = 0
    _link_eips_to_instance_state(resources)

    for resource_type, items in resources.items():
        for r in items:
            if prov.is_actual_cost(r):
                priced += 1
                continue

            rtype = r.get("type", resource_type)
            region = r.get("region") or region_default
            if region == "global":
                region = region_default

            pricer = _PRICERS.get(rtype)
            if pricer:
                pricer(r, region)
                priced += prov.has_cost(r)
                unpriced += not prov.has_cost(r)
            elif rtype in _USAGE_PRICED:
                if not prov.has_cost(r):
                    gaps.add(
                        category="Pricing",
                        what=f"{rtype} cost",
                        why=f"{rtype} is {_USAGE_PRICED[rtype]}; a per-resource "
                            f"monthly cost cannot be derived from inventory alone.",
                        how_to_fix="Enable a Cost and Usage Report, or turn on "
                                   "resource-level cost allocation in Cost Explorer.",
                        resource_type=rtype, region=region,
                        impact="Shown without a cost figure.")
                    unpriced += 1

    return {"priced": priced, "unpriced": unpriced}


def apply_metric_costs(resources, all_metrics):
    """
    Add the usage-driven portion of cost for resources whose charge depends on
    measured traffic. Runs after metrics collection.
    """
    for resource_type, items in resources.items():
        for r in items:
            rtype = r.get("type", resource_type)
            metrics = all_metrics.get(r.get("id")) or {}
            region = r.get("region") or "us-east-1"

            if rtype == "NATGateway" and r.get("_nat_hourly_price"):
                gb = metrics.get("total_gb_30d")
                per_gb = ap.nat_gateway_per_gb(region)
                hourly = r.pop("_nat_hourly_price")
                source = r.pop("_nat_price_source", "")
                if gb is None or per_gb is None:
                    continue
                fixed = round(hourly * HOURS_PER_MONTH, 4)
                data = round(per_gb.amount * float(gb), 4)
                # Kept on the resource so the NAT tab can show the split
                r["nat_fixed_cost_usd"] = fixed
                r["nat_data_cost_usd"] = data
                prov.set_cost(r, fixed + data, Basis(
                    DERIVED,
                    formula=(f"${hourly:,.6f}/hr x {HOURS_PER_MONTH} hr "
                             f"+ {float(gb):,.2f} GB x ${per_gb.amount:,.6f}/GB"),
                    provider=source or per_gb.source,
                    note="Traffic measured over the last 30 days"))

            elif rtype == "TransferFamily" and prov.has_cost(r):
                gb = metrics.get("total_gb_30d")
                per_gb = ap.transfer_per_gb(region)
                if gb is None or per_gb is None:
                    continue
                base = prov.cost_of(r)
                r["transfer_data_cost_usd"] = round(per_gb.amount * float(gb), 4)
                prov.set_cost(r, base + per_gb.amount * float(gb), Basis(
                    DERIVED,
                    formula=(f"endpoint hours ${base:,.2f} "
                             f"+ {float(gb):,.2f} GB x ${per_gb.amount:,.6f}/GB"),
                    provider=per_gb.source,
                    note="Transfer volume measured over the last 30 days"))

    return resources
