import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console

from utils import utcnow
from analysis.provenance import gaps
from collectors.ssm_inventory import enrich_ec2_with_graviton_signals
from collectors.ebs import get_ebs_volumes, enrich_ebs_with_metrics
from collectors.elb import get_load_balancers
from collectors.nat_gateway import get_nat_gateways
from collectors.transfer_family import get_transfer_servers
from collectors.cloudwatch_logs import get_log_groups, get_elastic_ips
from collectors.waf_kms_misc import (
    get_waf_webacls, get_kms_keys, get_secrets,
    get_ecr_repositories, get_route53_zones, get_codebuild_projects,
)
from collectors.dynamodb  import get_dynamodb_tables
from collectors.opensearch import get_opensearch_domains
from collectors import api_errors
from collectors.additional import (
    get_ecs_services, get_api_gateways, get_sqs_queues,
    get_sns_topics, get_efs_filesystems, get_redshift_clusters,
    get_kinesis_streams, get_msk_clusters,
)

console = Console()


def _safe(fn, *args, **kwargs):
    """
    Run a collector, converting failure into a recorded gap rather than silence.

    Previously a permissions failure and "this account has none of these"
    produced the same result — an empty list and an empty tab. That understates
    the account without saying so, which is an accuracy problem, not a
    cosmetic one.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        text = str(e)
        denied = any(k in text for k in
                     ("AccessDenied", "UnauthorizedOperation", "not authorized",
                      "AuthorizationError", "AccessDeniedException"))
        console.print(f"[yellow]  warn: {fn.__name__} — {text[:120]}[/yellow]")
        gaps.add(
            category="Collection",
            what=f"{fn.__name__}",
            why=(f"Permission denied — the credentials cannot list these resources."
                 if denied else f"Collector failed: {text[:180]}"),
            how_to_fix=("Grant the matching read-only permission and re-run."
                        if denied else "Re-run; if it persists, report the error."),
            impact=("These resources are missing from the report entirely, so "
                    "spend and savings are understated."))
        return []


def get_ec2_instances(session, regions):
    resources = []
    for region in regions:
        ec2 = session.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
        ):
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    iam_role = ""
                    if inst.get("IamInstanceProfile"):
                        arn = inst["IamInstanceProfile"].get("Arn", "")
                        iam_role = arn.split("/")[-1] if arn else ""
                    launch_time = inst.get("LaunchTime")
                    launch_time_str = launch_time.isoformat() if launch_time else ""
                    from datetime import datetime, timezone
                    age_days = ""
                    if launch_time:
                        try:
                            age_days = (datetime.now(timezone.utc) - launch_time).days
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass
                    resources.append({
                        "type": "EC2",
                        "id": inst["InstanceId"],
                        "name": tags.get("Name", inst["InstanceId"]),
                        "region": region,
                        "instance_type": inst.get("InstanceType", ""),
                        "state": inst["State"]["Name"],
                        "platform": inst.get("Platform", "Linux"),
                        "image_id": inst.get("ImageId", ""),
                        "launch_time": launch_time_str,
                        "age_days": age_days,
                        # Network
                        "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
                        "vpc_id": inst.get("VpcId", ""),
                        "subnet_id": inst.get("SubnetId", ""),
                        "private_ip": inst.get("PrivateIpAddress", ""),
                        "public_ip": inst.get("PublicIpAddress", ""),
                        # Identity
                        "iam_role": iam_role,
                        "key_name": inst.get("KeyName", ""),
                        "security_groups": ", ".join(sg.get("GroupName", "") for sg in inst.get("SecurityGroups", [])),
                        # Compute
                        "ebs_volume_count": len(inst.get("BlockDeviceMappings", [])),
                        "auto_scaling_group": tags.get("aws:autoscaling:groupName", ""),
                        "tags": tags,
                        # Populated later
                        "ami_architecture": "unknown",
                        "ssm_managed": False,
                        "x86_only_software": [],
                        "arm_verify_software": [],
                        "ssm_app_count": 0,
                    })
    return resources


def get_rds_instances(session, regions):
    resources = []
    for region in regions:
        rds = session.client("rds", region_name=region)
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
                # Latest snapshot age
                latest_snapshot_age = ""
                try:
                    snaps = rds.describe_db_snapshots(
                        DBInstanceIdentifier=db["DBInstanceIdentifier"],
                        SnapshotType="automated",
                    )["DBSnapshots"]
                    if snaps:
                        from datetime import datetime, timezone
                        latest = max(snaps, key=lambda s: s.get("SnapshotCreateTime") or datetime.min.replace(tzinfo=timezone.utc))
                        snap_time = latest.get("SnapshotCreateTime")
                        if snap_time:
                            latest_snapshot_age = (datetime.now(timezone.utc) - snap_time).days
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass
                resources.append({
                    "type": "RDS",
                    "id": db["DBInstanceIdentifier"],
                    "name": db["DBInstanceIdentifier"],
                    "region": region,
                    "instance_type": db.get("DBInstanceClass", ""),
                    "engine": db.get("Engine", ""),
                    "engine_version": db.get("EngineVersion", ""),
                    "state": db.get("DBInstanceStatus", ""),
                    "multi_az": db.get("MultiAZ", False),
                    "storage_gb": db.get("AllocatedStorage", 0),
                    "storage_type": db.get("StorageType", ""),
                    # Config & compliance
                    "vpc_id": db.get("DBSubnetGroup", {}).get("VpcId", ""),
                    "publicly_accessible": db.get("PubliclyAccessible", False),
                    "deletion_protection": db.get("DeletionProtection", False),
                    "backup_retention_days": db.get("BackupRetentionPeriod", 0),
                    "backup_window": db.get("PreferredBackupWindow", ""),
                    "maintenance_window": db.get("PreferredMaintenanceWindow", ""),
                    "auto_minor_version_upgrade": db.get("AutoMinorVersionUpgrade", False),
                    "performance_insights_enabled": db.get("PerformanceInsightsEnabled", False),
                    "read_replica_count": len(db.get("ReadReplicaDBInstanceIdentifiers", [])),
                    "ca_certificate": db.get("CACertificateIdentifier", ""),
                    "iops": db.get("Iops", ""),
                    "latest_snapshot_age_days": latest_snapshot_age,
                    "tags": tags,
                })
    return resources


def get_lambda_functions(session, regions):
    resources = []
    for region in regions:
        lam = session.client("lambda", region_name=region)
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                tags = {}
                try:
                    tags = lam.list_tags(Resource=fn["FunctionArn"]).get("Tags", {})
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass
                reserved_concurrency = None
                try:
                    cc = lam.get_function_concurrency(FunctionName=fn["FunctionName"])
                    reserved_concurrency = cc.get("ReservedConcurrentExecutions")
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass
                vpc_id = fn.get("VpcConfig", {}).get("VpcId", "")
                resources.append({
                    "type": "Lambda",
                    "id": fn["FunctionName"],
                    "name": fn["FunctionName"],
                    "region": region,
                    "runtime": fn.get("Runtime", ""),
                    "memory_mb": fn.get("MemorySize", 128),
                    "timeout_s": fn.get("Timeout", 3),
                    "last_modified": fn.get("LastModified", ""),
                    "code_size_bytes": fn.get("CodeSize", 0),
                    # Architecture & config
                    "architecture": fn.get("Architectures", ["x86_64"])[0],
                    "vpc_config": bool(vpc_id),
                    "vpc_id": vpc_id,
                    "layers_count": len(fn.get("Layers", [])),
                    "ephemeral_storage_mb": fn.get("EphemeralStorage", {}).get("Size", 512),
                    "reserved_concurrency": reserved_concurrency,
                    "description": fn.get("Description", ""),
                    "tags": tags,
                })
    return resources


def get_s3_buckets(session):
    s3 = session.client("s3", region_name="us-east-1")
    resources = []

    # S3 daily storage metrics are published in the BUCKET's own region, not in
    # us-east-1. Querying a single us-east-1 CloudWatch client returned no
    # datapoints for every bucket outside Virginia, so their size read as 0 and
    # their cost as $0.00. One client per region, created lazily.
    _cw_clients = {}

    def cw_for(region):
        if region not in _cw_clients:
            _cw_clients[region] = session.client("cloudwatch", region_name=region)
        return _cw_clients[region]

    from datetime import timedelta
    end = utcnow()
    # S3 publishes storage metrics once a day; a 2-day window can miss the most
    # recent publication entirely for a bucket in a lagging region.
    start = end - timedelta(days=4)

    buckets = s3.list_buckets().get("Buckets", [])
    for bucket in buckets:
        name = bucket["Name"]
        region = "us-east-1"
        try:
            loc = s3.get_bucket_location(Bucket=name)
            region = loc.get("LocationConstraint") or "us-east-1"
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
        cw = cw_for(region)

        tags = {}
        try:
            tag_resp = s3.get_bucket_tagging(Bucket=name)
            tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Storage sizes by class
        size_bytes = 0
        ia_size_bytes = 0
        glacier_size_bytes = 0
        object_count = 0
        for storage_type, key in [
            ("StandardStorage",    "size"),
            ("StandardIAStorage",  "ia"),
            ("GlacierStorage",     "glacier"),
            ("AllStorageTypes",    "objects"),
        ]:
            try:
                metric = "BucketSizeBytes" if key != "objects" else "NumberOfObjects"
                resp = cw.get_metric_statistics(
                    Namespace="AWS/S3", MetricName=metric,
                    Dimensions=[
                        {"Name": "BucketName", "Value": name},
                        {"Name": "StorageType", "Value": storage_type},
                    ],
                    StartTime=start, EndTime=end, Period=86400, Statistics=["Average"],
                )
                if resp["Datapoints"]:
                    # CloudWatch does not guarantee datapoint order; take the
                    # most recent by timestamp rather than whatever is last.
                    latest = max(resp["Datapoints"], key=lambda d: d["Timestamp"])
                    val = latest["Average"]
                    if key == "size":    size_bytes    = int(val)
                    elif key == "ia":   ia_size_bytes  = int(val)
                    elif key == "glacier": glacier_size_bytes = int(val)
                    else:               object_count   = int(val)
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                pass

        # Versioning
        versioning = "Disabled"
        try:
            versioning = s3.get_bucket_versioning(Bucket=name).get("Status") or "Disabled"
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Lifecycle rules
        lifecycle_rules_count = 0
        try:
            lifecycle_rules_count = len(s3.get_bucket_lifecycle_configuration(Bucket=name).get("Rules", []))
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Public access block
        public_access_blocked = False
        try:
            pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            public_access_blocked = all([
                pab.get("BlockPublicAcls"), pab.get("IgnorePublicAcls"),
                pab.get("BlockPublicPolicy"), pab.get("RestrictPublicBuckets"),
            ])
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Replication
        replication_enabled = False
        try:
            s3.get_bucket_replication(Bucket=name)
            replication_enabled = True
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Encryption
        encryption = "None"
        try:
            enc = s3.get_bucket_encryption(Bucket=name)
            rules = enc.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if rules:
                algo = rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm", "")
                encryption = algo
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        resources.append({
            "type": "S3",
            "id": name,
            "name": name,
            "region": region,
            "size_gb": round(size_bytes / (1024 ** 3), 2),
            "ia_size_gb": round(ia_size_bytes / (1024 ** 3), 4),
            "glacier_size_gb": round(glacier_size_bytes / (1024 ** 3), 4),
            "object_count": object_count,
            "versioning": versioning,
            "lifecycle_rules_count": lifecycle_rules_count,
            "public_access_blocked": public_access_blocked,
            "replication_enabled": replication_enabled,
            "encryption": encryption,
            "tags": tags,
        })

    return resources


def get_eks_clusters(session, regions):
    resources = []
    for region in regions:
        eks = session.client("eks", region_name=region)
        try:
            clusters = eks.list_clusters().get("clusters", [])
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            continue
        for cluster_name in clusters:
            try:
                cluster = eks.describe_cluster(name=cluster_name)["cluster"]
                tags = cluster.get("tags", {})

                # Count add-ons
                addon_count = 0
                addon_names = ""
                try:
                    addons = eks.list_addons(clusterName=cluster_name).get("addons", [])
                    addon_count = len(addons)
                    addon_names = ", ".join(addons)
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

                # Networking
                vpc_id = cluster.get("resourcesVpcConfig", {}).get("vpcId", "")
                k8s_network = cluster.get("kubernetesNetworkConfig", {})

                resources.append({
                    "type": "EKS",
                    "id": cluster_name,
                    "name": cluster_name,
                    "region": region,
                    "k8s_version": cluster.get("version", ""),
                    "status": cluster.get("status", ""),
                    "endpoint": cluster.get("endpoint", ""),
                    "vpc_id": vpc_id,
                    "addon_count": addon_count,
                    "addon_names": addon_names,
                    "logging_enabled": bool(cluster.get("logging", {}).get("clusterLogging")),
                    "platform_version": cluster.get("platformVersion", ""),
                    "created": cluster.get("createdAt", "").isoformat() if cluster.get("createdAt") else "",
                    # Cost is resolved live per region by collectors.service_costs
                    "tags": tags,
                })

                # Node groups
                ngs = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
                for ng_name in ngs:
                    ng = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)["nodegroup"]
                    resources.append({
                        "type": "EKS_NodeGroup",
                        "id": f"{cluster_name}/{ng_name}",
                        "name": ng_name,
                        "cluster": cluster_name,
                        "region": region,
                        "instance_type": ng.get("instanceTypes", [""])[0],
                        "capacity_type": ng.get("capacityType", "ON_DEMAND"),
                        "ami_type": ng.get("amiType", ""),
                        "desired": ng.get("scalingConfig", {}).get("desiredSize", 0),
                        "min": ng.get("scalingConfig", {}).get("minSize", 0),
                        "max": ng.get("scalingConfig", {}).get("maxSize", 0),
                        "disk_size_gb": ng.get("diskSize", 0),
                        "status": ng.get("status", ""),
                        "release_version": ng.get("releaseVersion", ""),
                        "tags": ng.get("tags", {}),
                    })
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                continue
    return resources


def get_elasticache_clusters(session, regions):
    resources = []
    for region in regions:
        ec = session.client("elasticache", region_name=region)
        try:
            paginator = ec.get_paginator("describe_cache_clusters")
            for page in paginator.paginate(ShowCacheNodeInfo=True):
                for cluster in page["CacheClusters"]:
                    tags = {}
                    try:
                        arn = cluster.get("ARN", "")
                        if arn:
                            tags = {t["Key"]: t["Value"] for t in ec.list_tags_for_resource(ResourceName=arn).get("TagList", [])}
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # Replication group for cluster mode info
                    cluster_mode = "Disabled"
                    replication_group_id = cluster.get("ReplicationGroupId", "")
                    if replication_group_id:
                        try:
                            rg = ec.describe_replication_groups(ReplicationGroupId=replication_group_id)["ReplicationGroups"][0]
                            cluster_mode = "Enabled" if rg.get("ClusterEnabled") else "Disabled"
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass

                    resources.append({
                        "type": "ElastiCache",
                        "id": cluster["CacheClusterId"],
                        "name": cluster["CacheClusterId"],
                        "region": region,
                        "instance_type": cluster.get("CacheNodeType", ""),
                        "engine": cluster.get("Engine", ""),
                        "engine_version": cluster.get("EngineVersion", ""),
                        "num_nodes": cluster.get("NumCacheNodes", 1),
                        "status": cluster.get("CacheClusterStatus", ""),
                        # Security & config
                        "cluster_mode": cluster_mode,
                        "replication_group_id": replication_group_id,
                        "auth_token_enabled": cluster.get("AuthTokenEnabled", False),
                        "transit_encryption": cluster.get("TransitEncryptionEnabled", False),
                        "at_rest_encryption": cluster.get("AtRestEncryptionEnabled", False),
                        "backup_retention_days": cluster.get("SnapshotRetentionLimit", 0),
                        "maintenance_window": cluster.get("PreferredMaintenanceWindow", ""),
                        "parameter_group": cluster.get("CacheParameterGroup", {}).get("CacheParameterGroupName", ""),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            continue
    return resources


def get_cloudfront_distributions(session):
    cf = session.client("cloudfront", region_name="us-east-1")
    resources = []
    try:
        paginator = cf.get_paginator("list_distributions")
        for page in paginator.paginate():
            items = page.get("DistributionList", {}).get("Items", [])
            for dist in items:
                tags = {}
                try:
                    tags = {t["Key"]: t["Value"] for t in cf.list_tags_for_resource(Resource=dist["ARN"]).get("Tags", {}).get("Items", [])}
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

                # Logging from distribution config
                logging_enabled = False
                try:
                    cfg = cf.get_distribution_config(Id=dist["Id"])["DistributionConfig"]
                    logging_enabled = cfg.get("Logging", {}).get("Enabled", False)
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

                origins = dist.get("Origins", {})
                origin_domains = ", ".join(o.get("DomainName", "") for o in origins.get("Items", []))

                resources.append({
                    "type": "CloudFront",
                    "id": dist["Id"],
                    "name": dist.get("Comment") or dist["Id"],
                    "region": "global",
                    "domain": dist.get("DomainName", ""),
                    "status": dist.get("Status", ""),
                    "enabled": dist.get("Enabled", True),
                    "price_class": dist.get("PriceClass", ""),
                    "http_version": dist.get("HttpVersion", ""),
                    "ipv6_enabled": dist.get("IsIPV6Enabled", False),
                    "origins_count": origins.get("Quantity", 0),
                    "origin_domains": origin_domains,
                    "behaviors_count": 1 + dist.get("CacheBehaviors", {}).get("Quantity", 0),
                    "custom_domains": dist.get("Aliases", {}).get("Quantity", 0),
                    "waf_attached": "Yes" if dist.get("WebACLId") else "No",
                    "logging_enabled": logging_enabled,
                    "tags": tags,
                })
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass
    return resources


# ─── Service Registry ────────────────────────────────────────────────────────
#
# Each entry defines:
#   ce_triggers  : Cost Explorer service display names that indicate this
#                  service is active in the account. The collector only runs
#                  if at least one trigger appears in the billing data.
#   collector    : callable(session, regions) → list[resource_dict]
#   post_enrich  : optional callable(session, resources) → list[resource_dict]
#                  Run after collection for enrichment (metrics, SSM scan, etc.)
#   always_run   : if True, run even with no CE spend (e.g. ElasticIPs — free
#                  resources can still exist and cost money when unattached)
#
# To add a new service: add one entry here + a sheet column/row builder.
# Nothing else needs to change.
#
SERVICE_REGISTRY = [
    # ── Compute ───────────────────────────────────────────────────────────
    {
        "key": "EC2",
        "ce_triggers": ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other"],
        "collector":   lambda s, r: _safe(get_ec2_instances, s, r),
        "post_enrich": lambda s, res: _safe(enrich_ec2_with_graviton_signals, s, res),
        "always_run":  False,
    },
    {
        "key": "EKS",
        "ce_triggers": [
            "Amazon Elastic Container Service for Kubernetes",
            "Amazon Elastic Kubernetes Service",
        ],
        "collector":   lambda s, r: _safe(get_eks_clusters, s, r),
        "always_run":  False,
    },
    {
        "key": "Lambda",
        "ce_triggers": ["AWS Lambda"],
        "collector":   lambda s, r: _safe(get_lambda_functions, s, r),
        "always_run":  False,
    },

    # ── Databases ─────────────────────────────────────────────────────────
    {
        "key": "RDS",
        "ce_triggers": ["Amazon Relational Database Service", "Amazon Aurora"],
        "collector":   lambda s, r: _safe(get_rds_instances, s, r),
        "always_run":  False,
    },
    {
        "key": "ElastiCache",
        "ce_triggers": ["Amazon ElastiCache"],
        "collector":   lambda s, r: _safe(get_elasticache_clusters, s, r),
        "always_run":  False,
    },

    # ── Storage ───────────────────────────────────────────────────────────
    {
        "key": "S3",
        "ce_triggers": ["Amazon Simple Storage Service"],
        "collector":   lambda s, r: _safe(get_s3_buckets, s),
        "always_run":  False,
    },
    {
        "key": "EBS",
        "ce_triggers": ["EC2 - Other", "Amazon Elastic Compute Cloud - Compute"],
        "collector":   lambda s, r: _safe(get_ebs_volumes, s, r),
        "post_enrich": lambda s, res: _safe(enrich_ebs_with_metrics, s, res),
        "always_run":  False,
    },

    # ── Networking ────────────────────────────────────────────────────────
    {
        "key": "ELB",
        "ce_triggers": ["Amazon Elastic Load Balancing"],
        "collector":   lambda s, r: _safe(get_load_balancers, s, r),
        "always_run":  False,
    },
    {
        "key": "NATGateway",
        "ce_triggers": ["Amazon Virtual Private Cloud", "EC2 - Other"],
        "collector":   lambda s, r: _safe(get_nat_gateways, s, r),
        "always_run":  False,
    },
    {
        "key": "ElasticIPs",
        "ce_triggers": ["EC2 - Other"],
        "collector":   lambda s, r: _safe(get_elastic_ips, s, r),
        "always_run":  True,   # unattached EIPs cost money even without a CE line item
    },
    {
        "key": "CloudFront",
        "ce_triggers": ["Amazon CloudFront"],
        "collector":   lambda s, r: _safe(get_cloudfront_distributions, s),
        "always_run":  False,
    },

    # ── Transfer & Integration ────────────────────────────────────────────
    {
        "key": "TransferFamily",
        "ce_triggers": ["AWS Transfer Family"],
        "collector":   lambda s, r: _safe(get_transfer_servers, s, r),
        "always_run":  False,
    },

    # ── Security & Compliance ─────────────────────────────────────────────
    {
        "key": "WAF",
        "ce_triggers": ["AWS WAF"],
        "collector":   lambda s, r: _safe(get_waf_webacls, s),
        "always_run":  False,
    },
    {
        "key": "KMS",
        "ce_triggers": ["AWS Key Management Service"],
        "collector":   lambda s, r: _safe(get_kms_keys, s, r),
        "always_run":  False,
    },
    {
        "key": "SecretsManager",
        "ce_triggers": ["AWS Secrets Manager"],
        "collector":   lambda s, r: _safe(get_secrets, s, r),
        "always_run":  False,
    },

    # ── DevOps & Containers ───────────────────────────────────────────────
    {
        "key": "ECR",
        "ce_triggers": [
            "Amazon EC2 Container Registry (ECR)",
            "Amazon ECR Public",
        ],
        "collector":   lambda s, r: _safe(get_ecr_repositories, s, r),
        "always_run":  False,
    },
    {
        "key": "CodeBuild",
        "ce_triggers": ["AWS CodeBuild", "CodeBuild"],
        "collector":   lambda s, r: _safe(get_codebuild_projects, s, r),
        "always_run":  False,
    },

    # ── Observability ─────────────────────────────────────────────────────
    {
        "key": "CWLogGroups",
        "ce_triggers": ["AmazonCloudWatch", "Amazon CloudWatch"],
        "collector":   lambda s, r: _safe(get_log_groups, s, r),
        "always_run":  False,
    },

    # ── DNS ───────────────────────────────────────────────────────────────
    {
        "key": "Route53",
        "ce_triggers": ["Amazon Route 53", "Amazon Registrar"],
        "collector":   lambda s, r: _safe(get_route53_zones, s),
        "always_run":  False,
    },

    # ── Databases (additional) ────────────────────────────────────────────
    {
        "key": "DynamoDB",
        "ce_triggers": ["Amazon DynamoDB"],
        "collector":   lambda s, r: _safe(get_dynamodb_tables, s, r),
        "always_run":  False,
    },
    {
        "key": "OpenSearch",
        "ce_triggers": ["Amazon OpenSearch Service",
                        "Amazon Elasticsearch Service", "Amazon ES"],
        "collector":   lambda s, r: _safe(get_opensearch_domains, s, r),
        "always_run":  False,
    },
    {
        "key": "Redshift",
        "ce_triggers": ["Amazon Redshift"],
        "collector":   lambda s, r: _safe(get_redshift_clusters, s, r),
        "always_run":  False,
    },

    # ── Containers ────────────────────────────────────────────────────────
    {
        "key": "ECS",
        "ce_triggers": ["Amazon Elastic Container Service", "AWS Fargate"],
        "collector":   lambda s, r: _safe(get_ecs_services, s, r),
        "always_run":  False,
    },

    # ── Messaging & Streaming ─────────────────────────────────────────────
    {
        "key": "SQS",
        "ce_triggers": ["Amazon Simple Queue Service"],
        "collector":   lambda s, r: _safe(get_sqs_queues, s, r),
        "always_run":  False,
    },
    {
        "key": "SNS",
        "ce_triggers": ["Amazon Simple Notification Service"],
        "collector":   lambda s, r: _safe(get_sns_topics, s, r),
        "always_run":  False,
    },
    {
        "key": "Kinesis",
        "ce_triggers": ["Amazon Kinesis", "Amazon Kinesis Streams",
                        "Amazon Kinesis Firehose"],
        "collector":   lambda s, r: _safe(get_kinesis_streams, s, r),
        "always_run":  False,
    },
    {
        "key": "MSK",
        "ce_triggers": ["Amazon Managed Streaming for Apache Kafka",
                        "Amazon MSK"],
        "collector":   lambda s, r: _safe(get_msk_clusters, s, r),
        "always_run":  False,
    },

    # ── Storage (additional) ──────────────────────────────────────────────
    {
        "key": "EFS",
        "ce_triggers": ["Amazon Elastic File System"],
        "collector":   lambda s, r: _safe(get_efs_filesystems, s, r),
        "always_run":  False,
    },

    # ── API ───────────────────────────────────────────────────────────────
    {
        "key": "APIGateway",
        "ce_triggers": ["Amazon API Gateway"],
        "collector":   lambda s, r: _safe(get_api_gateways, s, r),
        "always_run":  False,
    },
]


def collect_all(session, regions, monthly_costs=None):
    """
    Generic resource collection pipeline:

    Step 1 — Discovery (Resource Groups Tagging API)
        Finds ALL resources across ALL services in the account.
        No service-specific code. Works for any AWS account.

    Step 2 — Cost per resource (Cost Explorer RESOURCE_ID grouping)
        Attaches actual monthly cost to each discovered resource.
        Falls back gracefully if resource-level CE is not enabled.

    Step 3 — EC2 Graviton enrichment (SSM + AMI architecture)
        Only runs for EC2 instances. Adds ARM compatibility signals.

    Step 4 — SERVICE_REGISTRY fallback enrichment
        For resources that RGTA misses (untagged, global, or edge cases),
        the legacy service-specific collectors fill gaps.
    """
    from collectors.discovery import discover_all
    from collectors.cost_per_resource import get_cost_by_resource, enrich_resources_with_cost

    # ── Step 1: Generic discovery ─────────────────────────────────────────
    console.print("  Discovering resources via Resource Groups Tagging API...")
    discovered = discover_all(session, regions)
    total_discovered = sum(len(v) for v in discovered.values())
    console.print(f"  [green]✓[/green] {total_discovered} tagged resources found across "
                  f"{len(discovered)} service types")

    # ── Step 2: Cost per resource ─────────────────────────────────────────
    # Fetch now, apply AFTER the service collectors run. Applying it here would
    # be pointless: step 3 replaces each collected list wholesale, which used to
    # throw this attribution away for every service that has a collector.
    console.print("  Fetching per-resource cost attribution from Cost Explorer...")
    cost_map = _safe(get_cost_by_resource, session, 30) or {}
    if cost_map:
        console.print(f"  [green]✓[/green] Cost data returned for {len(cost_map)} resources")
    else:
        console.print("  [yellow]  Resource-level cost attribution not available — using list prices[/yellow]")

    # ── Step 3: Run SERVICE_REGISTRY collectors for richer data ──────────
    # RGTA gives us ARN + tags, but service-specific APIs give richer fields
    # (instance type, engine version, state, etc.). Run them to enrich/fill gaps.
    ce_with_spend = {row["service"] for row in (monthly_costs or []) if row.get("cost", 0) > 0}

    registry_tasks = {}
    for entry in SERVICE_REGISTRY:
        key = entry["key"]
        if entry.get("always_run") or any(t in ce_with_spend for t in entry.get("ce_triggers", [])):
            registry_tasks[key] = entry["collector"]

    console.print(f"  Running {len(registry_tasks)} service-specific enrichment collectors...")
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn, session, regions): key
                   for key, fn in registry_tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            result = future.result() or []
            if result:
                # Prefer service-specific data (richer), but carry across the
                # fields RGTA discovery supplies and the collectors do not —
                # notably tags and any cost already attached.
                prior = {r["id"]: r for r in discovered.get(key, [])}
                for r in result:
                    old = prior.get(r["id"])
                    if not old:
                        continue
                    if not r.get("tags"):
                        r["tags"] = old.get("tags", {})
                    if not r.get("arn") and old.get("arn"):
                        r["arn"] = old["arn"]
                    if not r.get("monthly_cost_usd") and old.get("monthly_cost_usd"):
                        r["monthly_cost_usd"] = old["monthly_cost_usd"]
                        r["cost_source"] = old.get("cost_source", "cost_explorer")
                discovered[key] = result
                console.print(f"  [green]✓[/green] {key}: {len(result)} resources (enriched)")

    # ── Step 4: Post-enrichment (SSM/Graviton for EC2) ────────────────────
    for entry in SERVICE_REGISTRY:
        key = entry["key"]
        if not discovered.get(key) or "post_enrich" not in entry:
            continue
        if key == "EC2":
            console.print("  Checking AMI arch + SSM inventory for Graviton compatibility...")
            discovered[key] = _safe(entry["post_enrich"], session, discovered[key])
            ssm = sum(1 for r in discovered[key] if r.get("ssm_managed"))
            arm = sum(1 for r in discovered[key] if r.get("ami_architecture") == "arm64")
            console.print(f"  [green]✓[/green] SSM managed: {ssm}/{len(discovered[key])} | ARM64: {arm}")
        else:
            discovered[key] = _safe(entry["post_enrich"], session, discovered[key])

    # ── Step 5: Apply per-resource CE cost ────────────────────────────────
    # Runs last so it survives the collector replacement above. Fill-only, so a
    # collector-supplied cost (e.g. the EKS control-plane flat rate) is kept.
    if cost_map:
        discovered = enrich_resources_with_cost(discovered, cost_map)
        attributed = sum(1 for items in discovered.values() for r in items
                         if r.get("cost_source") == "cost_explorer")
        console.print(f"  [green]✓[/green] Actual cost attached to {attributed} resources")

    # ── Step 6: Collapse alias type keys ──────────────────────────────────
    discovered = _canonicalise_types(discovered, set(registry_tasks))

    total = sum(len(v) for v in discovered.values())
    console.print(f"  Total: {total} resources across {len(discovered)} service types")
    return discovered


# RGTA derives its type key from the ARN, while the service collectors use
# their registry key. The same load balancer therefore arrived as both "ALB"
# (from discovery) and "ELB" (from the collector), and the report grew a
# duplicate tab per service: ALB/ELB, ElasticIP/ElasticIPs, CWLogGroup/
# CWLogGroups — including an empty "ElasticIP" tab with no rows at all.
CANONICAL_TYPES = {
    "ALB":          "ELB",
    "NLB":          "ELB",
    "GWLB":         "ELB",
    "ELBClassic":   "ELB",
    "ElasticIP":    "ElasticIPs",
    "CWLogGroup":   "CWLogGroups",
    "ECSCluster":   "ECS",
}


def _canonicalise_types(discovered, collector_keys):
    """
    Merge alias groups into their canonical key.

    Where a service-specific collector produced the canonical group, its data is
    richer than anything RGTA can give us, so alias entries are only kept when
    they describe a resource the collector did not return.
    """
    for alias, canonical in CANONICAL_TYPES.items():
        if alias not in discovered:
            continue
        alias_items = discovered.pop(alias)
        if not alias_items:
            continue

        target = discovered.setdefault(canonical, [])
        if canonical in collector_keys and target:
            # Collector output is authoritative — keep only genuinely new ids.
            # IDs are normalised before comparison: ARN parsing drops the
            # leading slash from log-group names, so RGTA reports
            # "aws/lambda/foo" where the collector reports "/aws/lambda/foo".
            def key(value):
                return str(value or "").lstrip("/").lower()

            known = {key(r.get("id")) for r in target}
            known |= {key(r.get("arn")) for r in target if r.get("arn")}
            extra = [r for r in alias_items
                     if key(r.get("id")) not in known
                     and key(r.get("arn")) not in known]
            if extra:
                target.extend(extra)
                console.print(f"  merged {len(extra)} extra {alias} into {canonical}")
        else:
            target.extend(alias_items)

    # Drop any group that ended up empty — an empty group still created a tab.
    return {k: v for k, v in discovered.items() if v}
