import boto3
from datetime import datetime, timedelta, date

from utils import utcnow
from collectors import api_errors


def get_waf_webacls(session):
    resources = []
    for scope in ["REGIONAL", "CLOUDFRONT"]:
        region = "us-east-1"
        client = session.client("wafv2", region_name=region)
        try:
            resp = client.list_web_acls(Scope=scope)
            for acl in resp.get("WebACLs", []):
                detail = {}
                rules = []
                try:
                    detail = client.get_web_acl(Name=acl["Name"], Scope=scope, Id=acl["Id"])["WebACL"]
                    rules = detail.get("Rules", [])
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

                rule_count = len(rules)
                managed_rules = sum(
                    1 for r in rules
                    if "ManagedRuleGroupStatement" in str(r.get("Statement", {}))
                )
                custom_rules = rule_count - managed_rules

                # Associations
                associations = []
                try:
                    assoc_resp = client.list_resources_for_web_acl(WebACLArn=acl["ARN"])
                    associations = assoc_resp.get("ResourceArns", [])
                except Exception as _e:
                    api_errors.note(_e, region=locals().get('region', ''))
                    pass

                resources.append({
                    "type": "WAF",
                    "id": acl["Id"],
                    "name": acl["Name"],
                    "region": "global" if scope == "CLOUDFRONT" else region,
                    "scope": scope,
                    "rule_count": rule_count,
                    "managed_rules": managed_rules,
                    "custom_rules": custom_rules,
                    "capacity_wcu": detail.get("Capacity", ""),
                    "associations": len(associations),
                    "associated_resources": ", ".join(a.split("/")[-1] for a in associations[:3]),
                    "tags": {},
                })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


def get_kms_keys(session, regions):
    resources = []
    for region in regions:
        client = session.client("kms", region_name=region)
        try:
            paginator = client.get_paginator("list_keys")
            for page in paginator.paginate():
                for key in page["Keys"]:
                    try:
                        meta = client.describe_key(KeyId=key["KeyId"])["KeyMetadata"]
                        if meta.get("KeyManager") == "AWS":  # skip AWS-managed keys (free)
                            continue
                        tags = {}
                        try:
                            tags = {t["TagKey"]: t["TagValue"] for t in
                                    client.list_resource_tags(KeyId=key["KeyId"]).get("Tags", [])}
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass
                        creation = meta.get("CreationDate")

                        rotation_enabled = False
                        try:
                            rotation_enabled = client.get_key_rotation_status(KeyId=key["KeyId"]).get("KeyRotationEnabled", False)
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass

                        aliases = []
                        try:
                            alias_resp = client.list_aliases(KeyId=key["KeyId"])
                            aliases = [a["AliasName"] for a in alias_resp.get("Aliases", []) if not a["AliasName"].startswith("alias/aws/")]
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass

                        resources.append({
                            "type": "KMS",
                            "id": key["KeyId"],
                            "name": meta.get("Description") or tags.get("Name", key["KeyId"]),
                            "aliases": ", ".join(aliases),
                            "region": region,
                            "key_state": meta.get("KeyState", ""),
                            "key_spec": meta.get("KeySpec", ""),
                            "key_usage": meta.get("KeyUsage", ""),
                            "key_manager": meta.get("KeyManager", ""),
                            "rotation_enabled": rotation_enabled,
                            "created": creation.strftime("%Y-%m-%d") if creation else "",
                            "deletion_date": meta.get("DeletionDate", ""),
                            "tags": tags,
                        })
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


def get_secrets(session, regions):
    resources = []
    for region in regions:
        client = session.client("secretsmanager", region_name=region)
        try:
            paginator = client.get_paginator("list_secrets")
            for page in paginator.paginate():
                for secret in page["SecretList"]:
                    last_accessed = secret.get("LastAccessedDate")
                    last_changed  = secret.get("LastChangedDate")
                    days_since_access = None
                    if last_accessed:
                        days_since_access = (utcnow() - last_accessed.replace(tzinfo=None)).days
                    tags = {t["Key"]: t["Value"] for t in secret.get("Tags", [])}
                    last_rotated = secret.get("LastRotatedDate")
                    next_rotation = secret.get("NextRotationDate")
                    resources.append({
                        "type": "SecretsManager",
                        "id": secret["ARN"].split(":")[-1],
                        "name": secret["Name"],
                        "description": secret.get("Description", ""),
                        "region": region,
                        "rotation_enabled": secret.get("RotationEnabled", False),
                        "rotation_lambda": secret.get("RotationLambdaARN", "").split(":")[-1] if secret.get("RotationLambdaARN") else "",
                        "last_rotated": last_rotated.strftime("%Y-%m-%d") if last_rotated else "",
                        "next_rotation": next_rotation.strftime("%Y-%m-%d") if next_rotation else "",
                        "last_accessed": last_accessed.strftime("%Y-%m-%d") if last_accessed else "Never",
                        "last_changed": last_changed.strftime("%Y-%m-%d") if last_changed else "",
                        "days_since_access": days_since_access,
                        "kms_key_id": (secret.get("KmsKeyId") or "aws/secretsmanager").split("/")[-1],
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


def get_ecr_repositories(session, regions):
    resources = []
    for region in regions:
        client = session.client("ecr", region_name=region)
        try:
            paginator = client.get_paginator("describe_repositories")
            for page in paginator.paginate():
                for repo in page["repositories"]:
                    # Count images
                    image_count = 0
                    untagged_count = 0
                    total_size_bytes = 0
                    latest_push = None
                    try:
                        img_paginator = client.get_paginator("describe_images")
                        for img_page in img_paginator.paginate(repositoryName=repo["repositoryName"]):
                            for img in img_page["imageDetails"]:
                                image_count += 1
                                total_size_bytes += img.get("imageSizeInBytes", 0)
                                if not img.get("imageTags"):
                                    untagged_count += 1
                                pushed_at = img.get("imagePushedAt")
                                if pushed_at and (latest_push is None or pushed_at > latest_push):
                                    latest_push = pushed_at
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    size_gb = round(total_size_bytes / (1024 ** 3), 4)

                    # Lifecycle policy
                    lifecycle_policy = False
                    try:
                        client.get_lifecycle_policy(repositoryName=repo["repositoryName"])
                        lifecycle_policy = True
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    tags = {}
                    try:
                        tags = {t["Key"]: t["Value"] for t in
                                client.list_tags_for_resource(resourceArn=repo["repositoryArn"]).get("tags", {})}
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    resources.append({
                        "type": "ECR",
                        "id": repo["repositoryName"],
                        "name": repo["repositoryName"],
                        "region": region,
                        "uri": repo.get("repositoryUri", ""),
                        "image_count": image_count,
                        "untagged_images": untagged_count,
                        "size_gb": size_gb,
                        "last_push_date": latest_push.strftime("%Y-%m-%d") if latest_push else "Never",
                        "lifecycle_policy": lifecycle_policy,
                        "tag_mutability": repo.get("imageTagMutability", ""),
                        "scan_on_push": repo.get("imageScanningConfiguration", {}).get("scanOnPush", False),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


def get_route53_zones(session):
    resources = []
    client = session.client("route53")
    try:
        paginator = client.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for zone in page["HostedZones"]:
                record_count = zone.get("ResourceRecordSetCount", 0)
                # Subtract NS and SOA which are always present
                data_records = max(0, record_count - 2)
                zone_type = "Private" if zone["Config"].get("PrivateZone") else "Public"
                resources.append({
                    "type": "Route53",
                    "id": zone["Id"].split("/")[-1],
                    "name": zone["Name"].rstrip("."),
                    "region": "global",
                    "zone_type": zone_type,
                    "record_count": record_count,
                    "data_records": data_records,
                    "comment": zone["Config"].get("Comment", ""),
                    "tags": {},
                })
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass
    return resources


def get_codebuild_projects(session, regions):
    resources = []
    for region in regions:
        client = session.client("codebuild", region_name=region)
        try:
            names = []
            paginator = client.get_paginator("list_projects")
            for page in paginator.paginate():
                names.extend(page.get("projects", []))

            if not names:
                continue

            # Batch describe
            for i in range(0, len(names), 100):
                batch = client.batch_get_projects(names=names[i:i+100])
                for proj in batch["projects"]:
                    tags = {t["key"]: t["value"] for t in proj.get("tags", [])}
                    env = proj.get("environment", {})
                    compute_type = env.get("computeType", "")
                    # Check last build
                    last_build_status = ""
                    last_build_date = ""
                    try:
                        builds = client.list_builds_for_project(projectName=proj["name"], sortOrder="DESCENDING")
                        if builds["ids"]:
                            b = client.batch_get_builds(ids=[builds["ids"][0]])["builds"][0]
                            last_build_status = b.get("buildStatus", "")
                            end_time = b.get("endTime")
                            last_build_date = end_time.strftime("%Y-%m-%d") if end_time else ""
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    resources.append({
                        "type": "CodeBuild",
                        "id": proj["name"],
                        "name": proj["name"],
                        "region": region,
                        "compute_type": compute_type,
                        "environment_type": env.get("type", ""),
                        "image": env.get("image", ""),
                        "service_role": proj.get("serviceRole", ""),
                        "last_build_status": last_build_status,
                        "last_build_date": last_build_date,
                        "source_type": proj.get("source", {}).get("type", ""),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources
