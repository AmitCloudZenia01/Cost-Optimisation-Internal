"""
Generic resource discovery using two APIs:

1. Resource Groups Tagging API (RGTA)
   - Discovers all TAGGED resources across every service in the account
   - Single paginated call per region — no service-specific code needed
   - Returns ARN + tags for every resource

2. ARN Parser
   - Parses ARN → service, resource_type, region, account_id, resource_id
   - Maps to a normalized internal type name used across the rest of the pipeline

Adding a new AWS service: add one line to RESOURCE_TYPE_MAP. Nothing else.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from collectors import api_errors

# ARNs the parser could not read; surfaced as a gap after discovery.
_UNPARSEABLE = []


# Maps (ce_service_name, arn_resource_prefix) → internal type key
# Longest prefix match wins
RESOURCE_TYPE_MAP = [
    # (arn_service, arn_resource_prefix, internal_type)
    ("ec2",                     "instance",             "EC2"),
    ("ec2",                     "volume",               "EBS"),
    ("ec2",                     "natgateway",           "NATGateway"),
    ("ec2",                     "eip-allocation",       "ElasticIP"),
    ("ec2",                     "elastic-ip",           "ElasticIP"),
    ("ec2",                     "subnet",               "Subnet"),
    ("ec2",                     "vpc",                  "VPC"),
    ("rds",                     "db",                   "RDS"),
    ("rds",                     "cluster",              "RDSCluster"),
    ("lambda",                  "function",             "Lambda"),
    ("eks",                     "cluster",              "EKS"),
    ("elasticache",             "cluster",              "ElastiCache"),
    ("elasticache",             "replicationgroup",     "ElastiCacheReplicationGroup"),
    ("s3",                      "",                     "S3"),
    ("cloudfront",              "distribution",         "CloudFront"),
    ("elasticloadbalancing",    "loadbalancer/app",     "ALB"),
    ("elasticloadbalancing",    "loadbalancer/net",     "NLB"),
    ("elasticloadbalancing",    "loadbalancer",         "ELB"),
    ("dynamodb",                "table",                "DynamoDB"),
    ("es",                      "domain",               "OpenSearch"),
    ("opensearch",              "domain",               "OpenSearch"),
    ("transfer",                "server",               "TransferFamily"),
    ("wafv2",                   "webacl",               "WAF"),
    ("kms",                     "key",                  "KMS"),
    ("secretsmanager",          "secret",               "SecretsManager"),
    ("ecr",                     "repository",           "ECR"),
    ("codebuild",               "project",              "CodeBuild"),
    ("codepipeline",            "",                     "CodePipeline"),
    ("logs",                    "log-group",            "CWLogGroup"),
    ("route53",                 "hostedzone",           "Route53"),
    ("sqs",                     "",                     "SQS"),
    ("sns",                     "",                     "SNS"),
    ("apigateway",              "",                     "APIGateway"),
    ("execute-api",             "",                     "APIGateway"),
    ("ecs",                     "cluster",              "ECSCluster"),
    ("ecs",                     "service",              "ECSService"),
    ("redshift",                "cluster",              "Redshift"),
    ("elasticmapreduce",        "cluster",              "EMR"),
    ("kinesis",                 "stream",               "Kinesis"),
    ("firehose",                "deliverystream",       "KinesisFirehose"),
    ("kafka",                   "cluster",              "MSK"),
    ("states",                  "stateMachine",         "StepFunctions"),
    ("glue",                    "",                     "Glue"),
    ("athena",                  "workgroup",            "Athena"),
    ("sagemaker",               "endpoint",             "SageMakerEndpoint"),
    ("sagemaker",               "notebook-instance",    "SageMakerNotebook"),
    ("sagemaker",               "",                     "SageMaker"),
    ("guardduty",               "detector",             "GuardDuty"),
    ("inspector2",              "",                     "Inspector"),
    ("macie2",                  "",                     "Macie"),
    ("waf",                     "",                     "WAFClassic"),
    ("backup",                  "vault",                "BackupVault"),
    ("efs",                     "file-system",          "EFS"),
    ("fsx",                     "file-system",          "FSx"),
    ("mq",                      "broker",               "MQ"),
    ("apprunner",               "service",              "AppRunner"),
    ("lightsail",               "",                     "Lightsail"),
    ("batch",                   "job-queue",            "Batch"),
    ("cognito-idp",             "userpool",             "Cognito"),
    ("codecommit",              "",                     "CodeCommit"),
    ("codeartifact",            "",                     "CodeArtifact"),
    ("events",                  "rule",                 "EventBridge"),
    ("scheduler",               "",                     "EventBridgeScheduler"),
    ("xray",                    "group",                "XRay"),
    ("grafana",                 "workspace",            "Grafana"),
    ("aps",                     "workspace",            "Prometheus"),
]


def _parse_arn(arn):
    """
    Parse ARN into components.
    arn:partition:service:region:account:resource_path
    """
    try:
        parts = arn.split(":", 5)
        if len(parts) < 6:
            return None
        _, partition, service, region, account, resource_path = parts

        # Normalize resource path — can be:
        # "instance/i-1234"           → type=instance, id=i-1234
        # "db:mydb"                   → type=db, id=mydb
        # "my-bucket"                 → type="", id=my-bucket
        # "loadbalancer/app/name/id"  → type=loadbalancer/app, id=name/id
        if "/" in resource_path:
            parts2 = resource_path.split("/")
            resource_type = parts2[0]
            resource_id = "/".join(parts2[1:])
        elif ":" in resource_path:
            parts2 = resource_path.split(":", 1)
            resource_type = parts2[0]
            resource_id = parts2[1]
        else:
            resource_type = ""
            resource_id = resource_path

        return {
            "arn":           arn,
            "service":       service,
            "resource_type": resource_type,
            "resource_id":   resource_id,
            "region":        region or "global",
            "account_id":    account,
        }
    except Exception:
        # A resource whose ARN we cannot parse vanishes from the report
        # entirely, so it is counted rather than dropped in silence.
        _UNPARSEABLE.append(arn)
        return None


def _map_type(service, resource_type, resource_path):
    """Map ARN components to our internal type key. Longest prefix match wins."""
    best_match = None
    best_len = -1

    full_resource = resource_path.lower()

    for svc, prefix, internal_type in RESOURCE_TYPE_MAP:
        if service != svc:
            continue
        rtype_lower = resource_type.lower()
        # Match on resource type prefix
        if prefix == "" or rtype_lower.startswith(prefix) or full_resource.startswith(prefix):
            match_len = len(prefix)
            if match_len > best_len:
                best_len = match_len
                best_match = internal_type

    return best_match or f"{service}/{resource_type}" or service


def discover_tagged_resources(session, region):
    """
    Discover all tagged resources in a single region via RGTA.
    Returns list of normalized resource dicts.
    """
    client = session.client("resourcegroupstaggingapi", region_name=region)
    resources = []
    try:
        paginator = client.get_paginator("get_resources")
        for page in paginator.paginate(ResourcesPerPage=100):
            for item in page.get("ResourceTagMappingList", []):
                arn = item["ResourceARN"]
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                parsed = _parse_arn(arn)
                if not parsed:
                    continue
                internal_type = _map_type(
                    parsed["service"],
                    parsed["resource_type"],
                    f"{parsed['resource_type']}/{parsed['resource_id']}",
                )
                resources.append({
                    "id":            parsed["resource_id"],
                    "name":          tags.get("Name", parsed["resource_id"]),
                    "arn":           arn,
                    "type":          internal_type,
                    "service":       parsed["service"],
                    "resource_type": parsed["resource_type"],
                    "region":        region,
                    "account_id":    parsed["account_id"],
                    "tags":          tags,
                })
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass
    return resources


def discover_all(session, regions):
    """
    Discover all tagged resources across all regions in parallel.
    Returns {type: [resource, ...]} grouped by internal type.
    """
    all_resources = []

    if regions:
        # max_workers must be >= 1; an empty region list used to raise ValueError.
        with ThreadPoolExecutor(max_workers=max(1, len(regions))) as ex:
            futures = {ex.submit(discover_tagged_resources, session, r): r for r in regions}
            for future in as_completed(futures):
                try:
                    all_resources.extend(future.result())
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

    # Also discover global resources (S3 buckets, Route53, CloudFront, IAM)
    all_resources.extend(_discover_global(session))

    if _UNPARSEABLE:
        from analysis.provenance import gaps
        gaps.add(
            category="Collection",
            what=f"{len(_UNPARSEABLE)} resource ARN(s) could not be parsed",
            why="These resources were discovered but their ARN did not match "
                "the expected format, so they were excluded.",
            how_to_fix="Report the ARN format so the parser can be extended.",
            impact="These resources are missing from the report entirely.")

    # Group by type, deduplicating by ARN via a set rather than a linear scan
    # of the growing list (which made this O(n^2) on large accounts).
    grouped = {}
    seen_arns = set()
    for r in all_resources:
        arn = r.get("arn", "")
        if arn and arn in seen_arns:
            continue
        if arn:
            seen_arns.add(arn)
        grouped.setdefault(r["type"], []).append(r)

    return grouped


def _discover_global(session):
    """Discover global/untagged resources that RGTA misses."""
    resources = []

    # S3 buckets (always us-east-1 endpoint, global)
    try:
        s3 = session.client("s3", region_name="us-east-1")
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            tags = {}
            try:
                tags = {t["Key"]: t["Value"] for t in
                        s3.get_bucket_tagging(Bucket=name).get("TagSet", [])}
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                pass
            region = "us-east-1"
            try:
                loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
                region = loc or "us-east-1"
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                pass
            resources.append({
                "id": name, "name": tags.get("Name", name),
                "arn": f"arn:aws:s3:::{name}",
                "type": "S3", "service": "s3",
                "resource_type": "", "region": region,
                "account_id": "", "tags": tags,
            })
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass

    # Route53 hosted zones
    try:
        r53 = session.client("route53")
        paginator = r53.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for zone in page["HostedZones"]:
                zone_id = zone["Id"].split("/")[-1]
                resources.append({
                    "id": zone_id, "name": zone["Name"].rstrip("."),
                    "arn": f"arn:aws:route53:::hostedzone/{zone_id}",
                    "type": "Route53", "service": "route53",
                    "resource_type": "hostedzone", "region": "global",
                    "account_id": "", "tags": {},
                    "private_zone": zone["Config"].get("PrivateZone", False),
                    "record_count": zone.get("ResourceRecordSetCount", 0),
                })
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass

    return resources
