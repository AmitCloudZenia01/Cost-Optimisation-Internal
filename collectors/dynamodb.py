import boto3
from datetime import datetime, timedelta
import statistics

from utils import utcnow
from collectors import api_errors


def get_dynamodb_tables(session, regions):
    resources = []
    for region in regions:
        client = session.client("dynamodb", region_name=region)
        try:
            paginator = client.get_paginator("list_tables")
            for page in paginator.paginate():
                for table_name in page["TableNames"]:
                    try:
                        detail = client.describe_table(TableName=table_name)["Table"]
                        tags = {}
                        try:
                            tags = {t["Key"]: t["Value"] for t in
                                    client.list_tags_of_resource(ResourceArn=detail["TableArn"]).get("Tags", [])}
                        except Exception as _e:
                            api_errors.note(_e, region=locals().get('region', ''))
                            pass

                        billing_mode = detail.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
                        prov = detail.get("ProvisionedThroughput", {})
                        size_bytes = detail.get("TableSizeBytes", 0)
                        item_count = detail.get("ItemCount", 0)

                        gsi_count = len(detail.get("GlobalSecondaryIndexes", []))
                        gsi_rcu = sum(g.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0)
                                      for g in detail.get("GlobalSecondaryIndexes", []))
                        gsi_wcu = sum(g.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0)
                                      for g in detail.get("GlobalSecondaryIndexes", []))

                        resources.append({
                            "type": "DynamoDB",
                            "id": table_name,
                            "name": table_name,
                            "region": region,
                            "status": detail.get("TableStatus", ""),
                            "billing_mode": billing_mode,
                            "provisioned_rcu": prov.get("ReadCapacityUnits", 0),
                            "provisioned_wcu": prov.get("WriteCapacityUnits", 0),
                            "gsi_count": gsi_count,
                            "gsi_rcu": gsi_rcu,
                            "gsi_wcu": gsi_wcu,
                            "size_gb": round(size_bytes / (1024 ** 3), 4),
                            "item_count": item_count,
                            "stream_enabled": bool(detail.get("StreamSpecification", {}).get("StreamEnabled")),
                            "tags": tags,
                        })
                    except Exception as _e:
                        api_errors.note(_e, region=locals().get('region', ''))
                        pass
        except Exception as _e:
            api_errors.note(_e, region=locals().get('region', ''))
            pass
    return resources