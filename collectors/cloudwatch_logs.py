import boto3
# timezone is required by the fromtimestamp() call below. It was missing, and
# the surrounding `except Exception: pass` swallowed the NameError — so every
# log group's last-event date came back blank in every region, silently.
from datetime import datetime, timedelta, timezone
from collectors import api_errors


def get_log_groups(session, regions):
    resources = []
    for region in regions:
        client = session.client("logs", region_name=region)
        try:
            paginator = client.get_paginator("describe_log_groups")
            for page in paginator.paginate():
                for lg in page["logGroups"]:
                    stored_bytes = lg.get("storedBytes", 0)
                    retention = lg.get("retentionInDays")

                    # Check last ingestion time
                    last_event = ""
                    try:
                        streams = client.describe_log_streams(
                            logGroupName=lg["logGroupName"],
                            orderBy="LastEventTime",
                            descending=True,
                            limit=1,
                        )
                        if streams["logStreams"]:
                            ts = streams["logStreams"][0].get("lastEventTimestamp")
                            if ts:
                                last_event = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # Subscription filters count
                    sub_filter_count = 0
                    try:
                        subs = client.describe_subscription_filters(logGroupName=lg["logGroupName"])
                        sub_filter_count = len(subs.get("subscriptionFilters", []))
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    resources.append({
                        "type": "CWLogGroup",
                        "id": lg["logGroupName"],
                        "name": lg["logGroupName"],
                        "region": region,
                        "stored_bytes": stored_bytes,
                        "stored_gb": round(stored_bytes / (1024 ** 3), 4),
                        "retention_days": retention,
                        "metric_filter_count": lg.get("metricFilterCount", 0),
                        "subscription_filter_count": sub_filter_count,
                        "last_event_date": last_event,
                        "tags": {},
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return sorted(resources, key=lambda x: x["stored_bytes"], reverse=True)


def get_elastic_ips(session, regions):
    resources = []
    for region in regions:
        ec2 = session.client("ec2", region_name=region)
        try:
            resp = ec2.describe_addresses()
            for eip in resp["Addresses"]:
                tags = {t["Key"]: t["Value"] for t in eip.get("Tags", [])}
                resources.append({
                    "type": "ElasticIP",
                    "id": eip.get("AllocationId", eip.get("PublicIp", "")),
                    "name": tags.get("Name", eip.get("PublicIp", "")),
                    "region": region,
                    "public_ip": eip.get("PublicIp", ""),
                    "attached_to": eip.get("InstanceId") or eip.get("NetworkInterfaceId") or "",
                    "association_id": eip.get("AssociationId", ""),
                    "unattached": not bool(eip.get("AssociationId")),
                    "tags": tags,
                })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources
