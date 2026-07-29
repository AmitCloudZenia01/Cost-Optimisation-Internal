"""
Maps AWS Cost Explorer service names to the collectors we have.

When the pipeline runs:
1. Cost Explorer returns the list of services with actual spend.
2. For each service, we look it up here.
3. If found → run the full collector + metrics + recommender.
4. If not found → billing-only entry (cost trend, no resource details).

To add coverage for a new service:
  - Write a collector in collectors/
  - Add an entry to SERVICE_REGISTRY below
  - Add column definitions + row builder in sheets/service_pages.py
  - Add a recommender in analysis/recommender.py
"""

# Maps Cost Explorer service display name → internal service key
# Multiple CE names can map to the same key (e.g., "EC2 - Other" → "EBS")
CE_NAME_TO_KEY = {
    # Compute
    "Amazon Elastic Compute Cloud - Compute":           "EC2",
    "EC2 - Other":                                       "EBS_NAT_EIP",  # handled specially
    "Amazon Elastic Container Service for Kubernetes":  "EKS",
    "Amazon Elastic Container Service":                 "ECS",
    "AWS Lambda":                                        "Lambda",
    "AWS Batch":                                         "Batch",
    "Amazon Lightsail":                                  "Lightsail",
    "Amazon AppStream":                                  "AppStream",
    "AWS Elastic Beanstalk":                             "ElasticBeanstalk",

    # Databases
    "Amazon Relational Database Service":               "RDS",
    "Amazon ElastiCache":                               "ElastiCache",
    "Amazon DynamoDB":                                  "DynamoDB",
    "Amazon Redshift":                                  "Redshift",
    "Amazon Neptune":                                   "Neptune",
    "Amazon DocumentDB":                                "DocumentDB",
    "Amazon MemoryDB":                                  "MemoryDB",
    "Amazon Timestream":                                "Timestream",
    "Amazon Keyspaces":                                 "Keyspaces",
    "Amazon Aurora":                                    "RDS",  # part of RDS

    # Storage
    "Amazon Simple Storage Service":                    "S3",
    "Amazon Elastic File System":                       "EFS",
    "Amazon FSx":                                       "FSx",
    "Amazon Glacier":                                   "Glacier",
    "AWS Backup":                                       "Backup",
    "Amazon Storage Gateway":                           "StorageGateway",

    # Networking
    "Amazon Virtual Private Cloud":                     "VPC_NAT",  # NAT Gateways
    "Amazon CloudFront":                                "CloudFront",
    "Amazon Route 53":                                  "Route53",
    "Amazon Registrar":                                 "Route53",
    "Amazon API Gateway":                               "APIGateway",
    "AWS Global Accelerator":                           "GlobalAccelerator",
    "Amazon Elastic Load Balancing":                    "ELB",
    "AWS Direct Connect":                               "DirectConnect",
    "AWS VPN":                                          "VPN",
    "AWS PrivateLink":                                  "PrivateLink",

    # Transfer & Integration
    "AWS Transfer Family":                              "TransferFamily",
    "Amazon MQ":                                        "MQ",
    "Amazon Simple Queue Service":                      "SQS",
    "Amazon Simple Notification Service":               "SNS",
    "Amazon EventBridge":                               "EventBridge",
    "AWS Step Functions":                               "StepFunctions",
    "Amazon Kinesis":                                   "Kinesis",
    "Amazon Kinesis Firehose":                          "KinesisFirehose",
    "Amazon MSK":                                       "MSK",
    "AWS DataSync":                                     "DataSync",
    "Amazon AppFlow":                                   "AppFlow",

    # ML / AI
    "Amazon SageMaker":                                 "SageMaker",
    "Amazon Rekognition":                               "Rekognition",
    "Amazon Comprehend":                                "Comprehend",
    "Amazon Textract":                                  "Textract",
    "Amazon Bedrock":                                   "Bedrock",
    "Amazon Polly":                                     "Polly",
    "Amazon Transcribe":                                "Transcribe",
    "Amazon Lex":                                       "Lex",
    "AWS Panorama":                                     "Panorama",

    # Security
    "AWS Key Management Service":                       "KMS",
    "AWS Secrets Manager":                              "SecretsManager",
    "AWS WAF":                                          "WAF",
    "Amazon GuardDuty":                                 "GuardDuty",
    "Amazon Inspector":                                 "Inspector",
    "AWS Shield":                                       "Shield",
    "Amazon Macie":                                     "Macie",
    "AWS Security Hub":                                 "SecurityHub",
    "AWS Certificate Manager":                          "ACM",
    "Amazon Cognito":                                   "Cognito",
    "AWS Single Sign-On":                               "SSO",
    "AWS IAM Identity Center":                          "SSO",
    "Amazon Detective":                                 "Detective",
    "AWS Firewall Manager":                             "FirewallManager",

    # DevOps
    "Amazon EC2 Container Registry (ECR)":              "ECR",
    "AWS CodeBuild":                                    "CodeBuild",
    "CodeBuild":                                        "CodeBuild",
    "AWS CodePipeline":                                 "CodePipeline",
    "AWS CodeDeploy":                                   "CodeDeploy",
    "AWS CodeCommit":                                   "CodeCommit",
    "AWS CodeArtifact":                                 "CodeArtifact",
    "Amazon ECR Public":                                "ECR",

    # Monitoring & Management
    "AmazonCloudWatch":                                 "CWLogGroups",
    "Amazon CloudWatch":                                "CWLogGroups",
    "AWS CloudTrail":                                   "CloudTrail",
    "AWS Config":                                       "Config",
    "AWS Systems Manager":                              "SSM",
    "AWS Trusted Advisor":                              "TrustedAdvisor",
    "AWS Health":                                       "Health",
    "Amazon Managed Grafana":                           "Grafana",
    "Amazon Managed Service for Prometheus":            "Prometheus",
    "AWS X-Ray":                                        "XRay",

    # Analytics
    "Amazon Athena":                                    "Athena",
    "Amazon EMR":                                       "EMR",
    "AWS Glue":                                         "Glue",
    "Amazon QuickSight":                                "QuickSight",
    "Amazon OpenSearch Service":                        "OpenSearch",
    "Amazon Elasticsearch Service":                     "OpenSearch",
    "AWS Lake Formation":                               "LakeFormation",
    "Amazon Kinesis Data Analytics":                    "KinesisAnalytics",

    # Media
    "AWS Elemental MediaConvert":                       "MediaConvert",
    "AWS Elemental MediaLive":                          "MediaLive",
    "Amazon IVS":                                       "IVS",

    # Other
    "AWS Support":                                      "Support",
    "AWS Marketplace":                                  "Marketplace",
    "Tax":                                              "Tax",
    "AWS Cost Explorer":                                "CostExplorer",
    "Amazon Simple Email Service":                      "SES",
    "AWS CloudShell":                                   "CloudShell",
}

# Services we have FULL collectors + metrics + recommenders for
FULLY_SUPPORTED = {
    "EC2", "RDS", "Lambda", "EKS", "ElastiCache", "S3", "CloudFront",
    "EBS", "ELB", "NATGateway", "TransferFamily", "CWLogGroups",
    "ElasticIPs", "WAF", "KMS", "SecretsManager", "ECR",
    "Route53", "CodeBuild",
}

# Services we surface billing data for but have no deep collectors
# These appear on the "Uncovered Services" sheet
BILLING_ONLY = set()  # populated dynamically at runtime

# Services to skip entirely (tax, internal AWS charges)
SKIP_SERVICES = {"Tax", "CostExplorer", "CloudShell", "Support", "Marketplace"}


def classify_services(monthly_costs):
    """
    Given monthly_costs from Cost Explorer, return:
      covered:   {key: ce_name} — services we have full collectors for
      billing_only: {key: ce_name} — services with spend but no collector
      skipped:   [ce_name] — services we intentionally skip
    """
    covered = {}
    billing_only = {}
    skipped = []

    seen_keys = set()
    for row in monthly_costs:
        ce_name = row["service"]
        key = CE_NAME_TO_KEY.get(ce_name)

        if not key:
            # Unknown service — billing only
            if ce_name not in billing_only:
                billing_only[ce_name] = {"key": ce_name, "ce_name": ce_name, "coverage": "billing_only"}
            continue

        if key in SKIP_SERVICES:
            skipped.append(ce_name)
            continue

        if key not in seen_keys:
            seen_keys.add(key)
            if key in FULLY_SUPPORTED:
                covered[key] = ce_name
            else:
                billing_only[key] = {"key": key, "ce_name": ce_name, "coverage": "billing_only"}

    return covered, billing_only, skipped


def get_service_cost_summary(monthly_costs):
    """Returns {service_key_or_name: {month: cost, total: cost}} for all services."""
    from collections import defaultdict
    summary = defaultdict(lambda: {"by_month": {}, "total": 0.0})
    for row in monthly_costs:
        ce_name = row["service"]
        key = CE_NAME_TO_KEY.get(ce_name, ce_name)
        summary[key]["by_month"][row["month"]] = summary[key]["by_month"].get(row["month"], 0) + row["cost"]
        summary[key]["total"] += row["cost"]
    return dict(summary)
