import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_transfer_servers(session, regions):
    resources = []
    for region in regions:
        client = session.client("transfer", region_name=region)
        try:
            paginator = client.get_paginator("list_servers")
            for page in paginator.paginate():
                for srv in page["Servers"]:
                    detail = {}
                    try:
                        detail = client.describe_server(ServerId=srv["ServerId"])["Server"]
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    tags = {t["Key"]: t["Value"] for t in detail.get("Tags", [])}

                    # Count users
                    user_count = 0
                    try:
                        users = client.list_users(ServerId=srv["ServerId"])
                        user_count = len(users.get("Users", []))
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass

                    resources.append({
                        "type": "TransferFamily",
                        "id": srv["ServerId"],
                        "name": tags.get("Name", srv["ServerId"]),
                        "region": region,
                        "domain": detail.get("Domain", srv.get("Domain", "")),
                        "protocols": ", ".join(detail.get("Protocols", srv.get("Protocols", []))),
                        "endpoint_type": detail.get("EndpointType", ""),
                        "state": srv.get("State", detail.get("State", "")),
                        "user_count": user_count,
                        "identity_provider": detail.get("IdentityProviderType", ""),
                        "logging_role": detail.get("LoggingRole", ""),
                        "tags": tags,
                    })
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources