"""
EBS snapshots — collection and cost.

Snapshots were previously counted per volume but never collected as resources
and never priced, so their spend sat unexplained inside the "EC2 - Other" line
item. They are frequently a large share of it: nothing deletes them by default,
so they accumulate for years after the volume they came from is gone.

Snapshots are billed on *incremental* stored data, which AWS does not expose
per snapshot. Where the incremental size is unavailable we say so rather than
billing the full volume size, which would badly overstate the total.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from analysis.provenance import Basis, DERIVED, gaps, set_cost
from collectors import aws_pricing as ap
from utils import utcnow_aware


def get_snapshots(session, regions, account_id=None):
    """Owned EBS snapshots across regions. Read-only."""
    resources = []

    def per_region(region):
        found = []
        try:
            ec2 = session.client("ec2", region_name=region)
            paginator = ec2.get_paginator("describe_snapshots")
            for page in paginator.paginate(OwnerIds=["self"]):
                for snap in page.get("Snapshots", []):
                    start = snap.get("StartTime")
                    age_days = ""
                    if start:
                        try:
                            age_days = (utcnow_aware() - start).days
                        except Exception:
                            pass
                    tags = {t["Key"]: t["Value"] for t in snap.get("Tags", [])}
                    volume_id = snap.get("VolumeId", "")
                    found.append({
                        "type": "EBSSnapshot",
                        "id": snap["SnapshotId"],
                        "name": tags.get("Name", snap["SnapshotId"]),
                        "region": region,
                        "volume_id": volume_id,
                        "volume_size_gb": snap.get("VolumeSize", 0),
                        "state": snap.get("State", ""),
                        "encrypted": snap.get("Encrypted", False),
                        "description": (snap.get("Description") or "")[:120],
                        "start_time": start.isoformat() if start else "",
                        "age_days": age_days,
                        "storage_tier": snap.get("StorageTier", "standard"),
                        # True when the source volume no longer exists — such a
                        # snapshot cannot be an incremental of anything live.
                        "orphaned": False,
                        "tags": tags,
                    })
        except Exception as e:
            text = str(e)
            denied = "AccessDenied" in text or "UnauthorizedOperation" in text
            gaps.add(
                category="Collection",
                what=f"EBS snapshots ({region})",
                why=("Permission denied listing snapshots." if denied
                     else f"describe_snapshots failed: {text[:150]}"),
                how_to_fix="Grant ec2:DescribeSnapshots.",
                region=region,
                impact="Snapshot storage cost is missing, understating EC2 - Other spend.")
        return found

    with ThreadPoolExecutor(max_workers=max(1, min(len(regions), 8))) as ex:
        futures = [ex.submit(per_region, r) for r in regions]
        for future in as_completed(futures):
            try:
                resources.extend(future.result() or [])
            except Exception:
                pass

    return resources


def mark_orphans(snapshots, volumes):
    """Flag snapshots whose source volume no longer exists."""
    live = {v.get("id") for v in volumes or []}
    for snap in snapshots:
        vid = snap.get("volume_id")
        snap["orphaned"] = bool(vid) and vid not in live
    return snapshots


def price_snapshots(snapshots):
    """
    Attach cost using the live per-region snapshot rate.

    AWS bills only the incremental blocks a snapshot holds, and there is no API
    that reports that per snapshot. We therefore price against the source
    volume size and label it clearly as an upper bound — the alternative,
    silently presenting it as exact, is the kind of confident-but-wrong number
    this codebase exists to avoid.
    """
    priced = 0
    for snap in snapshots:
        region = snap.get("region", "us-east-1")
        archive = str(snap.get("storage_tier", "")).lower() == "archive"
        rate = ap.ebs_snapshot_gb_month(region, archive=archive)
        size = snap.get("volume_size_gb") or 0
        if not rate or not size:
            set_cost(snap, None, None)
            continue
        set_cost(snap, rate.amount * float(size), Basis(
            DERIVED,
            formula=(f"{float(size):,.0f} GB source volume x "
                     f"${rate.amount:,.4f}/GB-month"),
            unit_price=rate.amount, unit="GB-month", provider=rate.source,
            note=("UPPER BOUND — AWS bills only incremental blocks, which no API "
                  "exposes per snapshot. Actual cost is lower where snapshots "
                  "share unchanged data.")))
        priced += 1

    if snapshots and priced:
        gaps.add(
            category="Pricing",
            what="EBS snapshot cost is an upper bound",
            why=("Snapshots bill on incremental stored data. AWS exposes no "
                 "per-snapshot incremental size, so cost is computed from the "
                 "source volume size."),
            how_to_fix=("Enable a Cost and Usage Report — it reports actual "
                        "snapshot usage per resource."),
            resource_type="EBSSnapshot",
            impact="Snapshot cost shown is a ceiling, not the billed amount.")
    return snapshots
