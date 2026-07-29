"""
Reserved Instance and Savings Plan inventory.

This is an accuracy control, not a feature.

Two things go wrong without it:

  1. The tool recommends "buy a Reserved Instance" for an instance that is
     already covered by one. The saving is claimed twice — once by the customer
     who already bought it, once by us.

  2. Every saving is quoted against on-demand list price. If the account holds
     commitments, the customer is not paying list price, so those savings are
     overstated — often by 30-40%. We cannot compute the true baseline without
     a CUR, but we can *detect* that the baseline is wrong and say so.

All calls are read-only describes.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from analysis.provenance import gaps
from utils import utcnow


def _record_access_gap(service, region, error):
    text = str(error)
    if "AccessDenied" in text or "UnauthorizedOperation" in text or "not authorized" in text:
        why = f"Permission denied calling {service} in {region}."
        fix = f"Grant read access to {service}."
    else:
        why = f"{service} failed in {region}: {text[:160]}"
        fix = "Re-run; if it persists this service may not be enabled in the region."
    gaps.add(category="Commitments", what=f"{service} inventory ({region})",
             why=why, how_to_fix=fix, region=region,
             impact="Existing commitments may be missed, which would overstate savings.")


def _ec2_reserved(session, region):
    out = []
    try:
        ec2 = session.client("ec2", region_name=region)
        resp = ec2.describe_reserved_instances(
            Filters=[{"Name": "state", "Values": ["active"]}])
        for ri in resp.get("ReservedInstances", []):
            out.append({
                "service": "EC2",
                "region": region,
                "id": ri.get("ReservedInstancesId", ""),
                "instance_type": ri.get("InstanceType", ""),
                "count": ri.get("InstanceCount", 0),
                "scope": ri.get("Scope", ""),
                "availability_zone": ri.get("AvailabilityZone", ""),
                "platform": ri.get("ProductDescription", ""),
                "offering_class": ri.get("OfferingClass", ""),
                "offering_type": ri.get("OfferingType", ""),
                "start": ri.get("Start").isoformat() if ri.get("Start") else "",
                "end": ri.get("End").isoformat() if ri.get("End") else "",
                "state": ri.get("State", ""),
            })
    except Exception as e:
        _record_access_gap("ec2:DescribeReservedInstances", region, e)
    return out


def _rds_reserved(session, region):
    out = []
    try:
        rds = session.client("rds", region_name=region)
        paginator = rds.get_paginator("describe_reserved_db_instances")
        for page in paginator.paginate():
            for ri in page.get("ReservedDBInstances", []):
                if ri.get("State") != "active":
                    continue
                out.append({
                    "service": "RDS",
                    "region": region,
                    "id": ri.get("ReservedDBInstanceId", ""),
                    "instance_type": ri.get("DBInstanceClass", ""),
                    "count": ri.get("DBInstanceCount", 0),
                    "scope": "Region",
                    "availability_zone": "",
                    "platform": ri.get("ProductDescription", ""),
                    "offering_class": "Multi-AZ" if ri.get("MultiAZ") else "Single-AZ",
                    "offering_type": ri.get("OfferingType", ""),
                    "start": ri.get("StartTime").isoformat() if ri.get("StartTime") else "",
                    "end": "",
                    "state": ri.get("State", ""),
                })
    except Exception as e:
        _record_access_gap("rds:DescribeReservedDBInstances", region, e)
    return out


def _elasticache_reserved(session, region):
    out = []
    try:
        ec = session.client("elasticache", region_name=region)
        paginator = ec.get_paginator("describe_reserved_cache_nodes")
        for page in paginator.paginate():
            for ri in page.get("ReservedCacheNodes", []):
                if ri.get("State") != "active":
                    continue
                out.append({
                    "service": "ElastiCache",
                    "region": region,
                    "id": ri.get("ReservedCacheNodeId", ""),
                    "instance_type": ri.get("CacheNodeType", ""),
                    "count": ri.get("CacheNodeCount", 0),
                    "scope": "Region",
                    "availability_zone": "",
                    "platform": ri.get("ProductDescription", ""),
                    "offering_class": "",
                    "offering_type": ri.get("OfferingType", ""),
                    "start": ri.get("StartTime").isoformat() if ri.get("StartTime") else "",
                    "end": "",
                    "state": ri.get("State", ""),
                })
    except Exception as e:
        _record_access_gap("elasticache:DescribeReservedCacheNodes", region, e)
    return out


def _savings_plans(session):
    """Savings Plans are account-wide; the API is global (us-east-1)."""
    out = []
    try:
        sp = session.client("savingsplans", region_name="us-east-1")
        token = None
        while True:
            kwargs = {"states": ["active"], "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = sp.describe_savings_plans(**kwargs)
            for plan in resp.get("savingsPlans", []):
                out.append({
                    "service": "SavingsPlan",
                    "region": plan.get("region", "global") or "global",
                    "id": plan.get("savingsPlanId", ""),
                    "instance_type": plan.get("ec2InstanceFamily", "") or "n/a",
                    "count": "",
                    "scope": plan.get("savingsPlanType", ""),
                    "availability_zone": "",
                    "platform": plan.get("productTypes") and ", ".join(plan["productTypes"]) or "",
                    "offering_class": plan.get("paymentOption", ""),
                    "offering_type": plan.get("termDurationInSeconds", ""),
                    "commitment_hourly": plan.get("commitment", ""),
                    "start": plan.get("start", ""),
                    "end": plan.get("end", ""),
                    "state": plan.get("state", ""),
                })
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        _record_access_gap("savingsplans:DescribeSavingsPlans", "global", e)
    return out


def collect(session, regions):
    """
    Returns {"items": [...], "has_commitments": bool, "by_type": {...}}.

    `has_commitments` is the important flag: when True, every list-price saving
    in the report is known to be overstated and must be labelled accordingly.
    """
    items = []
    tasks = []
    with ThreadPoolExecutor(max_workers=max(1, min(len(regions) * 3, 12))) as ex:
        for region in regions:
            tasks.append(ex.submit(_ec2_reserved, session, region))
            tasks.append(ex.submit(_rds_reserved, session, region))
            tasks.append(ex.submit(_elasticache_reserved, session, region))
        tasks.append(ex.submit(_savings_plans, session))
        for future in as_completed(tasks):
            try:
                items.extend(future.result() or [])
            except Exception:
                pass

    by_type = {}
    for item in items:
        by_type[item["service"]] = by_type.get(item["service"], 0) + 1

    if items:
        gaps.add(
            category="Cost data",
            what="Account holds active commitments",
            why=(f"{len(items)} active Reserved Instance(s)/Savings Plan(s) were "
                 f"found. The account is therefore NOT paying on-demand list "
                 f"price for the covered resources, but without a Cost and Usage "
                 f"Report the true rate cannot be read."),
            how_to_fix=("Enable a Cost and Usage Report so savings are computed "
                        "against actual billed cost."),
            impact=("List-price savings in this report are overstated for any "
                    "resource covered by a commitment."))

    return {"items": items, "has_commitments": bool(items), "by_type": by_type}


# ─── Coverage matching ───────────────────────────────────────────────────────

def _norm_platform(text):
    text = (text or "").lower()
    if "windows" in text:
        return "windows"
    if "rhel" in text or "red hat" in text:
        return "rhel"
    if "suse" in text or "sles" in text:
        return "suse"
    return "linux"


def build_coverage(commitment_items):
    """
    Capacity available per (service, region, instance_type).

    Deliberately conservative: Savings Plans are *dollar* commitments, not
    instance reservations, so they are tracked at family level as "some
    coverage exists" rather than pretending to know which instance they land
    on. Getting that exactly right needs CUR.
    """
    capacity = {}
    sp_families = set()

    for item in commitment_items:
        if item["service"] == "SavingsPlan":
            family = (item.get("instance_type") or "").lower()
            if family and family != "n/a":
                sp_families.add((item.get("region", "global"), family))
            else:
                sp_families.add((item.get("region", "global"), "*"))
            continue
        key = (item["service"], item["region"], item["instance_type"])
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        capacity[key] = capacity.get(key, 0) + count

    return {"reserved": capacity, "savings_plan_families": sp_families}


def is_covered(coverage, service, region, instance_type):
    """
    (covered: bool, reason: str) for one resource.

    Consumes reserved capacity as it matches, so N reservations cover only the
    first N instances rather than every instance of that type.
    """
    if not coverage:
        return False, ""

    key = (service, region, instance_type)
    remaining = coverage["reserved"].get(key, 0)
    if remaining > 0:
        coverage["reserved"][key] = remaining - 1
        return True, (f"Covered by an existing Reserved Instance "
                      f"({remaining} of this type remaining before this match)")

    family = (instance_type or "").split(".")[0].lower()
    for sp_region, sp_family in coverage["savings_plan_families"]:
        region_ok = sp_region in ("global", "", region)
        family_ok = sp_family in ("*", family)
        if region_ok and family_ok:
            return True, ("An active Savings Plan may already cover this "
                          "instance family — verify coverage before purchasing more")
    return False, ""
