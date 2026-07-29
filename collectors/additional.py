"""
Additional service collectors: ECS, API Gateway, SQS, SNS, EFS, Redshift, Kinesis, MSK
"""
from datetime import datetime, timezone
from collectors import api_errors


# ── ECS / Fargate ─────────────────────────────────────────────────────────────
def get_ecs_services(session, regions):
    resources = []
    for region in regions:
        ecs = session.client("ecs", region_name=region)
        try:
            cluster_arns = []
            pager = ecs.get_paginator("list_clusters")
            for page in pager.paginate():
                cluster_arns.extend(page["clusterArns"])
            if not cluster_arns:
                continue

            clusters = ecs.describe_clusters(
                clusters=cluster_arns,
                include=["TAGS"],
            )["clusters"]

            for cl in clusters:
                tags = {t["key"]: t["value"] for t in cl.get("tags", [])}
                resources.append({
                    "type": "ECS",
                    "id": cl["clusterName"],
                    "name": cl["clusterName"],
                    "region": region,
                    "status": cl.get("status", ""),
                    "running_tasks": cl.get("runningTasksCount", 0),
                    "pending_tasks": cl.get("pendingTasksCount", 0),
                    "active_services": cl.get("activeServicesCount", 0),
                    "registered_instances": cl.get("registeredContainerInstancesCount", 0),
                    "tags": tags,
                })

                # Services inside cluster
                svc_arns = []
                sp = ecs.get_paginator("list_services")
                for pg in sp.paginate(cluster=cl["clusterArn"]):
                    svc_arns.extend(pg["serviceArns"])

                for i in range(0, len(svc_arns), 10):
                    batch = ecs.describe_services(
                        cluster=cl["clusterArn"],
                        services=svc_arns[i:i+10],
                        include=["TAGS"],
                    )["services"]
                    for svc in batch:
                        cpu = memory = ""
                        try:
                            td = ecs.describe_task_definition(
                                taskDefinition=svc["taskDefinition"])["taskDefinition"]
                            cpu    = td.get("cpu", "")
                            memory = td.get("memory", "")
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass
                        stags = {t["key"]: t["value"] for t in svc.get("tags", [])}
                        resources.append({
                            "type": "ECSService",
                            "id": f"{cl['clusterName']}/{svc['serviceName']}",
                            "name": svc["serviceName"],
                            "cluster": cl["clusterName"],
                            "region": region,
                            "status": svc.get("status", ""),
                            "launch_type": svc.get("launchType", "FARGATE"),
                            "task_definition": svc.get("taskDefinition", "").split("/")[-1],
                            "desired_count": svc.get("desiredCount", 0),
                            "running_count": svc.get("runningCount", 0),
                            "pending_count": svc.get("pendingCount", 0),
                            "cpu_units": cpu,
                            "memory_mb": memory,
                            "tags": stags,
                        })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── API Gateway ───────────────────────────────────────────────────────────────
def get_api_gateways(session, regions):
    resources = []
    for region in regions:
        # REST APIs (v1)
        try:
            apigw = session.client("apigateway", region_name=region)
            pager = apigw.get_paginator("get_rest_apis")
            for page in pager.paginate():
                for api in page["items"]:
                    stages = []
                    try:
                        stages = apigw.get_stages(restApiId=api["id"]).get("item", [])
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
                    resources.append({
                        "type": "APIGateway",
                        "id": api["id"],
                        "name": api["name"],
                        "region": region,
                        "protocol": "REST",
                        "stage_count": len(stages),
                        "stages": ", ".join(s.get("stageName", "") for s in stages),
                        "description": api.get("description", ""),
                        "endpoint_type": api.get("endpointConfiguration", {}).get("types", [""])[0],
                        "created": api.get("createdDate", "").isoformat() if api.get("createdDate") else "",
                        "tags": api.get("tags", {}),
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # HTTP / WebSocket APIs (v2)
        try:
            apigw2 = session.client("apigatewayv2", region_name=region)
            pager2 = apigw2.get_paginator("get_apis")
            for page in pager2.paginate():
                for api in page["Items"]:
                    resources.append({
                        "type": "APIGateway",
                        "id": api["ApiId"],
                        "name": api["Name"],
                        "region": region,
                        "protocol": api.get("ProtocolType", "HTTP"),
                        "stage_count": 0,
                        "stages": "",
                        "description": api.get("Description", ""),
                        "endpoint_type": "REGIONAL",
                        "created": api.get("CreatedDate", "").isoformat() if api.get("CreatedDate") else "",
                        "tags": api.get("Tags", {}),
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── SQS ───────────────────────────────────────────────────────────────────────
def get_sqs_queues(session, regions):
    resources = []
    for region in regions:
        client = session.client("sqs", region_name=region)
        try:
            pager = client.get_paginator("list_queues")
            for page in pager.paginate():
                for url in page.get("QueueUrls", []):
                    try:
                        attrs = client.get_queue_attributes(
                            QueueUrl=url, AttributeNames=["All"]
                        ).get("Attributes", {})
                        name = url.split("/")[-1]
                        tags = {}
                        try:
                            tags = client.list_queue_tags(QueueUrl=url).get("Tags", {})
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass
                        resources.append({
                            "type": "SQS",
                            "id": name,
                            "name": name,
                            "region": region,
                            "queue_type": "FIFO" if name.endswith(".fifo") else "Standard",
                            "messages_available": int(attrs.get("ApproximateNumberOfMessages", 0)),
                            "messages_in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
                            "visibility_timeout_s": int(attrs.get("VisibilityTimeout", 30)),
                            "retention_days": int(attrs.get("MessageRetentionPeriod", 345600)) // 86400,
                            "max_message_size_kb": int(attrs.get("MaximumMessageSize", 262144)) // 1024,
                            "delay_seconds": int(attrs.get("DelaySeconds", 0)),
                            "dlq_configured": bool(attrs.get("RedrivePolicy")),
                            "kms_key": attrs.get("KmsMasterKeyId", ""),
                            "tags": tags,
                        })
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── SNS ───────────────────────────────────────────────────────────────────────
def get_sns_topics(session, regions):
    resources = []
    for region in regions:
        client = session.client("sns", region_name=region)
        try:
            pager = client.get_paginator("list_topics")
            for page in pager.paginate():
                for topic in page["Topics"]:
                    arn  = topic["TopicArn"]
                    name = arn.split(":")[-1]
                    attrs = tags = {}
                    try:
                        attrs = client.get_topic_attributes(TopicArn=arn).get("Attributes", {})
                        tags = {t["Key"]: t["Value"] for t in
                                client.list_tags_for_resource(ResourceArn=arn).get("Tags", [])}
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
                    resources.append({
                        "type": "SNS",
                        "id": name,
                        "name": name,
                        "region": region,
                        "topic_type": "FIFO" if name.endswith(".fifo") else "Standard",
                        "subscriptions_confirmed": int(attrs.get("SubscriptionsConfirmed", 0)),
                        "subscriptions_pending": int(attrs.get("SubscriptionsPending", 0)),
                        "kms_key": attrs.get("KmsMasterKeyId", ""),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── EFS ───────────────────────────────────────────────────────────────────────
def get_efs_filesystems(session, regions):
    resources = []
    for region in regions:
        client = session.client("efs", region_name=region)
        try:
            pager = client.get_paginator("describe_file_systems")
            for page in pager.paginate():
                for fs in page["FileSystems"]:
                    tags = {t["Key"]: t["Value"] for t in fs.get("Tags", [])}
                    size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                    size_gb    = round(size_bytes / (1024 ** 3), 2)
                    mount_targets = 0
                    try:
                        mt = client.describe_mount_targets(FileSystemId=fs["FileSystemId"])
                        mount_targets = len(mt.get("MountTargets", []))
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
                    created = fs.get("CreationTime")
                    age_days = (datetime.now(timezone.utc) - created).days if created else ""
                    resources.append({
                        "type": "EFS",
                        "id": fs["FileSystemId"],
                        "name": tags.get("Name", fs["FileSystemId"]),
                        "region": region,
                        "state": fs.get("LifeCycleState", ""),
                        "performance_mode": fs.get("PerformanceMode", ""),
                        "throughput_mode": fs.get("ThroughputMode", ""),
                        "encrypted": fs.get("Encrypted", False),
                        "kms_key": (fs.get("KmsKeyId") or "").split("/")[-1],
                        "size_gb": size_gb,
                        "mount_targets": mount_targets,
                        "ia_enabled": bool(fs.get("LifecyclePolicies")),
                        "created": created.strftime("%Y-%m-%d") if created else "",
                        "age_days": age_days,
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── Redshift ──────────────────────────────────────────────────────────────────
def get_redshift_clusters(session, regions):
    resources = []
    for region in regions:
        client = session.client("redshift", region_name=region)
        try:
            pager = client.get_paginator("describe_clusters")
            for page in pager.paginate():
                for cl in page["Clusters"]:
                    tags = {t["Key"]: t["Value"] for t in cl.get("Tags", [])}
                    resources.append({
                        "type": "Redshift",
                        "id": cl["ClusterIdentifier"],
                        "name": cl["ClusterIdentifier"],
                        "region": region,
                        "status": cl.get("ClusterStatus", ""),
                        "node_type": cl.get("NodeType", ""),
                        "node_count": cl.get("NumberOfNodes", 1),
                        "multi_az": cl.get("MultiAZ", "Disabled") != "Disabled",
                        "database": cl.get("DBName", ""),
                        "master_user": cl.get("MasterUsername", ""),
                        "vpc_id": cl.get("VpcId", ""),
                        "publicly_accessible": cl.get("PubliclyAccessible", False),
                        "encrypted": cl.get("Encrypted", False),
                        "snapshot_retention_days": cl.get("AutomatedSnapshotRetentionPeriod", 0),
                        "maintenance_window": cl.get("PreferredMaintenanceWindow", ""),
                        "enhanced_vpc_routing": cl.get("EnhancedVpcRouting", False),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── Kinesis Data Streams ──────────────────────────────────────────────────────
def get_kinesis_streams(session, regions):
    resources = []
    for region in regions:
        client = session.client("kinesis", region_name=region)
        try:
            pager = client.get_paginator("list_streams")
            for page in pager.paginate():
                for name in page.get("StreamNames", []):
                    try:
                        detail = client.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]
                        tags   = client.list_tags_for_stream(StreamName=name).get("Tags", {})
                        resources.append({
                            "type": "Kinesis",
                            "id": name,
                            "name": name,
                            "region": region,
                            "status": detail.get("StreamStatus", ""),
                            "shard_count": detail.get("OpenShardCount", 0),
                            "retention_hours": detail.get("RetentionPeriodHours", 24),
                            "consumer_count": detail.get("ConsumerCount", 0),
                            "stream_mode": detail.get("StreamModeDetails", {}).get("StreamMode", "PROVISIONED"),
                            "encrypted": detail.get("EncryptionType", "NONE") != "NONE",
                            "tags": tags,
                        })
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources


# ── MSK (Managed Kafka) ───────────────────────────────────────────────────────
def get_msk_clusters(session, regions):
    resources = []
    for region in regions:
        client = session.client("kafka", region_name=region)
        try:
            pager = client.get_paginator("list_clusters_v2")
            for page in pager.paginate():
                for cl in page.get("ClusterInfoList", []):
                    tags = cl.get("Tags", {})
                    broker_info = cl.get("Provisioned", cl.get("Serverless", {}))
                    broker_type = broker_info.get("BrokerNodeGroupInfo", {}).get("InstanceType", "")
                    broker_count = broker_info.get("NumberOfBrokerNodes", 0)
                    resources.append({
                        "type": "MSK",
                        "id": cl["ClusterName"],
                        "name": cl["ClusterName"],
                        "region": region,
                        "state": cl.get("State", ""),
                        "cluster_type": cl.get("ClusterType", "PROVISIONED"),
                        "kafka_version": broker_info.get("CurrentBrokerSoftwareInfo", {}).get("KafkaVersion", ""),
                        "broker_type": broker_type,
                        "broker_count": broker_count,
                        "storage_gb_per_broker": broker_info.get("BrokerNodeGroupInfo", {}).get(
                            "StorageInfo", {}).get("EbsStorageInfo", {}).get("VolumeSize", 0),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources
