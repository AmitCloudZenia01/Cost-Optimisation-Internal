"""
Two checks per EC2 instance:

1. AMI Architecture — describe_images on the instance's AMI.
   Returns "x86_64" or "arm64". This tells us whether the OS
   is currently running in Intel mode or ARM-native mode.

2. SSM Inventory — if SSM agent is installed, AWS already
   collects an application inventory. We pull it and scan for
   known x86-only software without touching the instance at all.
"""

import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collectors import api_errors

# Software that has NO ARM64 / Graviton support
# Pattern is matched case-insensitively against SSM AWS:Application inventory names
X86_ONLY_SIGNATURES = [
    ("Oracle Database",         ["oracle database", "oracle db", "oracle rdbms"]),
    ("Microsoft SQL Server",    ["sql server", "mssql", "sqlservr"]),
    ("Citrix",                  ["citrix virtual", "citrix receiver", "citrix workspace"]),
    ("SAP HANA",                ["sap hana", "hdbdaemon"]),
    ("McAfee/Trellix",          ["mcafee", "trellix agent"]),
    ("Trend Micro",             ["trend micro", "ds_agent"]),
    ("Symantec",                ["symantec endpoint", "sep "]),
    ("IBM Db2",                 ["db2", "ibm db2"]),
    ("Informatica",             ["informatica powercenter", "informatica data"]),
    ("TIBCO",                   ["tibco"]),
    ("Teradata",                ["teradata"]),
    ("32-bit only app",         ["(x86)", " x86 ", "32-bit", "32bit"]),
]

# Software that HAS ARM64 support but needs version check
ARM_REQUIRES_VERSION_CHECK = [
    ("Java/JDK",  ["jdk", "java se", "openjdk", "amazon corretto", "graalvm"]),
    ("Node.js",   ["node.js", "nodejs"]),
    ("Python",    ["python 3", "python 2"]),
    ("Go",        ["go programming", "golang"]),
    ("Docker",    ["docker engine", "docker desktop"]),
    ("nginx",     ["nginx"]),
    ("Apache",    ["apache http", "httpd"]),
    ("MySQL",     ["mysql server", "mysql community"]),
    ("PostgreSQL",["postgresql"]),
    ("Redis",     ["redis"]),
]


def get_ami_architectures(session, region, image_ids):
    """Batch-fetch AMI architecture for a set of AMI IDs in one API call."""
    if not image_ids:
        return {}
    ec2 = session.client("ec2", region_name=region)
    try:
        resp = ec2.describe_images(ImageIds=list(set(image_ids)))
        return {img["ImageId"]: img.get("Architecture", "x86_64") for img in resp["Images"]}
    except Exception:
        return {}


def get_ssm_managed_instances(session, region, instance_ids):
    """Return set of instance IDs that have SSM agent installed and reporting."""
    ssm = session.client("ssm", region_name=region)
    managed = set()
    try:
        paginator = ssm.get_paginator("describe_instance_information")
        for page in paginator.paginate(
            Filters=[{"Key": "InstanceIds", "Values": list(instance_ids)}]
        ):
            for info in page.get("InstanceInformationList", []):
                managed.add(info["InstanceId"])
    except Exception as _e:
        api_errors.note(_e, region=locals().get('region', ''))
        pass
    return managed


def get_ssm_application_inventory(session, instance_id, region):
    """
    Pull the AWS:Application inventory from SSM for one instance.
    Returns list of dicts with Name, Version, Publisher.
    """
    ssm = session.client("ssm", region_name=region)
    apps = []
    try:
        # botocore has no paginator for list_inventory_entries — asking for one
        # raised "Operation cannot be paginated" on every instance, so the
        # installed-software list was always empty and the Graviton x86-only
        # check silently fell back to "unable to verify". Page by hand.
        token = None
        while True:
            kwargs = {"InstanceId": instance_id, "TypeName": "AWS:Application",
                      "MaxResults": 50}
            if token:
                kwargs["NextToken"] = token
            resp = ssm.list_inventory_entries(**kwargs)
            for entry in resp.get("Entries", []):
                apps.append({
                    "name": entry.get("Name", ""),
                    "version": entry.get("Version", ""),
                    "publisher": entry.get("Publisher", ""),
                })
            token = resp.get("NextToken")
            if not token:
                break
    except Exception as _e:
        api_errors.note(_e, region=region)
    return apps


def _scan_apps(apps):
    """
    Scan installed app list against known incompatibility signatures.
    Returns:
        blockers: list of (software_name, matched_app) — confirmed x86-only
        warnings: list of (software_name, matched_app) — needs version verification
    """
    blockers = []
    warnings = []
    app_names_lower = " | ".join(
        f"{a['name']} {a['publisher']}".lower() for a in apps
    )

    for display_name, patterns in X86_ONLY_SIGNATURES:
        for pat in patterns:
            if pat in app_names_lower:
                blockers.append(display_name)
                break

    for display_name, patterns in ARM_REQUIRES_VERSION_CHECK:
        for pat in patterns:
            if pat in app_names_lower:
                warnings.append(display_name)
                break

    return blockers, warnings


def enrich_ec2_with_graviton_signals(session, ec2_resources):
    """
    Main entry point. For every EC2 instance:
    - Adds ami_architecture (x86_64 / arm64)
    - Adds ssm_managed (bool)
    - Adds x86_only_software (list of blocker names)
    - Adds arm_verify_software (list of software needing version check)

    All calls are read-only. Gracefully skips if permissions are missing.
    """
    by_region = {}
    for r in ec2_resources:
        region = r.get("region", "us-east-1")
        by_region.setdefault(region, []).append(r)

    for region, instances in by_region.items():
        # 1. Batch-fetch AMI architectures
        image_ids = [r.get("image_id") for r in instances if r.get("image_id")]
        arch_map = get_ami_architectures(session, region, image_ids)
        for r in instances:
            r["ami_architecture"] = arch_map.get(r.get("image_id", ""), "unknown")

        # 2. Find SSM-managed instances
        instance_ids = [r["id"] for r in instances]
        managed = get_ssm_managed_instances(session, region, instance_ids)

        # 3. Pull inventory for managed instances (parallel)
        def fetch_inventory(instance_id):
            if instance_id not in managed:
                return instance_id, []
            return instance_id, get_ssm_application_inventory(session, instance_id, region)

        inventory_map = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(fetch_inventory, r["id"]): r["id"] for r in instances}
            for future in as_completed(futures):
                iid, apps = future.result()
                inventory_map[iid] = apps

        # 4. Scan inventory and attach signals to each resource
        for r in instances:
            iid = r["id"]
            r["ssm_managed"] = iid in managed
            apps = inventory_map.get(iid, [])
            blockers, verify_list = _scan_apps(apps)
            r["x86_only_software"] = blockers
            r["arm_verify_software"] = verify_list
            r["ssm_app_count"] = len(apps)

    return ec2_resources
