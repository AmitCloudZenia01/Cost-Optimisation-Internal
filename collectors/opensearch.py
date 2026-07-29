import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_opensearch_domains(session, regions):
    resources = []
    for region in regions:
        for service in ["opensearch", "es"]:  # try both OpenSearch and Elasticsearch
            try:
                client = session.client(service, region_name=region)
                domains = client.list_domain_names().get("DomainNames", [])
                for d in domains:
                    name = d["DomainName"]
                    try:
                        detail = client.describe_domain(DomainName=name)
                        status = detail.get("DomainStatus", detail.get("DomainStatus", {}))
                        config = client.describe_domain_config(DomainName=name)
                        cluster = status.get("ClusterConfig", {})
                        tags = {}
                        try:
                            tags = {t["Key"]: t["Value"] for t in
                                    client.list_tags(ARN=status.get("ARN", "")).get("TagList", [])}
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass

                        instance_type = cluster.get("InstanceType", "")
                        instance_count = cluster.get("InstanceCount", 1)
                        dedicated_master = cluster.get("DedicatedMasterEnabled", False)
                        master_type = cluster.get("DedicatedMasterType", "") if dedicated_master else ""
                        master_count = cluster.get("DedicatedMasterCount", 0) if dedicated_master else 0

                        ebs = status.get("EBSOptions", {})
                        volume_size = ebs.get("VolumeSize", 0)
                        volume_type = ebs.get("VolumeType", "")

                        resources.append({
                            "type": "OpenSearch",
                            "id": name,
                            "name": name,
                            "region": region,
                            "engine": "opensearch" if service == "opensearch" else "elasticsearch",
                            "engine_version": status.get("EngineVersion", ""),
                            "instance_type": instance_type,
                            "instance_count": instance_count,
                            "dedicated_master": dedicated_master,
                            "master_type": master_type,
                            "master_count": master_count,
                            "volume_size_gb": volume_size,
                            "volume_type": volume_type,
                            "total_storage_gb": volume_size * instance_count,
                            "multi_az": cluster.get("ZoneAwarenessEnabled", False),
                            "endpoint": status.get("Endpoints", {}).get("vpc", status.get("Endpoint", "")),
                            "tags": tags,
                        })
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
                break  # don't try both services if one works
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                continue
    return resources