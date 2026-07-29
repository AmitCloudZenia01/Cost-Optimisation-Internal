import inspect

from .writer import safe_add_worksheet, safe_update, apply_formats
from analysis.spike_detector import get_spike_summary

# ─── Design tokens ────────────────────────────────────────────────────────────
NAVY   = {"red": 0.098, "green": 0.216, "blue": 0.412}   # header bg  #192C69
WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
ROW_A  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}       # white
ROW_B  = {"red": 0.937, "green": 0.953, "blue": 0.984}     # #EFF3FB  soft blue-grey
GRN_TXT= {"red": 0.055, "green": 0.459, "blue": 0.188}     # #0E7530
RED_TXT= {"red": 0.729, "green": 0.098, "blue": 0.098}     # #BA1919
GRN_BG = {"red": 0.878, "green": 0.961, "blue": 0.902}     # #E0F5E6
RED_BG = {"red": 0.988, "green": 0.875, "blue": 0.875}     # #FCE0E0
ORG_BG = {"red": 1.0,   "green": 0.937, "blue": 0.788}     # #FFEF C9
YLW_BG = {"red": 1.0,   "green": 0.976, "blue": 0.796}     # #FFF9CB

# ─── Tab colours by service category ─────────────────────────────────────────
_TAB = {
    # Compute — amber
    "EC2": (1.0, 0.549, 0.0), "EKS": (1.0, 0.549, 0.0), "Lambda": (1.0, 0.549, 0.0),
    # Database — royal blue
    "RDS": (0.176, 0.431, 0.863), "ElastiCache": (0.176, 0.431, 0.863),
    "ElastiCacheReplicationGroup": (0.176, 0.431, 0.863),
    # Storage — forest green
    "S3": (0.122, 0.600, 0.278), "EBS": (0.122, 0.600, 0.278),
    # Networking — violet
    "ELB": (0.514, 0.137, 0.718), "ALB": (0.514, 0.137, 0.718),
    "NLB": (0.514, 0.137, 0.718), "ELBClassic": (0.514, 0.137, 0.718),
    "NATGateway": (0.514, 0.137, 0.718), "CloudFront": (0.514, 0.137, 0.718),
    "ElasticIPs": (0.514, 0.137, 0.718), "ElasticIP": (0.514, 0.137, 0.718),
    "Route53": (0.514, 0.137, 0.718),
    # Security — crimson
    "WAF": (0.776, 0.125, 0.125), "KMS": (0.776, 0.125, 0.125),
    "SecretsManager": (0.776, 0.125, 0.125),
    # DevOps / Containers — teal
    "ECR": (0.063, 0.510, 0.510), "CodeBuild": (0.063, 0.510, 0.510),
    "CodePipeline": (0.063, 0.510, 0.510),
    # Observability — slate
    "CWLogGroups": (0.376, 0.416, 0.510), "CWLogGroup": (0.376, 0.416, 0.510),
    # Messaging — gold  (SQS is defined once, below, with the other streaming services)
    "EventBridge": (0.839, 0.639, 0.0),
    # Transfer — sienna
    "TransferFamily": (0.545, 0.345, 0.098),
    # Databases (additional) — indigo
    "DynamoDB":    (0.294, 0.000, 0.510),
    "OpenSearch":  (0.294, 0.000, 0.510),
    "Redshift":    (0.294, 0.000, 0.510),
    # Containers — dark teal
    "ECS":         (0.000, 0.420, 0.420),
    "ECSService":  (0.000, 0.420, 0.420),
    # Messaging / Streaming — dark gold
    "SQS":         (0.700, 0.500, 0.000),
    "SNS":         (0.700, 0.500, 0.000),
    "Kinesis":     (0.700, 0.500, 0.000),
    "MSK":         (0.700, 0.500, 0.000),
    # Storage (additional) — dark green
    "EFS":         (0.000, 0.490, 0.200),
    # API — dark purple
    "APIGateway":  (0.420, 0.000, 0.600),
}

def _tab_rgb(service):
    t = _TAB.get(service)
    return {"red": t[0], "green": t[1], "blue": t[2]} if t else None

# ─── Column width heuristics ──────────────────────────────────────────────────
def _width(h):
    h = h.lower()
    if any(x in h for x in ("action","caveats","validation","origin domain","security group","gp2","opportunity","description")):
        return 255
    if any(x in h for x in ("name","dns","aliases","associated","image","add-on","spike","lifecycle")):
        return 165
    if any(x in h for x in ("instance id","volume id","resource id","db id","function","bucket","cluster id","server id","alloc")):
        return 140
    if any(x in h for x in ("usd","cost","saving")):
        return 120
    if any(x in h for x in ("%","rate","ratio","balance","hit rate","error rate")):
        return 92
    if any(x in h for x in ("region","az","zone","risk","scheme","scope","type","engine","platform","state","status")):
        return 102
    if any(x in h for x in ("enabled","managed","protect","encrypt","replicat","accessible","logging","multi","cwagent","attach","config","policy","rotation","public","scan","mutab")):
        return 82
    if any(x in h for x in ("date","created","modified","accessed","rotated","window","expiry")):
        return 108
    if any(x in h for x in ("gb","mb","bytes","iops","ops","throughput","latency","lag","duration","timeout","connections","requests","invoc","errors","days","age","builds","files","count","total")):
        return 108
    return 112

# ─── Number-format heuristics ─────────────────────────────────────────────────
def _nfmt(h):
    h = h.lower()
    if any(x in h for x in ("usd","cost","saving")):
        return {"type": "NUMBER", "pattern": '"$"#,##0.00'}
    if any(x in h for x in ("%","rate","ratio","balance","hit rate","error rate","success rate")):
        return {"type": "NUMBER", "pattern": "0.0"}
    if any(x in h for x in ("gb","mb")):
        return {"type": "NUMBER", "pattern": "#,##0.00"}
    if any(x in h for x in ("latency","duration","lag","timeout")):
        return {"type": "NUMBER", "pattern": "#,##0.0"}
    if any(x in h for x in ("ops","iops","requests","invoc","errors","throttles","builds","files","connections","count","total","days","age","nodes","replicas","volumes","layers","images","rules","records")):
        return {"type": "NUMBER", "pattern": "#,##0"}
    return None

def _is_numeric(h):
    h = h.lower()
    return any(x in h for x in ("usd","cost","saving","%","rate","gb","mb","ops","iops","count",
        "total","requests","invoc","errors","days","age","connections","latency","duration",
        "lag","balance","timeout","builds","files","nodes","replicas","volumes","layers",
        "images","rules","records","hits","misses","evictions"))

# ─── Helpers ──────────────────────────────────────────────────────────────────
def _p(m, d, f):
    v = (m or {}).get("periods", {}).get(f"{d}d", {}).get(f)
    return round(v, 2) if v is not None else ""

def _rec(recs, ph):
    items = recs.get(f"phase{ph}", [])
    if not items: return "", "", "", ""
    first = items[0]
    if not isinstance(first, dict): return str(first), "", "", ""
    act = first.get("action", "")
    for ex in items[1:]:
        if isinstance(ex, dict) and ex.get("action"):
            act += f"  //  {ex['action']}"
    return act, first.get("risk",""), " | ".join(first.get("caveats",[])), " | ".join(first.get("validation_steps",[]))

def _lw(recs):
    return " | ".join(recs.get("lifecycle_warnings", []))

def _blank(v):
    """Render a missing value as an empty cell, never as 0."""
    return "" if v in (None, "") else v

def _mb(v): return round(v / (1024**2), 4) if v not in (None,"") else ""
def _gb(v): return round(v / (1024**3), 2) if v not in (None,"") else ""
def _yn(v): return ("Yes" if v else "No") if isinstance(v, bool) else v
def _tag(r, k): return r.get("tags",{}).get(k,"")
def _dt(s): return (s or "")[:10]

def _srec(r):
    p1 = r.get("recommendations",{}).get("phase1",[])
    if p1 and isinstance(p1[0], dict): return p1[0].get("action",""), p1[0].get("risk","")
    return "", ""

# ─── Column definitions ───────────────────────────────────────────────────────
SERVICE_COLUMNS = {
  "EC2": [
    "Instance ID","Name","Region","AZ","Instance Type","State",
    "Platform","Launch Date","Age (Days)",
    "VPC","Subnet","Private IP","Public IP",
    "IAM Role","Key Pair","Security Groups","Auto Scaling Group",
    "EBS Volumes","AMI ID","AMI Architecture","SSM Managed","Apps Scanned",
    "Graviton Compat","x86-Only SW","ARM Verify SW",
    "Tag: Project","Tag: Owner","Tag: Env",
    "Hourly Cost (USD)","Monthly Cost (USD)",
    "CPU 7d%","CPU 15d%","CPU 30d%","CPU 60d%","CPU 90d%",
    "CPU Min 30d%","CPU p95 30d%","CPU Peak 30d%",
    "RAM 30d%","RAM p95 30d%","RAM Peak 30d%","Disk 30d%","Swap 30d%",
    "Net In 30d (MB/s)","Net Out 30d (MB/s)",
    "Disk Read Ops 30d","Disk Write Ops 30d",
    "CPU Credit Bal 7d","CPU Surplus Credits",
    "CWAgent Installed","Spike Notes","Lifecycle Warning",
    "P1 Action","P1 Risk","P1 Caveats","P1 Validation",
    "P2 Action","P2 Risk","P2 Caveats","P2 Validation",
    "P1 Savings/mo (USD)","P2 Savings/mo (USD)",
  ],
  "RDS": [
    "DB ID","Region","Instance Type","Engine","Engine Version",
    "State","Multi-AZ","Storage (GB)","Storage Type","IOPS",
    "VPC","Publicly Accessible","Deletion Protection",
    "Backup Retention (days)","Backup Window","Maint. Window",
    "Perf. Insights","Auto Minor Upgrade","Read Replicas","CA Cert",
    "Latest Snapshot (days old)",
    "Tag: Project","Tag: Owner",
    "Hourly Cost (USD)","Monthly Cost (USD)",
    "CPU 7d%","CPU 15d%","CPU 30d%","CPU 60d%","CPU 90d%",
    "CPU Min 30d%","CPU p95 30d%","CPU Peak 30d%",
    "DB Connections 30d","Read IOPS 30d","Write IOPS 30d",
    "Free Storage 30d (GB)","Freeable Mem 30d (GB)",
    "Read Latency 30d (ms)","Write Latency 30d (ms)",
    "Replica Lag 30d (s)","Swap 30d (MB)","Burst Balance 30d%",
    "Spike Notes","Lifecycle Warning",
    "P1 Action","P1 Risk","P1 Caveats","P1 Validation",
    "P2 Action","P2 Risk","P2 Caveats","P2 Validation",
    "P1 Savings/mo (USD)","P2 Savings/mo (USD)",
  ],
  "Lambda": [
    "Function","Region","Runtime","Architecture","Memory (MB)","Timeout (s)",
    "VPC Config","VPC ID","Layers","Ephemeral Storage (MB)","Reserved Concurrency",
    "Last Modified","Code Size (MB)","Description",
    "Tag: Project","Tag: Owner",
    "Invocations 7d","Invocations 30d","Invocations 90d",
    "Duration Avg 30d (ms)","Init Duration 30d (ms)",
    "Errors 30d","Error Rate 30d%","Throttles 30d","Concurrent Avg 30d",
    "Spike Notes","Lifecycle Warning",
    "P1 Action","P1 Risk","P1 Caveats","P1 Validation",
    "P2 Action","P2 Risk","P2 Caveats",
  ],
  "S3": [
    "Bucket","Region",
    "Standard Size (GB)","IA Size (GB)","Glacier Size (GB)",
    "Object Count","Versioning","Lifecycle Rules","Public Access Blocked",
    "Replication","Encryption",
    "Tag: Project","Tag: Owner",
    "Monthly Cost Est. (USD)",
  ],
  "EKS": [
    "Cluster","Node Group","Region","Instance Type",
    "Capacity Type","AMI Type",
    "Desired","Min","Max","Disk (GB)","Status",
    "K8s Version","Add-ons","Platform Version","Logging Enabled",
    "VPC","Created","Tag: Project","Tag: Owner",
    "Monthly Cost (USD)","Lifecycle Warning",
    "P1 Action","P1 Risk","P1 Caveats","P1 Validation",
  ],
  "ElastiCache": [
    "Cluster ID","Region","Instance Type","Engine","Engine Version",
    "Num Nodes","State",
    "Cluster Mode","Replication Group","Auth Token","TLS In-Transit",
    "At-Rest Encrypt","Backup Retention (days)","Maint. Window","Parameter Group",
    "Tag: Project","Tag: Owner","Monthly Cost (USD)",
    "CPU 30d%","CPU p95 30d%","CPU Peak 30d%","Cache Hits 30d","Cache Misses 30d","Hit Rate 30d%",
    "Evictions 30d","Freeable Mem 30d (GB)","Bytes Used 30d (GB)",
    "Connections 30d","Replication Lag 30d (s)",
    "Spike Notes","Lifecycle Warning",
    "P1 Action","P1 Risk","P1 Caveats",
    "P2 Action","P2 Risk",
    "P1 Savings/mo (USD)","P2 Savings/mo (USD)",
  ],
  "CloudFront": [
    "Distribution ID","Name","Domain","Status","Enabled",
    "Price Class","HTTP Version","IPv6",
    "Origins","Origin Domains","Behaviors","Custom Domains",
    "WAF Attached","Logging","Tag: Project",
    "Requests 30d","Downloaded 30d (GB)","Uploaded 30d (GB)",
    "Cache Hit Rate%","4xx Error Rate%","5xx Error Rate%",
    "Monthly Cost (USD)","Recommendation",
  ],
  "EBS": [
    "Volume ID","Name","Region","Type","Size (GB)","IOPS","Throughput (MB/s)",
    "State","Attached To","Multi-Attach","Encrypted","KMS Key",
    "Create Date","Age (Days)",
    "Snapshot Count","Latest Snapshot (days old)","gp2→gp3 Opportunity",
    "Monthly Cost (USD)",
    "Read Ops 30d","Write Ops 30d",
    "Read Bytes 30d (MB/s)","Write Bytes 30d (MB/s)",
    "Burst Balance%","Queue Depth","Zero IO",
    "Recommendation","Risk",
  ],
  "ELB": [
    "Name","Region","Type","Scheme","State",
    "Listeners","HTTPS Ports","SSL Cert Expiry",
    "Target Groups","Healthy Targets",
    "Access Logs","Deletion Protection","Idle Timeout (s)",
    "DNS Name","Created","Tag: Project","Tag: Owner",
    "Monthly Cost (USD)",
    "Requests 30d","Latency Avg (ms)","HTTP 5xx 30d","HTTP 4xx 30d",
    "Active Connections","New Connections 30d",
    "Recommendation","Risk",
  ],
  "NATGateway": [
    "NAT GW ID","Name","Region","VPC","Subnet","State",
    "Public IPs","Connectivity Type","Created",
    "Total GB 30d","Data Cost 30d (USD)","Monthly Fixed (USD)",
    "Est. Monthly Total (USD)","Zero Traffic",
    "Recommendation","Risk",
  ],
  "TransferFamily": [
    "Server ID","Name","Region","Domain","Protocols","Endpoint Type",
    "State","Users","Identity Provider",
    "Files In 30d","Files Out 30d","Total GB 30d","Data Cost 30d (USD)",
    "Monthly Fixed (USD)","Zero Activity",
    "Recommendation","Risk",
  ],
  "CWLogGroups": [
    "Log Group","Region","Stored (GB)","Retention (Days)",
    "Metric Filters","Subscription Filters","Last Event Date",
    "Monthly Cost (USD)","Recommendation","Risk",
  ],
  "ElasticIPs": [
    "Allocation ID","Name","Region","Public IP","Attached To",
    "Unattached","Monthly Cost (USD)","Recommendation",
  ],
  "WAF": [
    "WebACL ID","Name","Scope","Rule Count",
    "Managed Rules","Custom Rules","Capacity (WCU)",
    "Associations","Associated Resources",
    "Monthly Cost (USD)","Recommendation",
  ],
  "KMS": [
    "Key ID","Name/Description","Aliases","Region","State",
    "Key Spec","Key Usage","Rotation Enabled","Created",
    "Monthly Cost (USD)","Recommendation",
  ],
  "SecretsManager": [
    "Secret Name","Description","Region",
    "Rotation Enabled","Rotation Lambda",
    "Last Rotated","Next Rotation",
    "Last Accessed","Days Since Access","Last Changed",
    "KMS Key","Monthly Cost (USD)","Recommendation",
  ],
  "ECR": [
    "Repository","Region","Images","Untagged Images",
    "Size (GB)","Last Push Date","Lifecycle Policy",
    "Tag Mutability","Scan on Push",
    "Monthly Cost (USD)","Recommendation",
  ],
  "Route53": [
    "Zone Name","Zone Type","Record Count","Data Records",
    "Comment","Monthly Cost (USD)","Recommendation",
  ],
  "DynamoDB": [
    "Table Name", "Region", "Status", "Billing Mode",
    "Provisioned RCU", "Provisioned WCU",
    "GSI Count", "GSI RCU", "GSI WCU",
    "Size (GB)", "Item Count", "Stream Enabled",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "Consumed RCU 30d", "Consumed WCU 30d",
    "Throttled Requests 30d", "Latency Avg (ms)",
    "Recommendation",
  ],
  "OpenSearch": [
    "Domain", "Region", "Engine", "Engine Version",
    "Instance Type", "Instances", "Dedicated Master", "Master Type",
    "Storage (GB/node)", "Total Storage (GB)", "Volume Type",
    "Multi-AZ", "Endpoint",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "CPU 30d%", "Free Storage 30d (MB)", "JVM Memory 30d%",
    "Search Latency 30d (ms)", "Index Latency 30d (ms)",
    "Cluster Status Red", "Cluster Status Yellow",
    "Recommendation",
  ],
  "Redshift": [
    "Cluster", "Region", "Status", "Node Type", "Nodes",
    "Multi-AZ", "Database", "Master User",
    "VPC", "Publicly Accessible", "Encrypted",
    "Snapshot Retention (days)", "Maint. Window",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "CPU 30d%", "Disk Used 30d%", "DB Connections 30d",
    "Read IOPS 30d", "Write IOPS 30d",
    "Recommendation",
  ],
  "ECS": [
    "Cluster / Service", "Cluster", "Region",
    "Type", "Status", "Launch Type",
    "Running Tasks", "Pending Tasks", "Active Services",
    "Desired Count", "Running Count",
    "CPU Units", "Memory (MB)", "Task Definition",
    "Tag: Project", "Tag: Owner",
    "CPU 30d%", "Memory 30d%",
    "Recommendation",
  ],
  "SQS": [
    "Queue Name", "Region", "Queue Type",
    "Messages Available", "Messages In-Flight",
    "Visibility Timeout (s)", "Retention (days)", "Max Msg Size (KB)",
    "Delay (s)", "DLQ Configured", "KMS Key",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "Messages Sent 30d", "Messages Received 30d", "Messages Deleted 30d",
    "Oldest Msg Age (s)", "Empty Receives 30d",
    "Recommendation",
  ],
  "SNS": [
    "Topic Name", "Region", "Topic Type",
    "Subscriptions Confirmed", "Subscriptions Pending", "KMS Key",
    "Tag: Project", "Tag: Owner",
    "Messages Published 30d", "Notifications Delivered 30d",
    "Notifications Failed 30d",
    "Recommendation",
  ],
  "EFS": [
    "File System ID", "Name", "Region", "State",
    "Performance Mode", "Throughput Mode",
    "Encrypted", "KMS Key", "IA Enabled",
    "Size (GB)", "Mount Targets",
    "Created", "Age (Days)",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "Burst Credit Balance", "Client Connections 30d",
    "Recommendation",
  ],
  "APIGateway": [
    "API Name", "Region", "Protocol",
    "Stage Count", "Stages", "Endpoint Type",
    "Description", "Created",
    "Tag: Project", "Tag: Owner",
    "Requests 30d", "Latency Avg (ms)", "Integration Latency (ms)",
    "4xx Errors 30d", "5xx Errors 30d",
    "Cache Hits 30d", "Cache Misses 30d",
    "Recommendation",
  ],
  "Kinesis": [
    "Stream Name", "Region", "Status", "Stream Mode",
    "Shards", "Retention (hours)", "Consumers",
    "Encrypted",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "Incoming Records 30d", "Read Throttle% 30d", "Write Throttle% 30d",
    "Iterator Age (ms)",
    "Recommendation",
  ],
  "MSK": [
    "Cluster Name", "Region", "State", "Cluster Type",
    "Kafka Version", "Broker Type", "Broker Count",
    "Storage (GB/broker)",
    "Tag: Project", "Tag: Owner",
    "Monthly Cost (USD)",
    "CPU User% 30d", "Disk Used% 30d", "Under-Replicated Partitions",
    "Recommendation",
  ],
  "CodeBuild": [
    "Project","Region","Compute Type","Environment Type",
    "Source Type","Environment Image",
    "Last Build Status","Last Build Date",
    "Builds 30d","Failed 30d","Succeeded 30d","Success Rate%",
    "Avg Duration (s)","Recommendation",
  ],
}

SERVICE_COLUMNS["EBSSnapshot"] = [
    "Snapshot ID","Name","Region","Source Volume","Source Volume Exists",
    "Size (GB)","Storage Tier","State","Encrypted","Created","Age (Days)",
    "Description","Tag: Project","Tag: Owner",
    "Monthly Cost (USD)","Cost Basis","Recommendation","Risk",
]

# ALB / NLB discovered by RGTA land in their own groups but share the ELB layout.
SERVICE_COLUMNS["ALB"] = SERVICE_COLUMNS["ELB"]
SERVICE_COLUMNS["NLB"] = SERVICE_COLUMNS["ELB"]

# ─── Row builders ─────────────────────────────────────────────────────────────
def build_ec2_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); recs = r.get("recommendations",{})
        p1a,p1r,p1c,p1v = _rec(recs,1); p2a,p2r,p2c,p2v = _rec(recs,2)
        x86 = ", ".join(r.get("x86_only_software",[])) or "None"
        vrfy = ", ".join(r.get("arm_verify_software",[])) or ""
        fam = r.get("instance_type","").split(".")[0]
        gc = ("Already Graviton" if fam in {"t4g","m7g","m6g","c7g","c6g","r7g","r6g","im4gn","is4gen"}
              else "Blocked" if "Blocked" in p2a
              else "Safe" if "Migrate to Graviton" in p2a
              else "Check Required" if p2a else "N/A")
        out.append([
            r["id"],r.get("name",""),r.get("region",""),r.get("availability_zone",""),
            r.get("instance_type",""),r.get("state",""),r.get("platform","Linux"),
            _dt(r.get("launch_time")),r.get("age_days",""),
            r.get("vpc_id",""),r.get("subnet_id",""),r.get("private_ip",""),r.get("public_ip",""),
            r.get("iam_role",""),r.get("key_name",""),r.get("security_groups",""),r.get("auto_scaling_group",""),
            r.get("ebs_volume_count",""),r.get("image_id",""),r.get("ami_architecture","unknown"),
            _yn(r.get("ssm_managed")),r.get("ssm_app_count",0),gc,x86,vrfy,
            _tag(r,"Project"),_tag(r,"Owner"),_tag(r,"Environment"),
            r.get("hourly_cost_usd",""),r.get("monthly_cost_usd",""),
            _p(m,7,"cpu_avg_pct"),_p(m,15,"cpu_avg_pct"),_p(m,30,"cpu_avg_pct"),_p(m,60,"cpu_avg_pct"),_p(m,90,"cpu_avg_pct"),
            _p(m,30,"cpu_min_pct"),_p(m,30,"cpu_p95_pct"),_p(m,30,"cpu_max_pct"),
            _p(m,30,"mem_used_pct"),_p(m,30,"mem_p95_pct"),_p(m,30,"mem_max_pct"),
            _p(m,30,"disk_used_pct"),_p(m,30,"swap_used_pct"),
            _mb(_p(m,30,"network_in_bytes")),_mb(_p(m,30,"network_out_bytes")),
            _p(m,30,"disk_read_ops"),_p(m,30,"disk_write_ops"),
            _p(m,7,"cpu_credit_balance"),_p(m,30,"cpu_surplus_charged"),
            "Yes" if m.get("cwagent_installed") else "No",
            get_spike_summary(r["id"],m),_lw(recs),
            p1a,p1r,p1c,p1v,p2a,p2r,p2c,p2v,
            recs.get("savings_phase1_usd",""),recs.get("savings_phase2_usd",""),
        ])
    return out

def build_rds_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); recs = r.get("recommendations",{})
        p1a,p1r,p1c,p1v = _rec(recs,1); p2a,p2r,p2c,p2v = _rec(recs,2)
        rl2 = _p(m,30,"read_latency_s"); wl = _p(m,30,"write_latency_s")
        out.append([
            r["id"],r.get("region",""),r.get("instance_type",""),r.get("engine",""),r.get("engine_version",""),
            r.get("state",""),_yn(r.get("multi_az")),r.get("storage_gb",""),r.get("storage_type",""),r.get("iops",""),
            r.get("vpc_id",""),_yn(r.get("publicly_accessible")),_yn(r.get("deletion_protection")),
            r.get("backup_retention_days",""),r.get("backup_window",""),r.get("maintenance_window",""),
            _yn(r.get("performance_insights_enabled")),_yn(r.get("auto_minor_version_upgrade")),
            r.get("read_replica_count",""),r.get("ca_certificate",""),r.get("latest_snapshot_age_days",""),
            _tag(r,"Project"),_tag(r,"Owner"),
            r.get("hourly_cost_usd",""),r.get("monthly_cost_usd",""),
            _p(m,7,"cpu_avg_pct"),_p(m,15,"cpu_avg_pct"),_p(m,30,"cpu_avg_pct"),_p(m,60,"cpu_avg_pct"),_p(m,90,"cpu_avg_pct"),
            _p(m,30,"cpu_min_pct"),_p(m,30,"cpu_p95_pct"),_p(m,30,"cpu_max_pct"),
            _p(m,30,"db_connections_avg"),_p(m,30,"read_iops"),_p(m,30,"write_iops"),
            _gb(_p(m,30,"free_storage_bytes")),_gb(_p(m,30,"freeable_memory_bytes")),
            round(rl2*1000,2) if rl2 != "" else "",round(wl*1000,2) if wl != "" else "",
            _p(m,30,"replica_lag_s"),_mb(_p(m,30,"swap_usage_bytes")),_p(m,30,"burst_balance_pct"),
            get_spike_summary(r["id"],m),_lw(recs),
            p1a,p1r,p1c,p1v,p2a,p2r,p2c,p2v,
            recs.get("savings_phase1_usd",""),recs.get("savings_phase2_usd",""),
        ])
    return out

def build_lambda_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); recs = r.get("recommendations",{})
        p1a,p1r,p1c,p1v = _rec(recs,1); p2a,p2r,p2c,_ = _rec(recs,2)
        inv = _p(m,30,"invocations_total"); err = _p(m,30,"errors_total")
        erate = round(err/inv*100,2) if inv and err != "" else (0 if inv else "")
        out.append([
            r["id"],r.get("region",""),r.get("runtime",""),r.get("architecture","x86_64"),
            r.get("memory_mb",""),r.get("timeout_s",""),
            _yn(r.get("vpc_config")),r.get("vpc_id",""),r.get("layers_count",0),
            r.get("ephemeral_storage_mb",512),r.get("reserved_concurrency","Unreserved"),
            _dt(r.get("last_modified")),round(r.get("code_size_bytes",0)/1048576,3),r.get("description",""),
            _tag(r,"Project"),_tag(r,"Owner"),
            _p(m,7,"invocations_total"),inv,_p(m,90,"invocations_total"),
            _p(m,30,"duration_avg_ms"),_p(m,30,"init_duration_ms"),
            err,erate,_p(m,30,"throttles_total"),_p(m,30,"concurrent_avg"),
            get_spike_summary(r["id"],m),_lw(recs),
            p1a,p1r,p1c,p1v,p2a,p2r,p2c,
        ])
    return out

def build_s3_rows(rl):
    # Cost comes from collectors.service_costs, which prices each storage tier
    # at the live per-region rate. It was previously size_gb * 0.023 — the
    # us-east-1 Standard rate applied to every region and every storage class.
    return [[r["id"],r.get("region",""),r.get("size_gb",""),r.get("ia_size_gb",""),
             r.get("glacier_size_gb",""),r.get("object_count",""),r.get("versioning",""),
             r.get("lifecycle_rules_count",""),_yn(r.get("public_access_blocked")),
             _yn(r.get("replication_enabled")),r.get("encryption",""),
             _tag(r,"Project"),_tag(r,"Owner"),r.get("monthly_cost_usd","")]
            for r in rl]

def build_eks_rows(rl):
    out = []
    for r in rl:
        if r["type"] == "EKS":
            recs = r.get("recommendations",{})
            p1a,p1r,p1c,p1v = _rec(recs,1)
            out.append([r.get("name",""),"",r.get("region",""),"","","","","","","",
                        r.get("status",""),r.get("k8s_version",""),
                        r.get("addon_names","") or str(r.get("addon_count","")),
                        r.get("platform_version",""),_yn(r.get("logging_enabled")),
                        r.get("vpc_id",""),_dt(r.get("created")),
                        _tag(r,"Project"),_tag(r,"Owner"),
                        r.get("monthly_cost_usd",""),_lw(recs),p1a,p1r,p1c,p1v])
        else:
            out.append([r.get("cluster",""),r.get("name",""),r.get("region",""),r.get("instance_type",""),
                        r.get("capacity_type",""),r.get("ami_type",""),
                        r.get("desired",""),r.get("min",""),r.get("max",""),r.get("disk_size_gb",""),
                        r.get("status",""),"",r.get("release_version",""),"","","","",
                        _tag(r,"Project"),_tag(r,"Owner"),"","","","","",""])
    return out

def build_elasticache_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); recs = r.get("recommendations",{})
        p1a,p1r,p1c,_ = _rec(recs,1); p2a,p2r,_,_ = _rec(recs,2)
        hits = _p(m,30,"cache_hits"); miss = _p(m,30,"cache_misses")
        hr = round(hits/(hits+miss)*100,1) if hits != "" and miss != "" and (hits+miss)>0 else ""
        out.append([r["id"],r.get("region",""),r.get("instance_type",""),r.get("engine",""),r.get("engine_version",""),
                    r.get("num_nodes",""),r.get("status",""),r.get("cluster_mode",""),r.get("replication_group_id",""),
                    _yn(r.get("auth_token_enabled")),_yn(r.get("transit_encryption")),_yn(r.get("at_rest_encryption")),
                    r.get("backup_retention_days",""),r.get("maintenance_window",""),r.get("parameter_group",""),
                    _tag(r,"Project"),_tag(r,"Owner"),r.get("monthly_cost_usd",""),
                    _p(m,30,"cpu_avg_pct"),_p(m,30,"cpu_p95_pct"),_p(m,30,"cpu_max_pct"),
                    hits,miss,hr,_p(m,30,"evictions"),
                    _gb(_p(m,30,"freeable_memory_bytes")),_gb(_p(m,30,"bytes_used_for_cache")),
                    _p(m,30,"connections_avg"),_p(m,30,"replication_lag_s"),
                    get_spike_summary(r["id"],m),_lw(recs),
                    p1a,p1r,p1c,p2a,p2r,
                    recs.get("savings_phase1_usd",""),recs.get("savings_phase2_usd","")])
    return out

def build_cloudfront_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); a,_ = _srec(r)
        dl = _p(m,30,"bytes_downloaded"); ul = _p(m,30,"bytes_uploaded")
        out.append([r["id"],r.get("name",""),r.get("domain",""),r.get("status",""),_yn(r.get("enabled",True)),
                    r.get("price_class",""),r.get("http_version",""),_yn(r.get("ipv6_enabled")),
                    r.get("origins_count",""),r.get("origin_domains",""),r.get("behaviors_count",""),r.get("custom_domains",""),
                    r.get("waf_attached","No"),_yn(r.get("logging_enabled")),_tag(r,"Project"),
                    _p(m,30,"requests_total"),_gb(dl) if dl!="" else "",_gb(ul) if ul!="" else "",
                    _p(m,30,"cache_hit_rate_pct"),_p(m,30,"error_rate_4xx_pct"),_p(m,30,"error_rate_5xx_pct"),
                    r.get("monthly_cost_usd",""),a])
    return out

def build_ebs_rows(rl, _):
    out = []
    for r in rl:
        m = r.get("metrics",{}); a,risk = _srec(r)
        rb = m.get("read_bytes_avg"); wb = m.get("write_bytes_avg")
        out.append([r["id"],r.get("name",""),r.get("region",""),r.get("volume_type",""),r.get("size_gb",""),
                    r.get("iops",""),r.get("throughput_mbps",""),r.get("state",""),r.get("attached_to",""),
                    _yn(r.get("multi_attach")),_yn(r.get("encrypted")),r.get("kms_key_id",""),
                    r.get("create_date",""),r.get("age_days",""),
                    r.get("snapshot_count",""),r.get("latest_snapshot_age_days",""),r.get("gp3_opportunity",""),
                    r.get("monthly_cost_usd",""),
                    m.get("read_ops_avg",""),m.get("write_ops_avg",""),
                    round(rb/1048576,4) if rb else "",round(wb/1048576,4) if wb else "",
                    m.get("burst_balance_avg",""),m.get("queue_depth_avg",""),
                    "Yes" if m.get("zero_io") else "No",a,risk])
    return out

# NOTE: metrics_auto returns {"periods": {"30d": {...}}, <derived flags>}.
# These three builders used to read the per-period metrics as flat top-level
# keys, so every metric column on the ELB / NAT / Transfer tabs was blank.
# Per-period values come from _p(); derived totals stay at the top level.
def build_elb_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); a,risk = _srec(r)
        lat = _p(m,30,"latency_avg_s")
        out.append([r.get("name",""),r.get("region",""),r.get("lb_type",""),r.get("scheme",""),r.get("state",""),
                    r.get("listener_count",""),r.get("https_ports",""),r.get("ssl_cert_expiry",""),
                    r.get("target_group_count",""),r.get("healthy_targets",""),
                    _yn(r.get("access_logs_enabled")),_yn(r.get("deletion_protection")),r.get("idle_timeout_s",""),
                    r.get("dns_name",""),_dt(r.get("created")),_tag(r,"Project"),_tag(r,"Owner"),
                    r.get("monthly_cost_usd",""),
                    _p(m,30,"request_count_30d"),round(lat*1000,2) if lat!="" else "",
                    _p(m,30,"http_5xx_30d"),_p(m,30,"http_4xx_30d"),
                    _p(m,30,"active_connections"),_p(m,30,"new_connections"),a,risk])
    return out

def build_nat_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); a,risk = _srec(r)
        zero = m.get("zero_traffic")
        out.append([r["id"],r.get("name",""),r.get("region",""),r.get("vpc_id",""),r.get("subnet_id",""),
                    r.get("state",""),r.get("public_ips",""),r.get("connectivity_type",""),_dt(r.get("created")),
                    _blank(m.get("total_gb_30d")),
                    _blank(r.get("nat_data_cost_usd")),
                    _blank(r.get("nat_fixed_cost_usd")),
                    _blank(r.get("monthly_cost_usd")),
                    "" if zero is None else ("Yes" if zero else "No"), a, risk])
    return out

def build_transfer_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); a,risk = _srec(r)
        zero = m.get("zero_activity")
        out.append([r["id"],r.get("name",""),r.get("region",""),r.get("domain",""),r.get("protocols",""),
                    r.get("endpoint_type",""),r.get("state",""),r.get("user_count",""),r.get("identity_provider",""),
                    _p(m,30,"files_in_30d"),_p(m,30,"files_out_30d"),
                    _blank(m.get("total_gb_30d")),
                    _blank(r.get("transfer_data_cost_usd")),
                    _blank(r.get("monthly_cost_usd")),
                    "" if zero is None else ("Yes" if zero else "No"), a, risk])
    return out

def build_cwlogs_rows(rl, _):
    out = []
    for r in rl:
        a,risk = _srec(r)
        out.append([r["id"],r.get("region",""),r.get("stored_gb",""),r.get("retention_days","Not set"),
                    r.get("metric_filter_count",""),r.get("subscription_filter_count",""),
                    r.get("last_event_date",""),r.get("monthly_cost_usd",""),a,risk])
    return out

def build_eip_rows(rl, _):
    return [[r["id"],r.get("name",""),r.get("region",""),r.get("public_ip",""),r.get("attached_to",""),
             "Yes" if r.get("unattached") else "No",r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_waf_rows(rl, _):
    return [[r["id"],r.get("name",""),r.get("scope",""),r.get("rule_count",""),
             r.get("managed_rules",""),r.get("custom_rules",""),r.get("capacity_wcu",""),
             r.get("associations",""),r.get("associated_resources",""),
             r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_kms_rows(rl, _):
    return [[r["id"],r.get("name",""),r.get("aliases",""),r.get("region",""),r.get("key_state",""),
             r.get("key_spec",""),r.get("key_usage",""),_yn(r.get("rotation_enabled")),r.get("created",""),
             r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_secrets_rows(rl, _):
    return [[r.get("name",""),r.get("description",""),r.get("region",""),
             _yn(r.get("rotation_enabled")),r.get("rotation_lambda",""),
             r.get("last_rotated",""),r.get("next_rotation",""),
             r.get("last_accessed",""),r.get("days_since_access",""),r.get("last_changed",""),
             r.get("kms_key_id",""),r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_ecr_rows(rl, _):
    return [[r["id"],r.get("region",""),r.get("image_count",""),r.get("untagged_images",""),
             r.get("size_gb",""),r.get("last_push_date",""),_yn(r.get("lifecycle_policy")),
             r.get("tag_mutability",""),_yn(r.get("scan_on_push")),
             r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_route53_rows(rl, _):
    return [[r.get("name",""),r.get("zone_type",""),r.get("record_count",""),r.get("data_records",""),
             r.get("comment",""),r.get("monthly_cost_usd",""),_srec(r)[0]] for r in rl]

def build_dynamodb_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("status",""),
            r.get("billing_mode",""),
            r.get("provisioned_rcu",""), r.get("provisioned_wcu",""),
            r.get("gsi_count",""), r.get("gsi_rcu",""), r.get("gsi_wcu",""),
            r.get("size_gb",""), r.get("item_count",""),
            _yn(r.get("stream_enabled")),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("consumed_rcu",""), p30.get("consumed_wcu",""),
            p30.get("throttled_requests",""), p30.get("latency_avg_ms",""),
            a,
        ])
    return out


def build_opensearch_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("engine",""),
            r.get("engine_version",""), r.get("instance_type",""),
            r.get("instance_count",""), _yn(r.get("dedicated_master")),
            r.get("master_type",""),
            r.get("volume_size_gb",""), r.get("total_storage_gb",""),
            r.get("volume_type",""), _yn(r.get("multi_az")),
            r.get("endpoint",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("cpu_avg_pct",""), p30.get("free_storage_mb",""),
            p30.get("jvm_memory_pct",""), p30.get("search_latency_ms",""),
            p30.get("index_latency_ms",""),
            p30.get("status_red",""), p30.get("status_yellow",""),
            a,
        ])
    return out


def build_redshift_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("status",""),
            r.get("node_type",""), r.get("node_count",""),
            _yn(r.get("multi_az")), r.get("database",""),
            r.get("master_user",""), r.get("vpc_id",""),
            _yn(r.get("publicly_accessible")), _yn(r.get("encrypted")),
            r.get("snapshot_retention_days",""), r.get("maintenance_window",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("cpu_avg_pct",""), p30.get("disk_used_pct",""),
            p30.get("db_connections_avg",""),
            p30.get("read_iops",""), p30.get("write_iops",""),
            a,
        ])
    return out


def build_ecs_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        if r["type"] == "ECS":
            out.append([
                r["id"], "", r.get("region",""),
                "Cluster", r.get("status",""), "",
                r.get("running_tasks",""), r.get("pending_tasks",""),
                r.get("active_services",""), "", "",
                "", "", "",
                _tag(r,"Project"), _tag(r,"Owner"),
                p30.get("cpu_avg_pct",""), p30.get("memory_avg_pct",""),
                a,
            ])
        else:
            out.append([
                r["id"], r.get("cluster",""), r.get("region",""),
                "Service", r.get("status",""), r.get("launch_type",""),
                "", "", "",
                r.get("desired_count",""), r.get("running_count",""),
                r.get("cpu_units",""), r.get("memory_mb",""),
                r.get("task_definition",""),
                _tag(r,"Project"), _tag(r,"Owner"),
                p30.get("cpu_avg_pct",""), p30.get("memory_avg_pct",""),
                a,
            ])
    return out


def build_sqs_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("queue_type",""),
            r.get("messages_available",""), r.get("messages_in_flight",""),
            r.get("visibility_timeout_s",""), r.get("retention_days",""),
            r.get("max_message_size_kb",""), r.get("delay_seconds",""),
            _yn(r.get("dlq_configured")), r.get("kms_key",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("messages_sent",""), p30.get("messages_received",""),
            p30.get("messages_deleted",""), p30.get("oldest_msg_age_s",""),
            p30.get("empty_receives",""),
            a,
        ])
    return out


def build_sns_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("topic_type",""),
            r.get("subscriptions_confirmed",""),
            r.get("subscriptions_pending",""),
            r.get("kms_key",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            p30.get("messages_published",""),
            p30.get("notifications_delivered",""),
            p30.get("notifications_failed",""),
            a,
        ])
    return out


def build_efs_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("name",""), r.get("region",""),
            r.get("state",""), r.get("performance_mode",""),
            r.get("throughput_mode",""), _yn(r.get("encrypted")),
            r.get("kms_key",""), _yn(r.get("ia_enabled")),
            r.get("size_gb",""), r.get("mount_targets",""),
            r.get("created",""), r.get("age_days",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("burst_credit_balance",""),
            p30.get("client_connections",""),
            a,
        ])
    return out


def build_apigateway_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r.get("name",""), r.get("region",""), r.get("protocol",""),
            r.get("stage_count",""), r.get("stages",""),
            r.get("endpoint_type",""), r.get("description",""),
            r.get("created","")[:10] if r.get("created") else "",
            _tag(r,"Project"), _tag(r,"Owner"),
            p30.get("requests_total",""), p30.get("latency_avg_ms",""),
            p30.get("integration_latency_ms",""),
            p30.get("errors_4xx",""), p30.get("errors_5xx",""),
            p30.get("cache_hits",""), p30.get("cache_misses",""),
            a,
        ])
    return out


def build_kinesis_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("status",""),
            r.get("stream_mode",""), r.get("shard_count",""),
            r.get("retention_hours",""), r.get("consumer_count",""),
            _yn(r.get("encrypted")),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("incoming_records",""),
            p30.get("read_throttle_pct",""),
            p30.get("write_throttle_pct",""),
            p30.get("iterator_age_ms",""),
            a,
        ])
    return out


def build_msk_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"], {})
        a, _ = _srec(r)
        p30 = m.get("periods", {}).get("30d", {})
        out.append([
            r["id"], r.get("region",""), r.get("state",""),
            r.get("cluster_type",""), r.get("kafka_version",""),
            r.get("broker_type",""), r.get("broker_count",""),
            r.get("storage_gb_per_broker",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            r.get("monthly_cost_usd",""),
            p30.get("cpu_user_pct",""),
            p30.get("disk_used_pct",""),
            p30.get("under_replicated",""),
            a,
        ])
    return out


def build_snapshot_rows(rl, _):
    out = []
    for r in rl:
        a, risk = _srec(r)
        basis = r.get("cost_basis") or {}
        out.append([
            r["id"], r.get("name",""), r.get("region",""),
            r.get("volume_id",""),
            "No" if r.get("orphaned") else "Yes",
            r.get("volume_size_gb",""), r.get("storage_tier",""),
            r.get("state",""), _yn(r.get("encrypted")),
            _dt(r.get("start_time")), r.get("age_days",""),
            r.get("description",""),
            _tag(r,"Project"), _tag(r,"Owner"),
            _blank(r.get("monthly_cost_usd")),
            basis.get("description","") if isinstance(basis, dict) else "",
            a, risk,
        ])
    return out


def build_codebuild_rows(rl, am):
    out = []
    for r in rl:
        m = am.get(r["id"],{}); a,_ = _srec(r)
        b = _p(m,30,"builds_total"); s = _p(m,30,"succeeded_builds"); f = _p(m,30,"failed_builds")
        sr = round(s/b*100,1) if b and s != "" else ""
        out.append([r.get("name",""),r.get("region",""),r.get("compute_type",""),r.get("environment_type",""),
                    r.get("source_type",""),r.get("image",""),r.get("last_build_status",""),r.get("last_build_date",""),
                    b,f,s,sr,_p(m,30,"avg_duration_s"),a])
    return out

# ─── Generic builder for any unknown service type ─────────────────────────────
# cost_basis is a dict; passing it raw made gspread throw, which left the
# EBSSnapshot tab created-but-empty. Skip the structured fields and
# stringify anything else non-scalar defensively.
_SKIP = {"type","tags","metrics","recommendations","cost_basis",
         "saving_basis","evidence"}
_PRIO = ["id","name","region","state","status","instance_type","engine",
         "engine_version","size_gb","monthly_cost_usd","hourly_cost_usd","vpc_id"]

def build_generic_rows(rl, am):
    if not rl: return [], []
    seen, fields = set(), []
    for f in _PRIO:
        for r in rl:
            if f in r and f not in seen and f not in _SKIP:
                fields.append(f); seen.add(f); break
    for r in rl:
        for f in r:
            if f not in seen and f not in _SKIP:
                fields.append(f); seen.add(f)
    tag_keys = sorted({k for r in rl for k in r.get("tags",{})})
    mkkeys   = sorted({k for r in rl for k in am.get(r["id"],{}).get("periods",{}).get("30d",{})})
    headers  = ([f.replace("_"," ").title() for f in fields]
                + [f"Tag: {k}" for k in tag_keys]
                + [f"{k.replace('_',' ').title()} 30d" for k in mkkeys]
                + ["P1 Action","P1 Risk","P1 Savings/mo (USD)","P2 Action","P2 Risk","P2 Savings/mo (USD)","Lifecycle"])
    rows = []
    for r in rl:
        row = []
        for f in fields:
            v = r.get(f,"")
            if isinstance(v,bool): v = "Yes" if v else "No"
            elif isinstance(v,dict): v = "; ".join(f"{k}={x}" for k,x in v.items())[:400]
            elif isinstance(v,(list,tuple,set)): v = ", ".join(str(x) for x in v)
            row.append(v)
        for tk in tag_keys: row.append(r.get("tags",{}).get(tk,""))
        p30 = am.get(r["id"],{}).get("periods",{}).get("30d",{})
        for mk in mkkeys:
            v = p30.get(mk); row.append(round(v,4) if v is not None else "")
        recs = r.get("recommendations",{})
        p1a,p1r,_,_ = _rec(recs,1); p2a,p2r,_,_ = _rec(recs,2)
        row += [p1a,p1r,recs.get("savings_phase1_usd",""),p2a,p2r,recs.get("savings_phase2_usd",""),_lw(recs)]
        rows.append(row)
    return headers, rows

ROW_BUILDERS = {
    "EC2": build_ec2_rows, "RDS": build_rds_rows, "Lambda": build_lambda_rows,
    "S3": lambda rl,_: build_s3_rows(rl), "EKS": lambda rl,_: build_eks_rows(rl),
    "ElastiCache": build_elasticache_rows, "CloudFront": build_cloudfront_rows,
    "EBS": build_ebs_rows, "ELB": build_elb_rows, "ALB": build_elb_rows,
    "NLB": build_elb_rows,
    "NATGateway": build_nat_rows, "TransferFamily": build_transfer_rows,
    "CWLogGroups": build_cwlogs_rows, "ElasticIPs": build_eip_rows,
    "WAF": build_waf_rows, "KMS": build_kms_rows, "SecretsManager": build_secrets_rows,
    "ECR": build_ecr_rows, "Route53": build_route53_rows, "CodeBuild": build_codebuild_rows,
    # New services
    "DynamoDB":   build_dynamodb_rows,
    "OpenSearch":  build_opensearch_rows,
    "Redshift":    build_redshift_rows,
    "ECS":         build_ecs_rows,
    "ECSService":  build_ecs_rows,
    "SQS":         build_sqs_rows,
    "SNS":         build_sns_rows,
    "EFS":         build_efs_rows,
    "APIGateway":  build_apigateway_rows,
    "Kinesis":     build_kinesis_rows,
    "MSK":         build_msk_rows,
    "EBSSnapshot": build_snapshot_rows,
}

# ─── Format builder ───────────────────────────────────────────────────────────
def _fmt(sheet_id, headers, nrows, tab_color=None):
    n = len(headers); er = nrows + 1
    reqs = []

    # Tab colour
    if tab_color:
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "tabColor": tab_color},
            "fields": "tabColor"}})

    # Header row height 38 px
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id,"dimension":"ROWS","startIndex":0,"endIndex":1},
        "properties": {"pixelSize": 38}, "fields": "pixelSize"}})

    # Header cell style
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id,"startRowIndex":0,"endRowIndex":1},
        "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE,"bold": True,"fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"}})

    # Freeze row 1
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id,"gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"}})

    # Alternating row banding — only over actual data rows
    if nrows > 0:
        reqs.append({"addBanding": {"bandedRange": {
            "range": {"sheetId": sheet_id,"startRowIndex":1,"endRowIndex":er,
                      "startColumnIndex":0,"endColumnIndex":n},
            "rowProperties": {"firstBandColor": ROW_A,"secondBandColor": ROW_B}}}})

    # Filter dropdowns on all columns
    if nrows > 0:
        reqs.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": sheet_id,"startRowIndex":0,"endRowIndex":er,
                      "startColumnIndex":0,"endColumnIndex":n}}}})

    # Per-column width + number format + alignment
    for i, h in enumerate(headers):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id,"dimension":"COLUMNS","startIndex":i,"endIndex":i+1},
            "properties": {"pixelSize": _width(h)}, "fields": "pixelSize"}})

        fmt = _nfmt(h)
        num = _is_numeric(h)
        align = "RIGHT" if (num or fmt) else "LEFT"
        cell_fmt = {"horizontalAlignment": align}
        if fmt: cell_fmt["numberFormat"] = fmt
        if i < 2: cell_fmt["textFormat"] = {"bold": True}
        fields = ["horizontalAlignment"]
        if fmt: fields.append("numberFormat")
        if i < 2: fields.append("textFormat")
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id,"startRowIndex":1,"endRowIndex":er,
                      "startColumnIndex":i,"endColumnIndex":i+1},
            "cell": {"userEnteredFormat": cell_fmt},
            "fields": "userEnteredFormat(" + ",".join(fields) + ")"}})

    # Conditional: State/Status → green or red text
    for i,h in enumerate(headers):
        if h.lower() in ("state","status","last build status"):
            for vals, clr in [
                (["running","active","available","healthy","succeeded","enabled"], GRN_TXT),
                (["stopped","failed","error","deleted","inactive","deprecated"],   RED_TXT),
            ]:
                for v in vals:
                    reqs.append({"addConditionalFormatRule": {"rule": {
                        "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                                    "startColumnIndex":i,"endColumnIndex":i+1}],
                        "booleanRule": {
                            "condition": {"type":"TEXT_CONTAINS","values":[{"userEnteredValue":v}]},
                            "format": {"textFormat":{"foregroundColor":clr,"bold":True}}}},
                        "index":0}})
            break

    # Conditional: P1 Savings > 0 → yellow bg
    for i,h in enumerate(headers):
        if "P1 Savings" in h:
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                            "startColumnIndex":i,"endColumnIndex":i+1}],
                "booleanRule": {
                    "condition": {"type":"NUMBER_GREATER","values":[{"userEnteredValue":"0"}]},
                    "format": {"backgroundColor": YLW_BG}}},
                "index":0}})
            break

    # Conditional: Monthly Cost ≥500 → red bg, 100-499 → orange bg
    for i,h in enumerate(headers):
        if "Monthly Cost" in h:
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                            "startColumnIndex":i,"endColumnIndex":i+1}],
                "booleanRule": {
                    "condition": {"type":"NUMBER_GREATER_THAN_EQ","values":[{"userEnteredValue":"500"}]},
                    "format": {"backgroundColor": RED_BG}}},
                "index":0}})
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                            "startColumnIndex":i,"endColumnIndex":i+1}],
                "booleanRule": {
                    "condition": {"type":"NUMBER_BETWEEN",
                                  "values":[{"userEnteredValue":"100"},{"userEnteredValue":"499.99"}]},
                    "format": {"backgroundColor": ORG_BG}}},
                "index":0}})
            break

    # Conditional: Unattached EIP / Zero IO / Zero Traffic → red bg
    for i,h in enumerate(headers):
        if h in ("Unattached","Zero IO","Zero Traffic","Zero Activity"):
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                            "startColumnIndex":i,"endColumnIndex":i+1}],
                "booleanRule": {
                    "condition": {"type":"TEXT_EQ","values":[{"userEnteredValue":"Yes"}]},
                    "format": {"backgroundColor": RED_BG,"textFormat":{"foregroundColor":RED_TXT,"bold":True}}}},
                "index":0}})

    # Conditional: Publicly Accessible = Yes → orange bg
    for i,h in enumerate(headers):
        if h == "Publicly Accessible":
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":er,
                            "startColumnIndex":i,"endColumnIndex":i+1}],
                "booleanRule": {
                    "condition": {"type":"TEXT_EQ","values":[{"userEnteredValue":"Yes"}]},
                    "format": {"backgroundColor": ORG_BG}}},
                "index":0}})

    return reqs

# ─── Entry point ──────────────────────────────────────────────────────────────
def build_service_page(spreadsheet, service, resources_list, all_metrics):
    if not resources_list:
        return None

    builder = ROW_BUILDERS.get(service)
    columns = SERVICE_COLUMNS.get(service)

    if builder and columns:
        # Inspect the arity rather than catching TypeError: the old form
        # swallowed any TypeError raised *inside* the builder (e.g. arithmetic
        # on None) and retried with the wrong number of arguments, replacing
        # the real traceback with a misleading one.
        try:
            takes_metrics = len(inspect.signature(builder).parameters) >= 2
        except (TypeError, ValueError):
            takes_metrics = True
        data_rows = builder(resources_list, all_metrics) if takes_metrics else builder(resources_list)
        headers = columns
    else:
        headers, data_rows = build_generic_rows(resources_list, all_metrics)

    n  = max(len(headers), 5)
    ws = safe_add_worksheet(spreadsheet, service, rows=len(data_rows)+5, cols=n+3)
    safe_update(ws, "A1", [headers] + data_rows, value_input_option="USER_ENTERED")
    apply_formats(spreadsheet, _fmt(ws.id, headers, len(data_rows), _tab_rgb(service)))
    return ws
