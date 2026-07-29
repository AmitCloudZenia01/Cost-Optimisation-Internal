# AWS Cost Optimization Report

A **read-only** AWS cost analysis tool. It scans an account, prices every
resource from live data, and publishes a multi-tab Google Sheet of
evidence-backed savings opportunities.

The design principle, which everything else follows from:

> **No dollar figure appears unless it can be traced to an AWS API response or
> a published price fetched at runtime. If something cannot be verified with
> read-only data, the report says "unable to verify" rather than guessing.**

There are no hardcoded prices, no fallback percentages, and no estimated
savings dressed up as measured ones. A blank cell with a stated reason is a
correct report; a confident wrong number is not.

---

## Confidence tiers

Every saving is labelled, and the tiers are **never summed into one headline**:

| Tier | Meaning |
| --- | --- |
| **Confirmed** | Derived from billing data — AWS's own Savings Plan / Reserved Instance recommendations, or a CUR-backed cost baseline |
| **Estimated** | Correct arithmetic on live *list* prices. Can be wrong if the account holds commitments, so it is reported separately |
| **Unpriced** | A real, actionable finding whose dollar impact cannot be established read-only. Shown with no number |

A saving is only **Confirmed** when its baseline came from billing. List price
is materially wrong for an account holding Reserved Instances or Savings Plans,
so the tool detects existing commitments and downgrades confidence when they
exist.

## How savings are calculated

Savings are the difference between two **live prices**, never a percentage:

```
Rightsize m5.xlarge -> m5.large        $147.46/mo -> $73.73/mo    = $73.73
Graviton  m5.large  -> m7g.large       $ 73.73/mo -> $42.56/mo    = $31.17
                                                          total     $104.90
m5.xlarge costs $147.46 · m7g.large costs $42.56 · true saving $104.90  ✓
```

Overlapping actions **compound rather than sum**. Rightsizing, Graviton and a
Reserved Instance all change what you pay for the same instance, so each is
priced from the previous step's end state. Storage reductions are billed
separately and are genuinely additive.

## What it checks

**33 rules** across EC2, RDS, ElastiCache, EBS, snapshots, ELB/ALB/NLB, NAT,
Elastic IPs, S3, EFS, ECR, KMS, Secrets Manager, WAF, Transfer Family, EKS,
Lambda and CloudWatch Logs — rightsizing, Graviton, commitments, idle
resources, orphaned snapshots, missing lifecycle policies, and version
end-of-life.

Safety checks that *prevent* bad advice:

- **Peak-aware rightsizing** — judged on p95, not the average. An instance at 8% average CPU touching 95% is not downsized.
- **Headroom** — the observed peak must still fit the smaller instance on **both** vCPU and memory after the resize.
- **Performance Insights** — a database at 10% CPU and 80% IO-wait is not CPU-bound; downsizing is blocked.
- **Commitment awareness** — never recommends a reservation for capacity already covered.
- **Instance existence** — targets are validated against `ec2:DescribeInstanceTypeOfferings` and `rds:DescribeOrderableDBInstanceOptions`, so it cannot suggest a class AWS does not sell.

## Output

| Tab | Contents |
| --- | --- |
| **Summary** | Spend, Confirmed vs Estimated savings, and the cost basis stated up front |
| **Recommendations** | Every action with its saving, confidence, price basis and evidence |
| **Data Gaps** | Everything that could not be measured or priced, why, and how to fix it |
| **Data Transfer** | Inter-AZ / egress spend from actual billing, with guidance |
| **Commitments** | Existing RIs and Savings Plans, plus AWS's own purchase recommendations |
| **All Services (Billing)** | Every service in Cost Explorer, flagging billing-only ones |
| **Changes** | Differential against the previous run |
| Per-service tabs | Resource-level detail, metrics and per-resource findings |

## Requirements

- Python 3.9+
- AWS credentials with **`ReadOnlyAccess`** plus `ce:GetCostAndUsage`
- Google credentials — a service-account JSON, or an OAuth client (browser consent on first run)
- *Optional:* a free [Vantage Instances API](https://instances-api.vantage.sh) token

Nothing is ever created, modified or deleted in the AWS account. Every call is
a `describe`/`list`/`get`. The CUR reader deliberately reads Parquet/CSV
straight from S3 rather than using Athena, which would write query results.

## Pricing sources

| Source | Provides |
| --- | --- |
| **CUR** (if configured) | Actual billed cost, effective discount, commitment coverage |
| **AWS Price List** | Every service rate, per region — Query API, falling back to the public bulk list which needs no credentials |
| **Vantage Instances API** | Instance on-demand **and real Reserved Instance** rates |
| **Cost Explorer** | Spend history, data transfer, AWS's own purchase recommendations |

Supply the Vantage token by environment variable or a git-ignored file —
**never commit it**:

```bash
export VANTAGE_API_TOKEN=...        # or
echo '...' > .vantage_token         # git-ignored
```

## Running

```bash
python3 run.py                                  # interactive, prompts for everything
python3 main.py --config config.yaml --gcp-sa creds.json
python3 main.py --config config.yaml --dry-run  # no spreadsheet
```

Cost Explorer bills ~$0.01/request; a run makes roughly 10, so a few cents.

## Configuration

See [`config.yaml`](config.yaml). Every setting is read at runtime.

```yaml
metrics:
  periods: [7, 15, 30, 60, 90]   # drives which per-period columns can populate
recommendations:
  phase1:
    cpu_max_avg: 40              # 30-day average ceiling
    cpu_peak_max: 70             # refuse to downsize if p95 exceeds this
    target_max_utilisation: 75   # projected load on the SMALLER instance
```

Thresholds are **policy**, not facts — they are yours to set. Prices are never
configured, only fetched.

## Tests

```bash
python3 tests/audit.py                  # 12 structural checks
python3 tests/test_no_fabrication.py    # 23 anti-fabrication checks
```

Each check exists because the corresponding bug shipped at least once: metric
key mismatches, column drift, dicts reaching the spreadsheet, savings counted
twice, undefined names hidden by broad `except` blocks. The suite fails the
build if a hardcoded price or fallback percentage reappears.

## Known limits

Stated plainly, because the tool states them in its own output too:

- **Without a CUR**, savings are quoted against list price and labelled Estimated. Existing commitments are detected so the distortion is at least known.
- **Per-request services** (Lambda, SQS, SNS, API Gateway, CloudFront, CodeBuild) cannot be given a per-resource monthly cost from inventory alone.
- **Snapshot cost is an upper bound** — AWS exposes no per-snapshot incremental size.
- **Memory is invisible without the CloudWatch Agent**, which caps rightsizing confidence. The report names the affected instances and their combined spend.
- **ElastiCache and Lambda runtime EOL** have no AWS API and remain dated reference data — labelled as such and never priced.

## Security

- Read-only against AWS.
- `run.py` copies credentials to a `0600` directory under the system temp dir, never the project folder, and deletes them on exit.
- `.gitignore` covers `*credentials*.json`, `*client_secret*.json`, `.vantage_token`, `tmp*.json` and `snapshots/`.

## License

Proprietary — © CloudZenia. Internal use only.
