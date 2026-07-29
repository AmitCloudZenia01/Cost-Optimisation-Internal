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

This matters most when you put a figure in front of a client: if the tool
cannot prove it, it will not print it — so anything it does print, you can
defend.

---

## Contents

1. [Setup](#1-setup)
2. [Running it](#2-running-it)
3. [Reading the report](#3-reading-the-report)
4. [Confidence tiers](#4-confidence-tiers)
5. [How savings are calculated](#5-how-savings-are-calculated)
6. [Enabling a CUR](#6-enabling-a-cur)
7. [Troubleshooting](#7-troubleshooting)
8. [Sharing the tool](#8-sharing-the-tool)
9. [Tests](#9-tests)
10. [Configuration](#10-configuration)
11. [Known limits](#11-known-limits)

---

## 1. Setup

### Requirements

- Python 3.9+
- AWS credentials with **`ReadOnlyAccess`** plus `ce:GetCostAndUsage`
- Google credentials — a service-account JSON, or an OAuth client (browser
  consent on first run)
- *Optional:* a free [Vantage Instances API](https://instances-api.vantage.sh)
  token

### AWS credentials

Every call the tool makes is a `describe`, `list` or `get`. Nothing is ever
created, modified or deleted. Confirm you are pointed at the right account
before running:

```bash
aws sts get-caller-identity
```

> **Why read-only is a design constraint, not a default.**
> The CUR reader pulls Parquet and CSV straight from S3 rather than querying
> Athena — Athena would write query results and require a Glue table. The
> read-only guarantee is what makes this safe to run in a client account.

### Google credentials

The report is published to Google Sheets. You need either a service-account
JSON or an OAuth client, from Google Cloud Console → APIs & Services →
Credentials.

### Vantage token — optional but worth it

Gives on-demand **and real Reserved Instance** rates. Without it the tool falls
back to the AWS Price List API, and some rightsizing rules gate off as "unable
to verify".

```bash
export VANTAGE_API_TOKEN=...     # or
echo '...' > .vantage_token      # git-ignored, chmod 0600
```

**Never commit a token.** Use your own key, not a shared one. If a token is
ever pasted into chat, a ticket or a commit, rotate it — treat it as burned.

### Install

```bash
pip install -r requirements.txt
```

`pyarrow` is listed but optional — needed only to read Parquet-format CUR
exports. Without it, a Parquet CUR is reported as a gap rather than misread.

---

## 2. Running it

### Interactive — recommended

```bash
python3 run.py
```

Prompts for everything in three steps: AWS credentials (existing CLI profile,
access keys, or a role to assume), the path to your Google credentials file,
then regions and how many days of cost history to read.

Credentials are copied to a `0600` directory under the system temp dir — never
the project folder — and deleted on exit.

### Direct — for scripted runs

```bash
python3 main.py --config config.yaml --gcp-sa creds.json
python3 main.py --config config.yaml --profile prod --dry-run
```

| Flag | Purpose |
| --- | --- |
| `--config` | Path to `config.yaml` |
| `--gcp-sa` | Google service-account JSON |
| `--profile` | AWS CLI profile name |
| `--role-arn` | Role to assume, for cross-account access |
| `--dry-run` | Collect and analyse, skip the spreadsheet |

A run takes roughly 3–5 minutes and makes about ten Cost Explorer requests at
~$0.01 each — a few cents per report.

### What a healthy run looks like

```
Step 2/5 Fetching billing data from Cost Explorer...
  ✓ 82 monthly records, 1539 daily records
  OK No active commitments — on-demand list price is the correct baseline
Step 3/5 Collecting resource inventory (CE-driven)...
  ✓ 1004 resources found across 63 service types
Step 4/5 Fetching CloudWatch metrics...
  Confirmed savings: $1,466.06/mo
  Estimated savings: $943.44/mo
  Data gaps recorded: 17
```

Data gaps are **expected**. They are the tool telling you what it could not
measure — that list is a feature, and the Data Gaps tab explains every entry.

---

## 3. Reading the report

Roughly 34 tabs. The first seven are analysis; the rest are per-service
resource detail.

| Tab | Contents |
| --- | --- |
| **Summary** | Spend, Confirmed vs Estimated savings, and the cost basis stated up front |
| **Recommendations** | Every action with its saving, confidence, price basis and evidence |
| **Data Gaps** | Everything that could not be measured or priced, why, and how to fix it |
| **Data Transfer** | Inter-AZ and egress spend from actual billing |
| **Commitments** | Existing RIs and Savings Plans, plus AWS's own purchase recommendations |
| **All Services (Billing)** | Every service in Cost Explorer, flagging billing-only ones |
| **Changes** | Differential against the previous run |
| Per-service tabs | Resource-level detail, metrics and per-resource findings |

### Read the Summary line first

The header states the cost basis before any number. It says one of:

| Line | Means |
| --- | --- |
| `actual billed cost (CUR)` | Costs came from your real bill. Trust the figures. |
| `LIST PRICE (no CUR configured)` | Public prices. Wrong if the account holds discounts. |
| `LIST PRICE (CUR configured but no data yet)` | A CUR exists but hasn't delivered. Re-run later. |

### Safety checks that prevent bad advice

- **Peak-aware rightsizing** — judged on p95, not the average. An instance at
  8% average CPU that touches 95% is not downsized.
- **Headroom** — the observed peak must still fit the smaller instance on
  **both** vCPU and memory after the resize.
- **Performance Insights** — a database at 10% CPU and 80% IO-wait is not
  CPU-bound; downsizing is blocked.
- **Commitment awareness** — never recommends a reservation for capacity
  already covered.
- **Instance existence** — targets are validated against
  `ec2:DescribeInstanceTypeOfferings` and
  `rds:DescribeOrderableDBInstanceOptions`, so it cannot suggest a class AWS
  does not sell.

---

## 4. Confidence tiers

Every saving is labelled, and the tiers are **never summed into one headline**:

| Tier | Basis | How to use it |
| --- | --- | --- |
| **Confirmed** | Billing data — AWS's own Savings Plan / RI recommendations, or a CUR-backed baseline | Quote it directly |
| **Estimated** | Correct arithmetic on live *list* prices | Quote with the caveat; wrong if the account holds commitments |
| **Unpriced** | A real, actionable finding whose dollar impact cannot be established read-only | Raise as an action, never as a number |

A saving is only **Confirmed** when its baseline came from billing. List price
is materially wrong for an account holding Reserved Instances or Savings Plans,
so the tool detects existing commitments and downgrades confidence when they
exist.

> **Do not add the tiers together.**
> "Confirmed $1,466 + Estimated $943" is not a $2,409 promise — it is one
> number you can defend and one that depends on the account having no hidden
> discounts. Present them separately, exactly as the Summary does.

---

## 5. How savings are calculated

Savings are the difference between two **live prices**, never a percentage:

```
Rightsize  m5.xlarge → m5.large     $147.46/mo → $73.73/mo   = $73.73
Graviton   m5.large  → m7g.large    $ 73.73/mo → $42.56/mo   = $31.17
                                                      total     $104.90

m5.xlarge costs $147.46 · m7g.large costs $42.56 · true saving $104.90  ✓
```

Overlapping actions **compound rather than sum**. Rightsizing, Graviton and a
Reserved Instance all change what you pay for the same instance, so each is
priced from the previous step's end state. Storage reductions are billed
separately and are genuinely additive.

### What it checks

**33 rules** across EC2, RDS, ElastiCache, EBS, snapshots, ELB/ALB/NLB, NAT,
Elastic IPs, S3, EFS, ECR, KMS, Secrets Manager, WAF, Transfer Family, EKS,
Lambda and CloudWatch Logs — rightsizing, Graviton, commitments, idle
resources, orphaned snapshots, missing lifecycle policies, and version
end-of-life.

### Pricing sources

| Source | Provides |
| --- | --- |
| **CUR** (if configured) | Actual billed cost, effective discount, commitment coverage |
| **AWS Price List** | Every service rate, per region — Query API, falling back to the public bulk list which needs no credentials |
| **Vantage Instances API** | Instance on-demand **and real Reserved Instance** rates |
| **Cost Explorer** | Spend history, data transfer, AWS's own purchase recommendations |

---

## 6. Enabling a CUR

A Cost and Usage Report is AWS's most detailed billing export — one row per
resource, per usage type, per day, written to an S3 bucket you own. It carries
both what you were charged and the public on-demand cost for the same usage, so
the effective discount is *measured* rather than assumed.

### What it actually buys you

Measured on a real account holding **zero** commitments — where list price
should in theory already be correct:

| Service | List price | AWS actual | Error |
| --- | ---: | ---: | ---: |
| EC2 | 2,216.29 | 2,013.46 | +10% |
| RDS | 1,157.05 | 1,379.64 | −16% |
| ELB | 117.02 | 101.65 | +15% |
| EKS | 146.00 | 144.00 | +1% |

The errors run in **both directions**, so they do not cancel — they distort
which service looks worth optimising. Separately, on that account only 77% of
spend could be attributed to a specific resource; the remaining 23% belongs to
usage-priced services (Lambda, SQS, SNS, CloudFront) where no per-resource cost
can be derived from inventory at all. A CUR closes that outright.

### Creating one

This is the one part of the workflow that **writes** to AWS, so it is
deliberately not something the tool does. Run it yourself, in the account you
want billed data for.

```bash
# 1. a dedicated bucket, locked down
aws s3api create-bucket --bucket aws-cur-<ACCOUNT> --region <REGION> \
  --create-bucket-configuration LocationConstraint=<REGION>

aws s3api put-public-access-block --bucket aws-cur-<ACCOUNT> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 2. bucket policy allowing billingreports.amazonaws.com to write,
#    scoped with aws:SourceAccount and aws:SourceArn to YOUR account

# 3. the report itself
aws cur put-report-definition --region us-east-1 --report-definition '{
  "ReportName": "cost-optimisation",
  "TimeUnit": "DAILY",
  "Format": "textORcsv",
  "Compression": "GZIP",
  "AdditionalSchemaElements": ["RESOURCES"],
  "S3Bucket": "aws-cur-<ACCOUNT>",
  "S3Prefix": "cur",
  "S3Region": "<REGION>",
  "RefreshClosedReports": true,
  "ReportVersioning": "OVERWRITE_REPORT"
}'
```

Three settings matter for how the tool reads it:

- **`DAILY`, not `HOURLY`** — 24× smaller. The tool aggregates to monthly
  totals anyway.
- **`OVERWRITE_REPORT`** — one folder per billing period that AWS restates in
  place. `CREATE_NEW_REPORT` piles up versioned assembly folders and the
  "latest period" logic has to guess.
- **`RESOURCES`** — without it there are no per-resource rows, and the whole
  point is lost.

### Then wait

CUR is a batch export, not an API. AWS generates files on its own schedule, and
the first delivery takes up to 24 hours. You do not configure anything in the
tool — it discovers the report itself on the next run.

> **Check AWS's own status, don't guess.**
> An empty bucket looks identical whether the report has never delivered or
> your permissions are broken. `describe-report-definitions` returns
> `ReportStatus.lastDelivery` and `lastStatus` — the tool reads both and says
> which case you are in on the Data Gaps tab.

> **If you run this in a management account**, the export covers *every account
> in the organisation*, not just the one you created it in. The tool keys costs
> by resource ID so linked accounts will not contaminate a single-account
> report, but the bucket will hold the whole org's billing detail. Know that
> before granting access to it.

---

## 7. Troubleshooting

| What you see | What it means |
| --- | --- |
| `Cost basis: LIST PRICE (CUR configured but no data yet)` | Working as intended. The CUR has not delivered its first file. Re-run after 24h. |
| `CUR incomplete: … exceeds the 200-file cap` | The read failed **closed** on purpose. Partial billing data would be applied as actual cost and shown as Confirmed — wrong numbers with the highest confidence badge. Switch the export to DAILY, or raise the caps in `collectors/cur_reader.py`. |
| `Credentials expired or were rejected` | Commitment savings were **not assessed**. The $0 means "not checked", not "nothing available". Run `aws sso login` and re-run. |
| `Resource-level cost attribution not available` | Resource-level cost allocation is not enabled on the account. Costs fall back to list price. |
| `<service> page skipped: quota` | Google Sheets allows 60 writes per minute. Analysis tabs are written first so they always survive. Re-run to get the missing service tab. |
| `SSM agent present but no application inventory` | Graviton compatibility is reported as "check required", never as "safe". Enable SSM Inventory to get an automated scan. |

When something genuinely cannot be measured, it appears on the **Data Gaps**
tab with a reason, a fix, and its impact. Gaps deduplicate, so five hundred
identical denials produce one row.

---

## 8. Sharing the tool

**Do not zip the project folder.** Secrets, client snapshots and generated
reports live alongside the source, and a plain copy ships all of them.

```bash
./make_share_bundle.sh ~/Desktop/cost-optimisation-share
```

It copies source only, then greps the *result* for account IDs, AKIA keys and
UUID-shaped tokens and refuses to ship on a hit — verifying rather than
trusting the exclude list.

Tell whoever receives it to set their own Vantage token and run the suite
first. Pricing-dependent checks skip rather than fail when no backend is
configured, so a clean machine still reports `ALL SUITES PASSED`.

---

## 9. Tests

```bash
./run_tests.sh
```

| Suite | Checks | Covers |
| --- | ---: | --- |
| `tests/audit.py` | 13 | Structural — imports, column parity, rule requirements, tab-name collisions |
| `tests/test_no_fabrication.py` | 23 | No hardcoded price or fallback percentage can reappear |
| `tests/test_account_shapes.py` | 42 | Account shapes the dev account cannot reach |
| **Total** | **78** | |

`test_account_shapes.py` simulates what a single development account never
exercises: an account with a CUR, one already holding commitments, partial
permissions, expired credentials, an empty account, a Parquet CUR, and a CUR
that is configured but has not delivered.

Every check exists because that bug shipped at least once — metric key
mismatches, column drift, dicts reaching the spreadsheet, savings counted
twice, undefined names hidden by broad `except` blocks. **Run the suite after
any change to savings logic.**

---

## 10. Configuration

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

Thresholds are **policy, not facts** — they are yours to set. Prices are never
configured, only fetched.

---

## 11. Known limits

Stated plainly, because the tool states them in its own output too:

- **Without a CUR**, savings are quoted against list price and labelled
  Estimated. Existing commitments are detected so the distortion is at least
  known.
- **Per-request services** (Lambda, SQS, SNS, API Gateway, CloudFront,
  CodeBuild) cannot be given a per-resource monthly cost from inventory alone.
- **Snapshot cost is an upper bound** — AWS exposes no per-snapshot incremental
  size.
- **Memory is invisible without the CloudWatch Agent**, which caps rightsizing
  confidence. The report names the affected instances and their combined spend.
- **ElastiCache and Lambda runtime EOL** have no AWS API and remain dated
  reference data — labelled as such and never priced.
- **Gateway Load Balancer and Route 53** have no rules. Neither has a savings
  lever that read-only data can confirm.

---

## Security

- Read-only against AWS. Every call is a `describe`/`list`/`get`.
- The CUR reader deliberately reads Parquet/CSV straight from S3 rather than
  using Athena, which would write query results.
- `run.py` copies credentials to a `0600` directory under the system temp dir,
  never the project folder, and deletes them on exit.
- `.gitignore` covers `*credentials*.json`, `*client_secret*.json`,
  `.vantage_token`, `tmp*.json`, `snapshots/` and generated `*.xlsx` reports.

---

## License

Proprietary — © CloudZenia. Internal use only.
