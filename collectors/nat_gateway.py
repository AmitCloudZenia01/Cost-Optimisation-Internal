import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_nat_gateways(session, regions):
    resources = []
    for region in regions:
        ec2 = session.client("ec2", region_name=region)
        try:
            paginator = ec2.get_paginator("describe_nat_gateways")
            for page in paginator.paginate(Filter=[{"Name": "state", "Values": ["available", "pending"]}]):
                for ngw in page["NatGateways"]:
                    tags = {t["Key"]: t["Value"] for t in ngw.get("Tags", [])}
                    eips = [a.get("PublicIp", "") for a in ngw.get("NatGatewayAddresses", [])]
                    resources.append({
                        "type": "NATGateway",
                        "id": ngw["NatGatewayId"],
                        "name": tags.get("Name", ngw["NatGatewayId"]),
                        "region": region,
                        "vpc_id": ngw.get("VpcId", ""),
                        "subnet_id": ngw.get("SubnetId", ""),
                        "state": ngw.get("State", ""),
                        "public_ips": ", ".join(eips),
                        "connectivity_type": ngw.get("ConnectivityType", "public"),
                        "created": ngw.get("CreateTime", "").isoformat() if ngw.get("CreateTime") else "",
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources