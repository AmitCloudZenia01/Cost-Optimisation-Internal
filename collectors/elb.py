import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_load_balancers(session, regions):
    resources = []
    for region in regions:
        client = session.client("elbv2", region_name=region)
        try:
            paginator = client.get_paginator("describe_load_balancers")
            for page in paginator.paginate():
                for lb in page["LoadBalancers"]:
                    tags = {}
                    try:
                        t = client.describe_tags(ResourceArns=[lb["LoadBalancerArn"]])
                        tags = {td["Key"]: td["Value"] for td in t["TagDescriptions"][0].get("Tags", [])}
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # Count target groups and targets
                    tg_count = 0
                    healthy_targets = 0
                    try:
                        tgs = client.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])
                        tg_count = len(tgs["TargetGroups"])
                        for tg in tgs["TargetGroups"]:
                            health = client.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                            healthy_targets += sum(1 for t in health["TargetHealthDescriptions"]
                                                   if t["TargetHealth"]["State"] == "healthy")
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # Attributes: access logs, deletion protection, idle timeout
                    access_logs = False
                    deletion_protection = False
                    idle_timeout = ""
                    try:
                        attrs_resp = client.describe_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"])
                        for attr in attrs_resp["Attributes"]:
                            if attr["Key"] == "access_logs.s3.enabled":
                                access_logs = attr["Value"] == "true"
                            elif attr["Key"] == "deletion_protection.enabled":
                                deletion_protection = attr["Value"] == "true"
                            elif attr["Key"] == "idle_timeout.timeout_seconds":
                                idle_timeout = attr["Value"]
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # Listeners: count + SSL cert expiry
                    listener_count = 0
                    ssl_cert_expiry = ""
                    https_ports = ""
                    try:
                        listeners = client.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"]).get("Listeners", [])
                        listener_count = len(listeners)
                        https_list = [str(l.get("Port", "")) for l in listeners if l.get("Protocol") in ("HTTPS", "TLS")]
                        https_ports = ", ".join(https_list)
                        # Get SSL cert expiry from first HTTPS listener
                        for lst in listeners:
                            if lst.get("Protocol") in ("HTTPS", "TLS"):
                                for cert in lst.get("Certificates", []):
                                    cert_arn = cert.get("CertificateArn", "")
                                    if cert_arn and ":acm:" in cert_arn:
                                        try:
                                            acm = session.client("acm", region_name=region)
                                            cert_det = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]
                                            expiry = cert_det.get("NotAfter")
                                            if expiry:
                                                ssl_cert_expiry = expiry.strftime("%Y-%m-%d")
                                        except Exception as _e:
                                            api_errors.note(_e, region=locals().get('region', ''))
                                            pass
                                    if ssl_cert_expiry:
                                        break
                            if ssl_cert_expiry:
                                break
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    # CloudWatch's LoadBalancer dimension is the full
                    # "app/<name>/<id>" suffix of the ARN. Using only the last
                    # two segments ("<name>/<id>") matches no metric at all.
                    lb_arn = lb["LoadBalancerArn"]
                    lb_dimension = lb_arn.split("loadbalancer/", 1)[-1]

                    lb_kind = lb.get("Type", "application")
                    metric_type = {"application": "ELB",
                                   "network": "NLB",
                                   "gateway": "GWLB"}.get(lb_kind, "ELB")

                    resources.append({
                        "type": metric_type,
                        "id": lb_dimension,
                        "name": lb["LoadBalancerName"],
                        "region": region,
                        "lb_type": lb_kind,
                        "scheme": lb.get("Scheme", ""),
                        # AWS bills one public IPv4 per AZ an internet-facing
                        # load balancer is enabled in. Without the count the
                        # charge cannot be priced without guessing.
                        "az_count": len(lb.get("AvailabilityZones") or []),
                        "state": lb.get("State", {}).get("Code", ""),
                        "dns_name": lb.get("DNSName", ""),
                        "arn": lb["LoadBalancerArn"],
                        "target_group_count": tg_count,
                        "healthy_targets": healthy_targets,
                        "listener_count": listener_count,
                        "https_ports": https_ports,
                        "ssl_cert_expiry": ssl_cert_expiry,
                        "access_logs_enabled": access_logs,
                        "deletion_protection": deletion_protection,
                        "idle_timeout_s": idle_timeout,
                        "created": lb.get("CreatedTime", "").isoformat() if lb.get("CreatedTime") else "",
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass

        # Classic ELBs
        try:
            classic = session.client("elb", region_name=region)
            resp = classic.describe_load_balancers()
            for lb in resp["LoadBalancerDescriptions"]:
                resources.append({
                    # Classic LBs live in the AWS/ELB namespace with a
                    # LoadBalancerName dimension — a different metric shape
                    # from ALB/NLB, so they get their own type.
                    "type": "ELBClassic",
                    "id": lb["LoadBalancerName"],
                    "name": lb["LoadBalancerName"],
                    "region": region,
                    "lb_type": "classic",
                    "scheme": lb.get("Scheme", ""),
                    "az_count": len(lb.get("AvailabilityZones") or []),
                    "state": "active",
                    "dns_name": lb.get("DNSName", ""),
                    "arn": "",
                    "target_group_count": len(lb.get("Instances", [])),
                    "healthy_targets": len(lb.get("Instances", [])),
                    "created": lb.get("CreatedTime", "").isoformat() if lb.get("CreatedTime") else "",
                    "tags": {},
                })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources