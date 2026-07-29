import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_ebs_volumes(session, regions):
    resources = []
    from datetime import datetime, timezone
    for region in regions:
        ec2 = session.client("ec2", region_name=region)

        # Pre-fetch all snapshots in this region once (group by volume)
        snap_map = {}
        try:
            snap_pager = ec2.get_paginator("describe_snapshots")
            for spage in snap_pager.paginate(OwnerIds=["self"]):
                for snap in spage["Snapshots"]:
                    vid = snap.get("VolumeId", "")
                    if vid:
                        snap_map.setdefault(vid, []).append(snap)
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for vol in page["Volumes"]:
                tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
                attachments = vol.get("Attachments", [])
                attached_to = attachments[0]["InstanceId"] if attachments else ""

                create_time = vol.get("CreateTime")
                create_date = create_time.strftime("%Y-%m-%d") if create_time else ""
                age_days = (datetime.now(timezone.utc) - create_time).days if create_time else ""

                # Snapshot info
                snaps = snap_map.get(vol["VolumeId"], [])
                snapshot_count = len(snaps)
                latest_snapshot_age = ""
                if snaps:
                    latest = max(snaps, key=lambda s: s.get("StartTime") or datetime.min.replace(tzinfo=timezone.utc))
                    snap_time = latest.get("StartTime")
                    if snap_time:
                        latest_snapshot_age = (datetime.now(timezone.utc) - snap_time).days

                # gp2 → gp3 eligibility. The saving is priced later from the
                # live gp2/gp3 rates for this region by the recommender; the
                # collector only records that the volume qualifies.
                vol_type = vol.get("VolumeType", "")
                gp3_opportunity = "Eligible" if vol_type == "gp2" else ""

                resources.append({
                    "type": "EBS",
                    "id": vol["VolumeId"],
                    "name": tags.get("Name", vol["VolumeId"]),
                    "region": region,
                    "volume_type": vol_type,
                    "size_gb": vol.get("Size", 0),
                    "iops": vol.get("Iops", ""),
                    "throughput_mbps": vol.get("Throughput", ""),
                    "state": vol.get("State", ""),
                    "attached_to": attached_to,
                    "multi_attach": vol.get("MultiAttachEnabled", False),
                    "encrypted": vol.get("Encrypted", False),
                    "kms_key_id": vol.get("KmsKeyId", "").split("/")[-1] if vol.get("KmsKeyId") else "",
                    "create_date": create_date,
                    "age_days": age_days,
                    "snapshot_count": snapshot_count,
                    "latest_snapshot_age_days": latest_snapshot_age,
                    "gp3_opportunity": gp3_opportunity,
                    "tags": tags,
                })
    return resources


def get_ebs_metrics(session, volume_id, region, days=30):
    cw = session.client("cloudwatch", region_name=region)
    dims = [{"Name": "VolumeId", "Value": volume_id}]
    end = utcnow()
    start = end - timedelta(days=days)

    def fetch(metric, stat="Average"):
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/EBS", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=3600, Statistics=[stat],
            )
            vals = [dp[stat] for dp in resp["Datapoints"]]
            return round(statistics.mean(vals), 2) if vals else None
        except Exception:
            return None

    read_ops   = fetch("VolumeReadOps")
    write_ops  = fetch("VolumeWriteOps")
    read_bytes = fetch("VolumeReadBytes")
    write_bytes = fetch("VolumeWriteBytes")
    burst      = fetch("BurstBalance")   # only for gp2/st1/sc1
    queue_depth = fetch("VolumeQueueLength")

    total_ops = (read_ops or 0) + (write_ops or 0)

    return {
        "read_ops_avg": read_ops,
        "write_ops_avg": write_ops,
        "read_bytes_avg": read_bytes,
        "write_bytes_avg": write_bytes,
        "burst_balance_avg": burst,
        "queue_depth_avg": queue_depth,
        "total_ops_avg": round(total_ops, 2),
        "zero_io": total_ops == 0 and read_ops is not None,
    }


def enrich_ebs_with_metrics(session, ebs_resources):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch(r):
        return r["id"], get_ebs_metrics(session, r["id"], r["region"])

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch, r): r["id"] for r in ebs_resources}
        metrics_map = {}
        for future in as_completed(futures):
            vid, m = future.result()
            metrics_map[vid] = m

    for r in ebs_resources:
        r["metrics"] = metrics_map.get(r["id"], {})

    return ebs_resources