"""
Generic CloudWatch metrics — BATCH mode.

Uses get_metric_data instead of get_metric_statistics.
  Old: 1 API call per metric  → 1200+ calls → 3-4 minutes
  New: 500 metrics per call   → 6-8 calls   → 10 seconds

Flow:
  1. Group all resources by region
  2. For each region + time window, build ALL metric queries in one list
  3. Send in batches of 500 to get_metric_data
  4. Parse results back into per-resource dicts

Per-resource config lookup: a group may hold mixed resource types (ALB, NLB and
Classic ELB all arrive under the "ELB" key but use different namespaces and
dimensions), so the config is resolved from each resource's own "type" first.
"""

import statistics
import threading
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import utcnow
from collectors import api_errors


# Known metrics per resource type.
#   metrics        : (metric_name, cloudwatch_stat, friendly_key)
#   dimension_key  : CloudWatch dimension name
#   dimension_field: which resource field supplies its value (default "id")
#   extra_dimension_fields: [(DimensionName, resource_field)] resolved per resource
#   extra_metrics  : metrics living in a *different* namespace (e.g. CWAgent),
#                    optionally with several dimension shapes to try in order
METRICS_CONFIG = {
    "EC2": {
        "namespace":     "AWS/EC2",
        "dimension_key": "InstanceId",
        "metrics": [
            ("CPUUtilization",           "Average", "cpu_avg_pct"),
            ("NetworkIn",                "Average", "network_in_bytes"),
            ("NetworkOut",               "Average", "network_out_bytes"),
            ("DiskReadOps",              "Average", "disk_read_ops"),
            ("DiskWriteOps",             "Average", "disk_write_ops"),
            ("CPUCreditBalance",         "Average", "cpu_credit_balance"),
            ("CPUSurplusCreditsCharged", "Sum",     "cpu_surplus_charged"),
        ],
        # Averages hide peaks. An instance averaging 8% CPU but touching 95%
        # cannot safely be downsized, yet an average-only check calls it safe.
        # Min/Max/p95 are collected so rightsizing is judged on the peak, with
        # p95 as the working figure (Maximum can be a single unrepresentative
        # spike, and is reported separately rather than driving the decision).
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        # RAM/disk/swap only exist if the CloudWatch Agent is installed. These
        # were lost when this module replaced the per-metric collector, which
        # made every EC2 rightsize report "RAM unavailable" and risk Medium.
        "extra_metrics": [
            {"namespace": "CWAgent", "name": "mem_used_percent",
             "stat": "Maximum", "key": "mem_max_pct"},
            {"namespace": "CWAgent", "name": "mem_used_percent",
             "stat": "p95", "key": "mem_p95_pct"},
            {"namespace": "CWAgent", "name": "mem_used_percent",
             "stat": "Average", "key": "mem_used_pct"},
            {"namespace": "CWAgent", "name": "swap_used_percent",
             "stat": "Average", "key": "swap_used_pct"},
            {"namespace": "CWAgent", "name": "disk_used_percent",
             "stat": "Average", "key": "disk_used_pct",
             "variants": [
                 [{"Name": "path", "Value": "/"}],
                 [{"Name": "path", "Value": "/"}, {"Name": "fstype", "Value": "ext4"}],
                 [{"Name": "path", "Value": "/"}, {"Name": "fstype", "Value": "xfs"}],
                 [{"Name": "path", "Value": "C:"}],
                 [],
             ]},
        ],
        "spike_metric": "CPUUtilization",
    },
    "RDS": {
        "namespace":     "AWS/RDS",
        "dimension_key": "DBInstanceIdentifier",
        "metrics": [
            ("CPUUtilization",      "Average", "cpu_avg_pct"),
            ("FreeableMemory",      "Average", "freeable_memory_bytes"),
            ("DatabaseConnections", "Average", "db_connections_avg"),
            ("ReadIOPS",            "Average", "read_iops"),
            ("WriteIOPS",           "Average", "write_iops"),
            ("FreeStorageSpace",    "Average", "free_storage_bytes"),
            ("ReadLatency",         "Average", "read_latency_s"),
            ("WriteLatency",        "Average", "write_latency_s"),
            ("SwapUsage",           "Average", "swap_usage_bytes"),
            ("ReplicaLag",          "Average", "replica_lag_s"),
            ("BurstBalance",        "Average", "burst_balance_pct"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "Lambda": {
        "namespace":     "AWS/Lambda",
        "dimension_key": "FunctionName",
        "metrics": [
            ("Invocations",          "Sum",     "invocations_total"),
            ("Duration",             "Average", "duration_avg_ms"),
            ("Errors",               "Sum",     "errors_total"),
            ("Throttles",            "Sum",     "throttles_total"),
            ("ConcurrentExecutions", "Average", "concurrent_avg"),
            ("InitDuration",         "Average", "init_duration_ms"),
        ],
        "spike_metric": "Duration",
    },
    "ElastiCache": {
        "namespace":     "AWS/ElastiCache",
        "dimension_key": "CacheClusterId",
        "metrics": [
            ("CPUUtilization",       "Average", "cpu_avg_pct"),
            ("EngineCPUUtilization", "Average", "engine_cpu_pct"),
            ("FreeableMemory",       "Average", "freeable_memory_bytes"),
            ("BytesUsedForCache",    "Average", "bytes_used_for_cache"),
            ("CacheHits",            "Sum",     "cache_hits"),
            ("CacheMisses",          "Sum",     "cache_misses"),
            ("Evictions",            "Sum",     "evictions"),
            ("CurrConnections",      "Average", "connections_avg"),
            ("ReplicationLag",       "Average", "replication_lag_s"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "EBS": {
        "namespace":     "AWS/EBS",
        "dimension_key": "VolumeId",
        "metrics": [
            ("VolumeReadOps",    "Average", "read_ops_avg"),
            ("VolumeWriteOps",   "Average", "write_ops_avg"),
            ("VolumeReadBytes",  "Average", "read_bytes_avg"),
            ("VolumeWriteBytes", "Average", "write_bytes_avg"),
            ("BurstBalance",     "Average", "burst_balance_avg"),
            ("VolumeQueueLength","Average", "queue_depth_avg"),
        ],
    },
    # Application Load Balancers. The dimension value must be the full
    # "app/<name>/<id>" ARN suffix — see collectors/elb.py.
    "ELB": {
        "namespace":     "AWS/ApplicationELB",
        "dimension_key": "LoadBalancer",
        "metrics": [
            ("RequestCount",              "Sum",     "request_count_30d"),
            ("TargetResponseTime",        "Average", "latency_avg_s"),
            ("HTTPCode_ELB_5XX_Count",    "Sum",     "http_5xx_30d"),
            ("HTTPCode_Target_4XX_Count", "Sum",     "http_4xx_30d"),
            ("ActiveConnectionCount",     "Average", "active_connections"),
            ("NewConnectionCount",        "Sum",     "new_connections"),
        ],
    },
    "ALB": {
        "namespace":     "AWS/ApplicationELB",
        "dimension_key": "LoadBalancer",
        "metrics": [
            ("RequestCount",              "Sum",     "request_count_30d"),
            ("TargetResponseTime",        "Average", "latency_avg_s"),
            ("HTTPCode_ELB_5XX_Count",    "Sum",     "http_5xx_30d"),
            ("HTTPCode_Target_4XX_Count", "Sum",     "http_4xx_30d"),
            ("ActiveConnectionCount",     "Average", "active_connections"),
            ("NewConnectionCount",        "Sum",     "new_connections"),
        ],
    },
    "NLB": {
        "namespace":     "AWS/NetworkELB",
        "dimension_key": "LoadBalancer",
        "metrics": [
            ("ActiveFlowCount",       "Average", "active_connections"),
            ("NewFlowCount",          "Sum",     "new_connections"),
            ("ProcessedBytes",        "Sum",     "processed_bytes"),
            ("TCP_Target_Reset_Count","Sum",     "tcp_resets"),
        ],
    },
    "ELBClassic": {
        "namespace":     "AWS/ELB",
        "dimension_key": "LoadBalancerName",
        "metrics": [
            ("RequestCount",           "Sum",     "request_count_30d"),
            ("Latency",                "Average", "latency_avg_s"),
            ("HTTPCode_Backend_5XX",   "Sum",     "http_5xx_30d"),
            ("HTTPCode_Backend_4XX",   "Sum",     "http_4xx_30d"),
        ],
    },
    "NATGateway": {
        "namespace":     "AWS/NATGateway",
        "dimension_key": "NatGatewayId",
        "metrics": [
            ("BytesInFromSource",      "Sum",     "bytes_in"),
            ("BytesInFromDestination", "Sum",     "bytes_out"),
            ("PacketsDropCount",       "Sum",     "packets_dropped"),
            ("ActiveConnectionCount",  "Average", "active_connections"),
        ],
    },
    "DynamoDB": {
        "namespace":     "AWS/DynamoDB",
        "dimension_key": "TableName",
        "metrics": [
            ("ConsumedReadCapacityUnits",   "Sum",     "consumed_rcu"),
            ("ConsumedWriteCapacityUnits",  "Sum",     "consumed_wcu"),
            ("ThrottledRequests",           "Sum",     "throttled_requests"),
            ("SuccessfulRequestLatency",    "Average", "latency_avg_ms"),
            ("SystemErrors",                "Sum",     "system_errors"),
        ],
    },
    "OpenSearch": {
        "namespace":     "AWS/ES",
        "dimension_key": "DomainName",
        "metrics": [
            ("CPUUtilization",         "Average", "cpu_avg_pct"),
            ("FreeStorageSpace",       "Average", "free_storage_mb"),
            ("JVMMemoryPressure",      "Average", "jvm_memory_pct"),
            ("SearchLatency",          "Average", "search_latency_ms"),
            ("IndexingLatency",        "Average", "index_latency_ms"),
            ("ClusterStatus.red",      "Maximum", "status_red"),
            ("ClusterStatus.yellow",   "Maximum", "status_yellow"),
            ("SearchRate",             "Average", "search_rate"),
            ("IndexingRate",           "Average", "index_rate"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "TransferFamily": {
        "namespace":     "AWS/Transfer",
        "dimension_key": "ServerId",
        "metrics": [
            ("BytesIn",      "Sum", "bytes_in_30d"),
            ("BytesOut",     "Sum", "bytes_out_30d"),
            ("FilesIn",      "Sum", "files_in_30d"),
            ("FilesOut",     "Sum", "files_out_30d"),
            ("SessionsCount","Sum", "sessions_30d"),
        ],
    },
    "SQS": {
        "namespace":     "AWS/SQS",
        "dimension_key": "QueueName",
        "metrics": [
            ("NumberOfMessagesSent",               "Sum",     "messages_sent"),
            ("NumberOfMessagesReceived",           "Sum",     "messages_received"),
            ("NumberOfMessagesDeleted",            "Sum",     "messages_deleted"),
            ("ApproximateNumberOfMessagesVisible", "Average", "messages_visible"),
            ("ApproximateAgeOfOldestMessage",      "Maximum", "oldest_msg_age_s"),
            ("NumberOfEmptyReceives",              "Sum",     "empty_receives"),
        ],
    },
    "SNS": {
        "namespace":     "AWS/SNS",
        "dimension_key": "TopicName",
        "metrics": [
            ("NumberOfMessagesPublished",      "Sum",     "messages_published"),
            ("NumberOfNotificationsDelivered", "Sum",     "notifications_delivered"),
            ("NumberOfNotificationsFailed",    "Sum",     "notifications_failed"),
            ("PublishSize",                    "Average", "publish_size_bytes"),
        ],
    },
    "ECS": {
        "namespace":     "AWS/ECS",
        "dimension_key": "ClusterName",
        "metrics": [
            ("CPUUtilization",    "Average", "cpu_avg_pct"),
            ("MemoryUtilization", "Average", "memory_avg_pct"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "ECSService": {
        "namespace":     "AWS/ECS",
        "dimension_key": "ServiceName",
        "dimension_field": "name",
        "extra_dimension_fields": [("ClusterName", "cluster")],
        "metrics": [
            ("CPUUtilization",    "Average", "cpu_avg_pct"),
            ("MemoryUtilization", "Average", "memory_avg_pct"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "Redshift": {
        "namespace":     "AWS/Redshift",
        "dimension_key": "ClusterIdentifier",
        "metrics": [
            ("CPUUtilization",           "Average", "cpu_avg_pct"),
            ("PercentageDiskSpaceUsed",  "Average", "disk_used_pct"),
            ("DatabaseConnections",      "Average", "db_connections_avg"),
            ("ReadIOPS",                 "Average", "read_iops"),
            ("WriteIOPS",                "Average", "write_iops"),
            ("ReadLatency",              "Average", "read_latency_ms"),
            ("WriteLatency",             "Average", "write_latency_ms"),
            ("NetworkReceiveThroughput", "Average", "network_in_bytes"),
            ("NetworkTransmitThroughput","Average", "network_out_bytes"),
        ],
        "peak_metrics": [
            {"name": "CPUUtilization",
             "stats": {"Minimum": "cpu_min_pct",
                       "Maximum": "cpu_max_pct",
                       "p95": "cpu_p95_pct"}},
        ],
        "spike_metric": "CPUUtilization",
    },
    "Kinesis": {
        "namespace":     "AWS/Kinesis",
        "dimension_key": "StreamName",
        "metrics": [
            ("GetRecords.IteratorAgeMilliseconds",  "Average", "iterator_age_ms"),
            ("IncomingRecords",                     "Sum",     "incoming_records"),
            ("ReadProvisionedThroughputExceeded",   "Average", "read_throttle_pct"),
            ("WriteProvisionedThroughputExceeded",  "Average", "write_throttle_pct"),
        ],
    },
    "MSK": {
        "namespace":     "AWS/Kafka",
        "dimension_key": "Cluster Name",
        "metrics": [
            ("CpuUser",                  "Average", "cpu_user_pct"),
            ("KafkaDataLogsDiskUsed",    "Average", "disk_used_pct"),
            ("UnderReplicatedPartitions","Average", "under_replicated"),
        ],
        "spike_metric": "CpuUser",
    },
    "EFS": {
        "namespace":     "AWS/EFS",
        "dimension_key": "FileSystemId",
        "metrics": [
            ("BurstCreditBalance",  "Average", "burst_credit_balance"),
            ("PermittedThroughput", "Average", "permitted_throughput"),
            ("MeteredIOBytes",      "Sum",     "io_bytes_total"),
            ("ClientConnections",   "Average", "client_connections"),
            ("StorageBytes",        "Average", "storage_bytes"),
        ],
    },
    "APIGateway": {
        "namespace":     "AWS/ApiGateway",
        "dimension_key": "ApiName",
        "dimension_field": "name",
        "metrics": [
            ("Count",              "Sum",     "requests_total"),
            ("Latency",            "Average", "latency_avg_ms"),
            ("IntegrationLatency", "Average", "integration_latency_ms"),
            ("4XXError",           "Sum",     "errors_4xx"),
            ("5XXError",           "Sum",     "errors_5xx"),
            ("CacheHitCount",      "Sum",     "cache_hits"),
            ("CacheMissCount",     "Sum",     "cache_misses"),
        ],
    },
    "CloudFront": {
        "namespace":        "AWS/CloudFront",
        "dimension_key":    "DistributionId",
        "extra_dimensions": [{"Name": "Region", "Value": "Global"}],
        "metrics": [
            ("Requests",        "Sum",     "requests_total"),
            ("BytesDownloaded", "Sum",     "bytes_downloaded"),
            ("BytesUploaded",   "Sum",     "bytes_uploaded"),
            ("CacheHitRate",    "Average", "cache_hit_rate_pct"),
            ("4xxErrorRate",    "Average", "error_rate_4xx_pct"),
            ("5xxErrorRate",    "Average", "error_rate_5xx_pct"),
            ("TotalErrorRate",  "Average", "total_error_rate_pct"),
        ],
    },
    "CodeBuild": {
        "namespace":     "CodeBuild",
        "dimension_key": "ProjectName",
        "metrics": [
            ("Builds",          "Sum",     "builds_total"),
            ("FailedBuilds",    "Sum",     "failed_builds"),
            ("SucceededBuilds", "Sum",     "succeeded_builds"),
            ("Duration",        "Average", "avg_duration_s"),
        ],
    },
}

BATCH_SIZE = 500            # CloudWatch get_metric_data limit

# CloudWatch rate-limits GetMetricData per account per region. Regions are
# scanned in parallel, so without a shared gate the bursts collide and the
# retry path does the work the design should have avoided.
_CW_GATE = threading.Semaphore(4)
DEFAULT_PERIODS = [7, 30, 90]
DEFAULT_SPIKE_MULTIPLIER = 2.5

# This module measures; it does not price. Anything billable derived from these
# measurements is costed in collectors/service_costs.py using live per-region
# rates. (It previously carried us-east-1 NAT and Transfer rates inline, which
# were simply wrong in every other region.)


def _make_query_id(idx):
    return f"m{idx:06d}"


def _aggregate(values, stat):
    """
    Collapse a window of datapoints into one figure.

    The aggregation must match the statistic: the maximum over 30 days is the
    max of the per-period maxima, not their average — averaging maxima would
    quietly smooth away the very peak we collected them to see. Percentiles
    are averaged because CloudWatch already computed the percentile within
    each period, and averaging those is the closest defensible summary.
    """
    if not values:
        return None
    if stat == "Sum":
        return round(sum(values), 4)
    if stat == "Maximum":
        return round(max(values), 4)
    if stat == "Minimum":
        return round(min(values), 4)
    return round(sum(values) / len(values), 4)


def _detect_spikes(values, multiplier=DEFAULT_SPIKE_MULTIPLIER):
    if len(values) < 10:
        return []
    avg = statistics.mean(values)
    if avg == 0:
        return []
    return [
        {"value": round(v, 2), "avg": round(avg, 2), "ratio": round(v / avg, 1)}
        for v in values if v > avg * multiplier
    ]


def _config_for(resource, group_key):
    """Resource's own type wins; the group key is the fallback."""
    return METRICS_CONFIG.get(resource.get("type")) or METRICS_CONFIG.get(group_key)


def _dimensions_for(resource, config, extra=None):
    dims = [{
        "Name":  config["dimension_key"],
        "Value": str(resource.get(config.get("dimension_field", "id"), "")),
    }]
    for dim_name, field in config.get("extra_dimension_fields", []):
        value = resource.get(field)
        if value:
            dims.append({"Name": dim_name, "Value": str(value)})
    dims += config.get("extra_dimensions", [])
    dims += (extra or [])
    return dims


def _batch_fetch(cw, queries_with_meta, start, end, max_attempts=5):
    """
    Send up to 500 metric queries in one get_metric_data call.
    Returns {query_id: [values]}

    Throttling is RETRIED, not swallowed. Dropping a batch silently makes the
    whole report non-reproducible: one run collected CPU for 32/32 instances
    and the next, forty minutes later, got 23/32 — nine instances gated out and
    $178.92 of savings vanished with no infrastructure change. The gating was
    correct; the missing data was not. A total a client cannot reproduce is a
    total they cannot act on.
    """
    results = {}
    queries = [q["query"] for q in queries_with_meta]
    last_error = None

    def call(**extra):
        nonlocal last_error
        for attempt in range(max_attempts):
            try:
                with _CW_GATE:
                    return cw.get_metric_data(
                        MetricDataQueries=queries,
                        StartTime=start,
                        EndTime=end,
                        ScanBy="TimestampAscending",
                        **extra,
                    )
            except Exception as e:
                last_error = e
                # CloudWatch throttles per account per region; back off and
                # retry rather than losing the batch.
                if api_errors.classify(e) == "throttled" and attempt < max_attempts - 1:
                    time.sleep(min(20, 2 ** attempt))
                    continue
                api_errors.note(e, context="cloudwatch:GetMetricData")
                return None
        if last_error is not None:
            api_errors.note(last_error, context="cloudwatch:GetMetricData")
        return None

    resp = call()
    if resp is None:
        return results
    for result in resp.get("MetricDataResults", []):
        results.setdefault(result["Id"], []).extend(result.get("Values", []))

    while resp.get("NextToken"):
        resp = call(NextToken=resp["NextToken"])
        if resp is None:
            break
        for result in resp.get("MetricDataResults", []):
            results.setdefault(result["Id"], []).extend(result.get("Values", []))

    return results


def _period_seconds(days):
    """
    Keep each query under CloudWatch's per-request datapoint ceiling.
    1-hour granularity is plenty for a 7-day window but generates 2160 points
    over 90 days per metric, which forces heavy pagination for no extra signal.
    """
    if days <= 15:
        return 3600        # 1h
    if days <= 45:
        return 10800       # 3h
    return 21600           # 6h


def _collect_region(session, region, resource_configs, periods, spike_multiplier):
    """Collect all metrics for all resources in one region."""
    cw = session.client("cloudwatch", region_name=region)
    region_metrics = {}
    spike_window = max(periods)

    # Peak (min/max/p95) and CloudWatch Agent metrics are only ever READ at the
    # 30-day window — by the rules and by every sheet column. Querying them for
    # all five configured periods was half the total CloudWatch load for no
    # benefit, and it is what pushed a run into throttling: one report came
    # back with CPU for 23 of 32 instances and silently lost $178 of findings.
    primary = 30 if 30 in periods else min(periods, key=lambda p: abs(p - 30))

    for days in periods:
        end   = utcnow()
        start = end - timedelta(days=days)
        period_key = f"{days}d"
        resolution = _period_seconds(days)

        idx = 0
        queries_with_meta = []
        for resource, config in resource_configs:
            rid = resource["id"]

            for metric_name, stat, friendly_name in config["metrics"]:
                queries_with_meta.append({
                    "query": {
                        "Id": _make_query_id(idx),
                        "MetricStat": {
                            "Metric": {
                                "Namespace":  config["namespace"],
                                "MetricName": metric_name,
                                "Dimensions": _dimensions_for(resource, config),
                            },
                            "Period": resolution,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    },
                    "resource_id":   rid,
                    "friendly_name": friendly_name,
                    "stat":          stat,
                    "is_spike_src":  config.get("spike_metric") == metric_name,
                })
                idx += 1

            # Min / Max / p95 of the same metric, so rightsizing is judged on
            # the peak rather than an average that hides it.
            for peak in (config.get("peak_metrics", []) if days == primary else []):
                for stat, friendly_name in peak["stats"].items():
                    queries_with_meta.append({
                        "query": {
                            "Id": _make_query_id(idx),
                            "MetricStat": {
                                "Metric": {
                                    "Namespace":  peak.get("namespace", config["namespace"]),
                                    "MetricName": peak["name"],
                                    "Dimensions": _dimensions_for(resource, config),
                                },
                                "Period": resolution,
                                "Stat": stat,
                            },
                            "ReturnData": True,
                        },
                        "resource_id":   rid,
                        "friendly_name": friendly_name,
                        # Max-of-maxima and min-of-minima are the correct
                        # aggregations across the window; percentiles and
                        # averages are averaged.
                        "stat":          stat,
                        "is_spike_src":  False,
                    })
                    idx += 1

            # Metrics from other namespaces (CloudWatch Agent), each optionally
            # tried against several dimension shapes — first hit wins.
            for extra in (config.get("extra_metrics", []) if days == primary else []):
                for variant in extra.get("variants", [[]]):
                    queries_with_meta.append({
                        "query": {
                            "Id": _make_query_id(idx),
                            "MetricStat": {
                                "Metric": {
                                    "Namespace":  extra["namespace"],
                                    "MetricName": extra["name"],
                                    "Dimensions": _dimensions_for(resource, config, variant),
                                },
                                "Period": resolution,
                                "Stat": extra.get("stat", "Average"),
                            },
                            "ReturnData": True,
                        },
                        "resource_id":   rid,
                        "friendly_name": extra["key"],
                        "stat":          extra.get("stat", "Average"),
                        "is_spike_src":  False,
                    })
                    idx += 1

        for i in range(0, len(queries_with_meta), BATCH_SIZE):
            batch = queries_with_meta[i:i + BATCH_SIZE]
            raw = _batch_fetch(cw, batch, start, end)

            for q in batch:
                rid    = q["resource_id"]
                fname  = q["friendly_name"]
                values = raw.get(q["query"]["Id"], [])
                agg    = _aggregate(values, q["stat"])

                entry = region_metrics.setdefault(rid, {"periods": {}})
                bucket = entry["periods"].setdefault(period_key, {})
                # First non-None wins, so dimension variants don't clobber a hit.
                if bucket.get(fname) is None:
                    bucket[fname] = agg

                if days == spike_window and q["is_spike_src"] and values:
                    entry["_spike_raw"] = values

    # Spike detection over the longest window
    for resource, config in resource_configs:
        rid = resource["id"]
        entry = region_metrics.get(rid)
        if not entry:
            continue
        raw = entry.pop("_spike_raw", [])
        entry["datapoints"] = len(raw)
        if config.get("spike_metric"):
            entry["spikes"] = _detect_spikes(raw, spike_multiplier)
            entry["spike_metric"] = config["spike_metric"]
            entry["spike_window_days"] = spike_window
        else:
            entry["spikes"] = []

    for entry in region_metrics.values():
        entry.pop("_spike_raw", None)

    return region_metrics


def _derive(resource_type, resource, m):
    """Derived flags and cost estimates the sheets read directly."""
    p30 = m["periods"].get("30d") or {}

    if resource_type == "ElastiCache":
        evictions = p30.get("evictions") or 0
        m["has_evictions"] = evictions > 0
        m["evictions_30d"] = evictions

    elif resource_type == "EC2":
        c7  = m["periods"].get("7d", {}).get("cpu_credit_balance")
        c30 = p30.get("cpu_credit_balance")
        m["credit_starved"] = bool(
            c7 is not None and c30 and c30 > 0 and c7 < c30 * 0.30
        )
        m["cwagent_installed"] = p30.get("mem_used_pct") is not None

    elif resource_type == "EBS":
        total_ops = (p30.get("read_ops_avg") or 0) + (p30.get("write_ops_avg") or 0)
        m["zero_io"] = total_ops == 0 and p30.get("read_ops_avg") is not None

    elif resource_type == "NATGateway":
        bytes_in  = p30.get("bytes_in")
        bytes_out = p30.get("bytes_out")
        if bytes_in is None and bytes_out is None:
            m["total_gb_30d"] = None
            m["zero_traffic"] = None
        else:
            total_gb = round(((bytes_in or 0) + (bytes_out or 0)) / (1024 ** 3), 4)
            m["total_gb_30d"] = total_gb
            m["zero_traffic"] = total_gb == 0

    elif resource_type == "TransferFamily":
        bytes_in  = p30.get("bytes_in_30d")
        bytes_out = p30.get("bytes_out_30d")
        files_in  = p30.get("files_in_30d")
        files_out = p30.get("files_out_30d")
        if bytes_in is None and bytes_out is None:
            m["total_gb_30d"] = None
        else:
            m["total_gb_30d"] = round(((bytes_in or 0) + (bytes_out or 0)) / (1024 ** 3), 4)
        m["zero_activity"] = (
            None if files_in is None and files_out is None
            else ((files_in or 0) + (files_out or 0)) == 0)


def collect_all_metrics(session, grouped_resources, config=None):
    """
    Collect metrics for ALL resources using the batch API.

    config: the parsed config.yaml. `metrics.periods` and
    `metrics.spike_multiplier` are honoured here — previously both were
    hardcoded, so the settings in config.yaml did nothing.
    """
    metrics_cfg = (config or {}).get("metrics", {}) or {}
    periods = [int(p) for p in metrics_cfg.get("periods") or DEFAULT_PERIODS]
    periods = sorted({p for p in periods if p > 0}) or DEFAULT_PERIODS
    spike_multiplier = float(metrics_cfg.get("spike_multiplier") or DEFAULT_SPIKE_MULTIPLIER)

    by_region = {}
    for resource_type, resources in grouped_resources.items():
        for r in resources:
            cfg = _config_for(r, resource_type)
            if not cfg:
                continue
            region = r.get("region", "us-east-1")
            if region == "global":
                region = "us-east-1"
            by_region.setdefault(region, []).append((r, cfg))

    all_metrics = {}
    if not by_region:
        return all_metrics

    def fetch_region(region_data):
        region, resource_configs = region_data
        return _collect_region(session, region, resource_configs, periods, spike_multiplier)

    with ThreadPoolExecutor(max_workers=max(1, min(len(by_region), 6))) as ex:
        futures = {ex.submit(fetch_region, (region, rcs)): region
                   for region, rcs in by_region.items()}
        for future in as_completed(futures):
            try:
                all_metrics.update(future.result())
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                pass

    for resource_type, resources in grouped_resources.items():
        for r in resources:
            m = all_metrics.get(r["id"])
            if m:
                _derive(r.get("type", resource_type), r, m)

    return all_metrics
