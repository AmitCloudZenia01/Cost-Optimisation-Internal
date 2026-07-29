"""
Instance pricing, with two providers.

  1. Vantage Instances API (preferred) — on-demand AND real reserved rates,
     plus CPU architecture. Needs a free API token; see vantage_pricing.py.
  2. AWS Price List API (fallback) — on-demand only.

IMPORTANT: every AWS client here is built from the caller's boto3 Session.
Using a bare boto3.client() would fall back to the ambient credential chain,
which silently returns no prices when the user supplied access keys or an
assumed role at runtime — and since every saving estimate is derived from
these prices, the whole report would show $0.
"""

import json
from functools import lru_cache

from collectors import vantage_pricing

REGION_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-central-1": "Europe (Frankfurt)",
    "sa-east-1": "South America (Sao Paulo)",
    "ca-central-1": "Canada (Central)",
}

# Set once per run by enrich_with_pricing(). The Price List API is only
# offered in a few regions; us-east-1 always works.
_pricing_client = None


_session = None
_ec2_offerings = {}
_rds_offerings = {}


def set_session(session):
    """Bind the Price List client to the run's session."""
    global _pricing_client, _session
    _session = session
    _pricing_client = session.client("pricing", region_name="us-east-1")
    _get_ec2_price_cached.cache_clear()
    _get_rds_price_cached.cache_clear()
    _ec2_offerings.clear()
    _rds_offerings.clear()


def _ec2_types_offered(region):
    """
    Every EC2 instance type AWS actually offers in a region.

    ec2:DescribeInstanceTypeOfferings is authoritative and region-exact, so it
    is the right source for "does this target exist?". The pricing-catalogue
    family listing is only a fallback: if it were unavailable the check
    returned "unknown", the rule proceeded, and a naive one-tier step down
    could name something like m5.medium — which AWS does not sell.
    """
    if region in _ec2_offerings:
        return _ec2_offerings[region]
    types = set()
    if _session is not None:
        try:
            ec2 = _session.client("ec2", region_name=region)
            paginator = ec2.get_paginator("describe_instance_type_offerings")
            for page in paginator.paginate(LocationType="region"):
                types |= {o["InstanceType"] for o in page["InstanceTypeOfferings"]}
        except Exception:
            types = set()
    _ec2_offerings[region] = types
    return types


_instance_specs = {}


def instance_specs(instance_type, region=None):
    """
    (vcpu, memory_gib) for an EC2 instance type, from AWS itself.

    ec2:DescribeInstanceTypes is authoritative and free, so it is preferred
    over any third-party catalogue for a figure that gates a rightsizing
    decision. Returns (None, None) when it cannot be established — callers
    must treat that as "unable to verify", never as a pass.
    """
    if instance_type in _instance_specs:
        return _instance_specs[instance_type]

    result = (None, None)
    if _session is not None and instance_type:
        try:
            ec2 = _session.client("ec2", region_name=region or "us-east-1")
            resp = ec2.describe_instance_types(InstanceTypes=[instance_type])
            for info in resp.get("InstanceTypes", []):
                vcpu = (info.get("VCpuInfo") or {}).get("DefaultVCpus")
                mib = (info.get("MemoryInfo") or {}).get("SizeInMiB")
                result = (vcpu, round(mib / 1024, 2) if mib else None)
                break
        except Exception:
            result = (None, None)

    if result == (None, None):
        result = vantage_pricing.ec2_specs(instance_type)

    _instance_specs[instance_type] = result
    return result


def _rds_class_offered(region, engine, db_class):
    """
    Is one RDS instance class orderable for this engine and region?

    Queried per class rather than by enumerating the engine: unfiltered,
    DescribeOrderableDBInstanceOptions returns every class x engine-version x
    AZ combination and takes minutes. Filtering to the single class of interest
    makes it one small call, and the answer is authoritative — RDS families
    skip sizes EC2 has (db.m5 starts at .large), so this is the only reliable
    way to know a target exists.

    Returns True / False / None (could not determine).
    """
    key = (region, (engine or "").lower(), db_class)
    if key in _rds_offerings:
        return _rds_offerings[key]

    result = None
    if _session is not None and engine and db_class:
        try:
            rds = _session.client("rds", region_name=region)
            resp = rds.describe_orderable_db_instance_options(
                Engine=engine, DBInstanceClass=db_class, MaxRecords=20)
            result = bool(resp.get("OrderableDBInstanceOptions"))
        except Exception as e:
            # An unknown class is rejected outright rather than returned empty.
            text = str(e)
            if "InvalidParameterValue" in text or "InvalidParameterCombination" in text:
                result = False
            else:
                result = None
    _rds_offerings[key] = result
    return result


def _client():
    return _pricing_client


def _first_ondemand_price(price_list):
    for entry in price_list:
        price_data = json.loads(entry)
        for term in price_data.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                price = float(dim["pricePerUnit"].get("USD", 0))
                if price > 0:
                    return price
    return None


@lru_cache(maxsize=512)
def _get_ec2_price_cached(instance_type, location, os_type):
    pricing = _client()
    if pricing is None or not instance_type:
        return None
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_type},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
    ]
    try:
        resp = pricing.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=10)
        return _first_ondemand_price(resp.get("PriceList", []))
    except Exception:
        return None


def get_ec2_hourly_price(instance_type, region, platform="Linux"):
    price = vantage_pricing.ec2_hourly(instance_type, region, platform)
    if price:
        return price
    location = REGION_MAP.get(region, "US East (N. Virginia)")
    os_type = "Windows" if "Windows" in (platform or "") else "Linux"
    return _get_ec2_price_cached(instance_type, location, os_type)


def get_ec2_reserved_hourly(instance_type, region, platform="Linux"):
    """Best 1-year Standard RI hourly rate, or None if unavailable."""
    return vantage_pricing.ec2_reserved_hourly(instance_type, region, platform)


def instance_type_exists(instance_type, region=None):
    """
    True / False / None (unknown). Prevents recommending a target instance type
    AWS does not actually offer in that region.

    AWS's own offerings API is authoritative and answers per region, so it wins.
    The pricing-catalogue family listing is the fallback for when no session is
    available (offline tests) — but it cannot confirm regional availability.
    """
    if region:
        offered = _ec2_types_offered(region)
        if offered:
            return instance_type in offered

    family = instance_type.split(".")[0] if "." in instance_type else ""
    types = vantage_pricing.family_instance_types(family)
    if not types:
        return None
    if instance_type not in types:
        return False
    if region is None:
        return True
    return vantage_pricing.ec2_supports_region(instance_type, region)


def db_instance_type_exists(instance_type, region=None, engine=None):
    """
    True / False / None (unknown) for an RDS instance class.

    RDS families do not offer every size EC2 does — db.m5 starts at .large, so
    a naive "one tier down" from db.m5.large produces db.m5.medium, which does
    not exist. AWS's orderable-options API answers this exactly, for the engine
    and region in question; the catalogue family listing is the fallback.
    """
    if region and engine:
        offered = _rds_class_offered(region, engine, instance_type)
        if offered is not None:
            return offered

    parts = instance_type.split(".")
    if len(parts) < 3:
        return None
    types = vantage_pricing.family_instance_types(parts[1], service="rds")
    if not types:
        return None
    return instance_type in types


def cache_instance_type_exists(instance_type):
    parts = instance_type.split(".")
    if len(parts) < 3:
        return None
    types = vantage_pricing.family_instance_types(parts[1], service="cache")
    if not types:
        return None
    return instance_type in types


def is_arm_instance_type(instance_type):
    """True if the type is ARM64 per the Vantage spec data, else None."""
    arch = vantage_pricing.ec2_architectures(instance_type)
    if not arch:
        return None
    return any("arm" in a.lower() for a in arch)


@lru_cache(maxsize=256)
def _get_rds_price_cached(instance_type, engine, location, deployment):
    pricing = _client()
    if pricing is None or not instance_type:
        return None
    engine_map = {
        "mysql": "MySQL",
        "postgres": "PostgreSQL",
        "aurora-mysql": "Aurora MySQL",
        "aurora-postgresql": "Aurora PostgreSQL",
        "mariadb": "MariaDB",
        "oracle-ee": "Oracle",
        "oracle-se2": "Oracle",
        "sqlserver-ex": "SQL Server",
        "sqlserver-se": "SQL Server",
        "sqlserver-ee": "SQL Server",
        "sqlserver-web": "SQL Server",
    }
    db_engine = engine_map.get(engine, "MySQL")
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": db_engine},
        {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment},
    ]
    try:
        resp = pricing.get_products(ServiceCode="AmazonRDS", Filters=filters, MaxResults=10)
        return _first_ondemand_price(resp.get("PriceList", []))
    except Exception:
        return None


def get_rds_hourly_price(instance_type, engine, region, multi_az=False):
    """
    Hourly on-demand price. Multi-AZ is priced by AWS as its own SKU, so we ask
    for it directly rather than doubling the Single-AZ price.
    """
    price = vantage_pricing.rds_hourly(instance_type, region, engine, multi_az)
    if price:
        return price

    location = REGION_MAP.get(region, "US East (N. Virginia)")
    deployment = "Multi-AZ" if multi_az else "Single-AZ"
    price = _get_rds_price_cached(instance_type, engine, location, deployment)
    if price is None and multi_az:
        # Fall back to the documented ~2x approximation if the Multi-AZ SKU is missing.
        single = _get_rds_price_cached(instance_type, engine, location, "Single-AZ")
        return round(single * 2, 4) if single else None
    return price


def get_rds_reserved_hourly(instance_type, engine, region, multi_az=False):
    return vantage_pricing.rds_reserved_hourly(instance_type, region, engine, multi_az)


def rds_specs(instance_type):
    """(vcpu, memory_gib) for an RDS instance class."""
    return vantage_pricing.service_specs("rds", instance_type)


def cache_specs(instance_type):
    """(vcpu, memory_gib) for an ElastiCache node type."""
    return vantage_pricing.service_specs("cache", instance_type)


def get_rds_vcpu(instance_type):
    return vantage_pricing.rds_vcpu(instance_type)


def get_cache_hourly_price(instance_type, region, engine="Redis"):
    return vantage_pricing.cache_hourly(instance_type, region, engine)


def compute_monthly_cost(hourly_price):
    if hourly_price is None:
        return None
    return round(hourly_price * 730, 2)


def monthly_cost_for_ec2(instance_type, region, platform="Linux"):
    """Estimated on-demand monthly cost for an arbitrary EC2 type — used to
    price rightsize/Graviton *targets*, not just the current instance."""
    return compute_monthly_cost(get_ec2_hourly_price(instance_type, region, platform))


def monthly_cost_for_rds(instance_type, engine, region, multi_az=False):
    return compute_monthly_cost(get_rds_hourly_price(instance_type, engine, region, multi_az))


def monthly_cost_for_cache(instance_type, region, engine="Redis"):
    return compute_monthly_cost(get_cache_hourly_price(instance_type, region, engine))


def active_provider():
    """Which pricing source is in play — surfaced in the run log."""
    return "Vantage Instances API" if vantage_pricing.is_available() else "AWS Price List API"


def enrich_with_pricing(resources, session=None, vantage_token=None):
    """
    Attach list-price estimates. Never overwrites a cost that came from Cost
    Explorer's per-resource attribution — actuals beat list price.
    """
    if vantage_token:
        vantage_pricing.set_token(vantage_token)
    if session is not None:
        set_session(session)

    for service, items in resources.items():
        for item in items:
            if item.get("type") == "EC2":
                hourly = get_ec2_hourly_price(
                    item.get("instance_type", ""),
                    item.get("region", "us-east-1"),
                    item.get("platform", "Linux"),
                )
                item["hourly_cost_usd"] = hourly

                # A stopped instance is not billed for compute. Quoting its
                # on-demand rate as a monthly cost overstates both the account
                # total and any "terminate this" saving. Its real ongoing cost
                # is the attached EBS volumes, priced on the EBS tab.
                if item.get("state") == "stopped":
                    if not item.get("monthly_cost_usd"):
                        item["monthly_cost_usd"] = 0.0
                        item["cost_source"] = "derived"
                        item["cost_basis"] = {
                            "source": "derived",
                            "formula": "Stopped — no compute charge",
                            "unit_price": None, "unit": "", "provider": "ec2:DescribeInstances",
                            "as_of": "", "note": "Attached EBS volumes are billed separately",
                            "description": ("Stopped — no compute charge; attached EBS "
                                            "volumes are billed separately"),
                        }
                    continue

                if not item.get("monthly_cost_usd"):
                    item["monthly_cost_usd"] = compute_monthly_cost(hourly)
                    item.setdefault("cost_source", "list_price")

            elif item.get("type") == "RDS":
                hourly = get_rds_hourly_price(
                    item.get("instance_type", ""),
                    item.get("engine", "mysql"),
                    item.get("region", "us-east-1"),
                    item.get("multi_az", False),
                )
                item["hourly_cost_usd"] = hourly
                if not item.get("monthly_cost_usd"):
                    item["monthly_cost_usd"] = compute_monthly_cost(hourly)
                    item.setdefault("cost_source", "list_price")

            elif item.get("type") == "ElastiCache":
                hourly = get_cache_hourly_price(
                    item.get("instance_type", ""),
                    item.get("region", "us-east-1"),
                    item.get("engine", "redis"),
                )
                if hourly:
                    nodes = item.get("num_nodes", 1) or 1
                    item["hourly_cost_usd"] = round(hourly * nodes, 4)
                    if not item.get("monthly_cost_usd"):
                        item["monthly_cost_usd"] = compute_monthly_cost(hourly * nodes)
                        item.setdefault("cost_source", "list_price")

    return resources
