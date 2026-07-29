"""
Dynamic AWS price resolution.

Nothing in this project should quote a dollar figure it did not fetch. Every
price below is resolved at runtime from AWS's own published price list, for the
caller's actual region. If a price cannot be resolved the accessor returns
None — callers must then record "unavailable" and leave the cell empty rather
than substituting a guess.

Two backends, tried in order:

  1. Price List Query API — session.client("pricing").get_products(...).
     Server-side filtered, so responses are small. Needs pricing:GetProducts,
     which AWS's ReadOnlyAccess policy grants. Required for AmazonEC2 SKUs
     (EBS volumes, NAT Gateway) because the EC2 bulk file is ~480 MB.

  2. Public bulk price list — pricing.us-east-1.amazonaws.com region-scoped
     JSON. No credentials at all. Region files for non-EC2 services are
     10 KB-2 MB, so this is a genuine fallback when the IAM permission is
     missing, not a token gesture.

Resolved prices are cached in memory for the run and on disk (default 24h) so a
report does not re-download the same offer file repeatedly.
"""

import json
import os
import time
import urllib.request
from pathlib import Path

BULK_HOST = "https://pricing.us-east-1.amazonaws.com"
BULK_INDEX = "/offers/v1.0/aws/index.json"
TIMEOUT_S = 45
CACHE_TTL_S = 24 * 3600
MAX_BULK_BYTES = 64 * 1024 * 1024   # AmazonEC2 is ~480 MB — never bulk-fetch it

_session = None
_pricing_client = None
_api_disabled = False
_cache_dir = Path(os.environ.get("COST_REPORT_CACHE",
                                 Path.home() / ".cache" / "aws-cost-report" / "pricing"))
_memo = {}
_bulk_memo = {}
_region_index_memo = {}
_unresolved = set()


class Price:
    """A resolved price, carrying where it came from so the report can say so."""

    __slots__ = ("amount", "unit", "source", "sku_description")

    def __init__(self, amount, unit, source, sku_description=""):
        self.amount = float(amount)
        self.unit = unit
        self.source = source
        self.sku_description = sku_description

    def __float__(self):
        return self.amount

    def __repr__(self):
        return f"Price({self.amount} /{self.unit} via {self.source})"

    def monthly(self, hours=730):
        """Convert an hourly rate to a monthly one."""
        return round(self.amount * hours, 4)


def configure(session=None, cache_dir=None):
    global _session, _pricing_client, _api_disabled, _cache_dir
    _session = session
    _pricing_client = None
    _api_disabled = False
    if cache_dir:
        _cache_dir = Path(cache_dir)
    try:
        _cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def unresolved_prices():
    """Everything we failed to price this run — surfaced in the report."""
    return sorted(_unresolved)


# ─── Region → Price List "location" name, resolved from botocore's own data ───

_location_memo = {}


def region_location(region):
    """
    "us-east-1" → "US East (N. Virginia)".

    Read from botocore's bundled endpoints data rather than a hand-maintained
    map, so new regions work without a code change.
    """
    if region in _location_memo:
        return _location_memo[region]

    location = None
    try:
        import botocore.session
        loader = botocore.session.get_session().get_component("data_loader")
        endpoints = loader.load_data("endpoints")
        for partition in endpoints.get("partitions", []):
            info = partition.get("regions", {}).get(region)
            if info and info.get("description"):
                location = info["description"]
                break
    except Exception:
        pass

    # botocore spells it "Europe (Ireland)"; the price list agrees on modern
    # versions but older data used "EU (Ireland)".
    _location_memo[region] = location
    return location


def _location_variants(region):
    base = region_location(region)
    if not base:
        return []
    variants = [base]
    if base.startswith("Europe ("):
        variants.append(base.replace("Europe (", "EU (", 1))
    elif base.startswith("EU ("):
        variants.append(base.replace("EU (", "Europe (", 1))
    return variants


# ─── Disk cache ───────────────────────────────────────────────────────────────

def _cache_path(key):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return _cache_dir / f"{safe}.json"


def _cache_read(key, ttl=CACHE_TTL_S):
    path = _cache_path(key)
    try:
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > ttl:
            return None
        with path.open() as f:
            return json.load(f)
    except Exception:
        return None


def _cache_write(key, data):
    try:
        _cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(key).with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(data, f)
        tmp.replace(_cache_path(key))
    except Exception:
        pass


# ─── Backend 1: Price List Query API ─────────────────────────────────────────

def _api_client():
    global _pricing_client, _api_disabled
    if _api_disabled or _session is None:
        return None
    if _pricing_client is None:
        try:
            _pricing_client = _session.client("pricing", region_name="us-east-1")
        except Exception:
            _api_disabled = True
            return None
    return _pricing_client


def _api_products(service_code, filters):
    """
    Query API products, normalised to the same shape as the bulk offer files.

    The two sources disagree on structure and this bit them badly: the bulk file
    has {sku, productFamily, attributes} at the top level, while the Query API
    nests all of that under a "product" key. Reading the bulk shape for both
    meant the API path matched nothing at all — every EBS and NAT lookup
    reported "unavailable" even with full admin credentials.
    """
    client = _api_client()
    if client is None:
        return []
    try:
        resp = client.get_products(
            ServiceCode=service_code,
            Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters],
            MaxResults=100,
        )
    except Exception as e:
        if "AccessDenied" in str(e) or "not authorized" in str(e):
            globals()["_api_disabled"] = True
        return []

    normalised = []
    for raw in resp.get("PriceList", []):
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        product = entry.get("product") or {}
        normalised.append({
            "sku": product.get("sku", ""),
            "productFamily": product.get("productFamily"),
            "attributes": product.get("attributes", {}) or {},
            "terms": entry.get("terms", {}) or {},
        })
    return normalised


# ─── Backend 2: public bulk price list ───────────────────────────────────────

def _http_json(url, cache_key, ttl=CACHE_TTL_S):
    cached = _cache_read(cache_key, ttl)
    if cached is not None:
        return cached
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "cloudzenia-cost-report/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            length = resp.headers.get("Content-Length")
            if length and int(length) > MAX_BULK_BYTES:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    _cache_write(cache_key, data)
    return data


def _bulk_region_offer(service_code, region):
    """Region-scoped offer file for a service, or None."""
    if service_code == "AmazonEC2":
        return None          # 480 MB — API only
    key = (service_code, region)
    if key in _bulk_memo:
        return _bulk_memo[key]

    index = _region_index_memo.get(service_code)
    if index is None:
        index = _http_json(
            f"{BULK_HOST}/offers/v1.0/aws/{service_code}/current/region_index.json",
            f"regionindex-{service_code}") or {}
        _region_index_memo[service_code] = index

    entry = (index.get("regions") or {}).get(region)
    if not entry or not entry.get("currentVersionUrl"):
        _bulk_memo[key] = None
        return None

    offer = _http_json(BULK_HOST + entry["currentVersionUrl"],
                       f"offer-{service_code}-{region}")
    _bulk_memo[key] = offer
    return offer


# ─── Matching ────────────────────────────────────────────────────────────────

def _strip_region_prefix(usagetype):
    """
    Kept for tests/back-compat. Prefer _strip_known_prefix, which only removes
    a prefix that was actually observed to be this region's.
    """
    if "-" in usagetype:
        head, rest = usagetype.split("-", 1)
        if head and head.isupper() and head.isalnum() and len(head) <= 6:
            return rest
    return usagetype


def _detect_region_prefixes(usagetypes):
    """
    Work out this offer file's region prefix from the data rather than guessing.

    Region prefixes vary by region and are not derivable from the region code:
    us-east-1 usually omits one entirely, eu-west-1 uses 'EU', ap-south-1 uses
    'APS1'. Guessing "any short upper-case token" is unsafe — 'TS' in
    'TS-LoadBalancerUsage' is a product variant priced at $0.005/hr, and
    stripping it made an ALB resolve to a fifth of its real hourly rate.

    A genuine region prefix appears on a large share of a regional file's
    usagetypes; a product token appears on a handful. Requiring 30% coverage
    separates them cleanly.
    """
    from collections import Counter
    total = len(usagetypes)
    if not total:
        return frozenset()
    counts = Counter()
    for ut in usagetypes:
        if "-" not in ut:
            continue
        head = ut.split("-", 1)[0]
        if head and head.isupper() and head.isalnum() and len(head) <= 6:
            counts[head] += 1
    return frozenset(p for p, c in counts.items() if c / total >= 0.30)


def _strip_known_prefix(usagetype, prefixes):
    for prefix in prefixes:
        if usagetype.startswith(prefix + "-"):
            return usagetype[len(prefix) + 1:]
    return usagetype


def _matches(attrs, usagetype_suffix, product_family, attributes, product_family_field,
             prefixes=frozenset()):
    if product_family and attrs.get(product_family_field, "") != product_family:
        return False
    if usagetype_suffix:
        ut = _strip_known_prefix(attrs.get("usagetype", ""), prefixes)
        if ut != usagetype_suffix:
            return False
    for key, want in (attributes or {}).items():
        got = attrs.get(key)
        if got is None:
            return False
        if isinstance(want, (list, tuple, set)):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


def _ondemand_dimensions(product, terms_ondemand):
    """
    On-demand price dimensions for a product, across both source shapes.

    The bulk offer file keys terms.OnDemand by the bare SKU. The Query API keys
    it by "<SKU>.<offerTermCode>" — so a plain terms[sku] lookup silently
    returned nothing for every API-sourced product, which is why EBS and NAT
    reported "unavailable" even with administrator credentials.
    """
    sku = product.get("sku")
    if not sku:
        return

    term = terms_ondemand.get(sku)
    if term:
        for offer in term.values():
            for dim in (offer.get("priceDimensions") or {}).values():
                yield dim
        return

    prefix = sku + "."
    for key, offer in terms_ondemand.items():
        if key.startswith(prefix):
            for dim in (offer.get("priceDimensions") or {}).values():
                yield dim


def _extract(products_and_terms, usagetype_suffix, product_family, attributes, pick):
    """Yield (amount, unit, description) for every matching on-demand dimension."""
    region_prefixes = _detect_region_prefixes(
        [p.get("attributes", {}).get("usagetype", "") for p, _ in products_and_terms])

    def rank(usagetype):
        """
        How well a usagetype matches the wanted SKU. Lower is better; None
        means no match.

          0  exact — no prefix at all              'LoadBalancerUsage'
          1  this region's detected prefix         'EU-TimedStorage-ByteHrs'
          2  some other upper-case leading token   'USE1-AmazonEKS-Hours:...'

        Ranking rather than blind stripping is what stops 'TS-LoadBalancerUsage'
        ($0.005/hr, a product variant) from being taken for the real ALB rate
        when the unprefixed 'LoadBalancerUsage' ($0.0225/hr) is present.
        """
        if not usagetype_suffix:
            return 0
        if usagetype == usagetype_suffix:
            return 0
        for prefix in region_prefixes:
            if usagetype == f"{prefix}-{usagetype_suffix}":
                return 1
        if "-" in usagetype:
            head, rest = usagetype.split("-", 1)
            if (rest == usagetype_suffix and head and head.isupper()
                    and head.isalnum() and len(head) <= 6):
                return 2
        return None

    by_rank = {}
    for product, dims in products_and_terms:
        attrs = product.get("attributes", {})
        # productFamily sits at the top level in bulk files and inside
        # attributes in some Query API responses — accept either.
        if product_family and product_family not in (product.get("productFamily"),
                                                     attrs.get("productFamily")):
            continue
        if not _matches(attrs, None, None, attributes, "productFamily"):
            continue
        r = rank(attrs.get("usagetype", ""))
        if r is None:
            continue
        for dim in dims:
            amount = dim.get("pricePerUnit", {}).get("USD")
            try:
                value = float(amount)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            # Skip tiered rows that only apply beyond a volume threshold
            if float(dim.get("beginRange", 0) or 0) > 0 and pick == "first_tier":
                continue
            by_rank.setdefault(r, []).append(
                (value, dim.get("unit", ""), dim.get("description", "")))

    if not by_rank:
        return None
    hits = by_rank[min(by_rank)]
    if pick == "max":
        return max(hits, key=lambda h: h[0])
    return min(hits, key=lambda h: h[0])


def price(service_code, region, usagetype_suffix=None, product_family=None,
          attributes=None, pick="first_tier", label=None, global_service=False):
    """
    Resolve a single unit price. Returns a Price, or None if it cannot be found.

    pick: "first_tier" (default) takes the lowest-priced non-tiered row, which
    is the entry rate; "max" takes the highest.
    """
    def _hashable(v):
        return frozenset(v) if isinstance(v, (list, tuple, set)) else v

    memo_key = (service_code, region, usagetype_suffix, product_family,
                tuple(sorted((k, _hashable(v)) for k, v in (attributes or {}).items())),
                pick, global_service)
    if memo_key in _memo:
        return _memo[memo_key]

    result = None

    # Backend 1 — Query API.
    # Globally-billed SKUs (Route 53 hosted zones, for one) carry an EMPTY
    # regionCode, so filtering on region excludes them entirely — which is why
    # the hosted-zone price could never be resolved.
    filters = [] if global_service else [("regionCode", region)]
    if product_family:
        filters.append(("productFamily", product_family))
    for key, want in (attributes or {}).items():
        if isinstance(want, str):
            filters.append((key, want))
    products = _api_products(service_code, filters)
    if products:
        pairs = [(p, list(_ondemand_dimensions(p, p.get("terms", {}).get("OnDemand", {}))))
                 for p in products]
        found = _extract(pairs, usagetype_suffix, product_family, attributes, pick)
        if found:
            result = Price(found[0], found[1], "aws-price-list-api", found[2])

    # Backend 2 — public bulk offer file
    if result is None:
        offer = _bulk_region_offer(service_code, region)
        if offer:
            ondemand = offer.get("terms", {}).get("OnDemand", {})
            pairs = []
            for sku, product in offer.get("products", {}).items():
                product = dict(product, sku=sku)
                pairs.append((product, list(_ondemand_dimensions(product, ondemand))))
            found = _extract(pairs, usagetype_suffix, product_family, attributes, pick)
            if found:
                result = Price(found[0], found[1], "aws-bulk-price-list", found[2])

    if result is None:
        _unresolved.add(label or f"{service_code}:{usagetype_suffix or product_family}:{region}")

    _memo[memo_key] = result
    return result


def amount(p):
    """Price → float, or None. Keeps call sites from hallucinating a default."""
    return p.amount if p is not None else None


# ─── Typed accessors ─────────────────────────────────────────────────────────
# Every one returns a Price or None. None means "we could not price this" and
# must never be silently turned into a number.

def nat_gateway_hourly(region):
    # productFamily is required: without it the Query API returns the first 100
    # EC2 products for the region, none of which are the NAT Gateway SKU.
    return price("AmazonEC2", region, usagetype_suffix="NatGateway-Hours",
                 product_family="NAT Gateway",
                 label=f"NAT Gateway hourly ({region})")


def nat_gateway_per_gb(region):
    return price("AmazonEC2", region, usagetype_suffix="NatGateway-Bytes",
                 product_family="NAT Gateway",
                 label=f"NAT Gateway data processing ({region})")


def eip_idle_hourly(region):
    return price("AmazonVPC", region, usagetype_suffix="PublicIPv4:IdleAddress",
                 label=f"Idle Elastic IP hourly ({region})")


def eip_in_use_hourly(region):
    return price("AmazonVPC", region, usagetype_suffix="PublicIPv4:InUseAddress",
                 label=f"In-use public IPv4 hourly ({region})")


def ebs_gb_month(region, volume_type):
    return price("AmazonEC2", region, product_family="Storage",
                 attributes={"volumeApiName": volume_type},
                 label=f"EBS {volume_type} GB-month ({region})")


def ebs_iops_month(region, volume_type):
    return price("AmazonEC2", region, product_family="System Operation",
                 attributes={"volumeApiName": volume_type, "group": "EBS IOPS"},
                 label=f"EBS {volume_type} IOPS-month ({region})")


def ebs_snapshot_gb_month(region, archive=False):
    """
    EBS snapshot storage, per GB-month. Standard snapshots and the cheaper
    archive tier are separate SKUs.
    """
    suffix = "EBS:SnapshotArchiveStorage" if archive else "EBS:SnapshotUsage"
    return price("AmazonEC2", region, usagetype_suffix=suffix,
                 product_family="Storage Snapshot",
                 label=f"EBS snapshot{' archive' if archive else ''} GB-month ({region})")


def ebs_throughput_month(region, volume_type="gp3"):
    """
    Price per provisioned MiB/s-month.

    AWS publishes this SKU per *GiBps*-month ($40.96), while its own description
    reads "$0.04 per provisioned MiBps-month" and every console/API figure for
    volume throughput is in MiB/s. Returning the raw number would overstate the
    throughput component of EBS cost by 1024x, so the unit is normalised here.
    """
    found = price("AmazonEC2", region, product_family="Provisioned Throughput",
                  attributes={"volumeApiName": volume_type},
                  label=f"EBS {volume_type} throughput ({region})")
    if found and "gibps" in (found.unit or "").lower():
        return Price(found.amount / 1024, "MiBps-mo", found.source,
                     found.sku_description)
    return found


_LB_FAMILY = {
    "application": "Load Balancer-Application",
    "network":     "Load Balancer-Network",
    "gateway":     "Load Balancer-Gateway",
    "classic":     "Load Balancer",
}


def load_balancer_hourly(region, lb_type="application"):
    family = _LB_FAMILY.get(lb_type, "Load Balancer-Application")
    return price("AWSELB", region, usagetype_suffix="LoadBalancerUsage",
                 product_family=family,
                 label=f"{lb_type} load balancer hourly ({region})")


def load_balancer_lcu_hourly(region, lb_type="application"):
    family = _LB_FAMILY.get(lb_type, "Load Balancer-Application")
    return price("AWSELB", region, usagetype_suffix="LCUUsage",
                 product_family=family,
                 label=f"{lb_type} LCU hourly ({region})")


# S3 identifies each storage tier with its own usagetype, not an attribute.
# Note "TimedStorage-ByteHrs" is Standard only — Files/Tables/Annotation share
# the suffix but keep their own leading token, which _strip_region_prefix
# deliberately preserves.
_S3_TIER_USAGETYPE = {
    "standard":     ("TimedStorage-ByteHrs",),
    "ia":           ("TimedStorage-SIA-ByteHrs",),
    "onezone_ia":   ("TimedStorage-ZIA-ByteHrs",),
    "intelligent":  ("TimedStorage-INT-FA-ByteHrs",),
    "glacier_ir":   ("TimedStorage-GIR-ByteHrs",),
    "glacier":      ("TimedStorage-GlacierByteHrs",),
    # NOTE: deliberately NOT falling back to TimedStorage-GDA-Staging — that is
    # the staging-object SKU ($0.021/GB), ~21x the real Deep Archive rate.
    # Returning None here is correct; a wrong number is worse than no number.
    "deep_archive": ("TimedStorage-GDA-ByteHrs",),
    "express":      ("TimedStorage-XZ-ByteHrs",),
    "rrs":          ("TimedStorage-RRS-ByteHrs",),
}


def s3_storage_gb_month(region, tier="standard"):
    for suffix in _S3_TIER_USAGETYPE.get(tier, _S3_TIER_USAGETYPE["standard"]):
        found = price("AmazonS3", region, usagetype_suffix=suffix,
                      product_family="Storage",
                      label=f"S3 {tier} GB-month ({region})")
        if found:
            return found
    return None


def cw_logs_storage_gb_month(region):
    return price("AmazonCloudWatch", region, usagetype_suffix="TimedStorage-ByteHrs",
                 label=f"CloudWatch Logs storage GB-month ({region})")


def cw_logs_ingest_gb(region):
    return price("AmazonCloudWatch", region, usagetype_suffix="DataProcessing-Bytes",
                 label=f"CloudWatch Logs ingestion GB ({region})")


def kms_key_month(region):
    return price("awskms", region, product_family="Encryption Key",
                 label=f"KMS customer key month ({region})")


def secret_month(region):
    return price("AWSSecretsManager", region, product_family="Secret",
                 label=f"Secrets Manager secret month ({region})")


def ecr_gb_month(region):
    return price("AmazonECR", region, usagetype_suffix="TimedStorage-ByteHrs",
                 label=f"ECR storage GB-month ({region})")


def efs_gb_month(region, infrequent_access=False):
    suffix = "IATimedStorage-ByteHrs" if infrequent_access else "TimedStorage-ByteHrs"
    return price("AmazonEFS", region, usagetype_suffix=suffix,
                 product_family="Storage",
                 label=f"EFS {'IA' if infrequent_access else 'standard'} GB-month ({region})")


def transfer_protocol_hourly(region, protocol="SFTP:S3"):
    return price("AWSTransfer", region, usagetype_suffix="ProtocolHours",
                 attributes={"operation": protocol},
                 label=f"Transfer Family {protocol} hourly ({region})")


def transfer_per_gb(region, direction="Download", protocol="SFTP:S3"):
    return price("AWSTransfer", region, usagetype_suffix=f"{direction}Bytes",
                 attributes={"operation": protocol},
                 label=f"Transfer Family {direction} GB ({region})")


def eks_cluster_hourly(region):
    return price("AmazonEKS", region, usagetype_suffix="AmazonEKS-Hours:perCluster",
                 label=f"EKS control plane hourly ({region})")


def eks_extended_support_hourly(region):
    return price("AmazonEKS", region,
                 usagetype_suffix="AmazonEKS-Hours:extendedSupport",
                 label=f"EKS extended support hourly ({region})")


def waf_web_acl_month(region):
    for suffix in ("WebACLV2", "WebACL"):
        found = price("awswaf", region, usagetype_suffix=suffix,
                      label=f"WAF Web ACL month ({region})")
        if found:
            return found
    return None


def waf_rule_month(region):
    for suffix in ("RuleV2", "Rule"):
        found = price("awswaf", region, usagetype_suffix=suffix,
                      label=f"WAF rule month ({region})")
        if found:
            return found
    return None


def rds_storage_gb_month(region, storage_type="gp3", multi_az=False):
    suffix_map = {
        "gp2": "RDS:Multi-AZ-GP2-Storage" if multi_az else "RDS:GP2-Storage",
        "gp3": "RDS:Multi-AZ-GP3-Storage" if multi_az else "RDS:GP3-Storage",
        "io1": "RDS:Multi-AZ-PIOPS-Storage" if multi_az else "RDS:PIOPS-Storage",
        "standard": "RDS:StorageUsage",
    }
    suffix = suffix_map.get(storage_type, suffix_map["gp3"])
    return price("AmazonRDS", region, usagetype_suffix=suffix,
                 product_family="Database Storage",
                 label=f"RDS {storage_type} storage GB-month ({region})")


# RDS extended support is billed per vCPU-hour, under a usagetype of the form
# "ExtendedSupport:Yr1-Yr2:<Engine><MajorVersion>" (Yr3 is roughly double).
# There is no productFamily on these SKUs, so they are matched by usagetype.
_RDS_ES_ENGINE = {
    "mysql":             "MySQL",
    "postgres":          "PostgreSQL",
    "aurora-mysql":      "AuroraMySQL",
    "aurora-postgresql": "AuroraPostgreSQL",
}


def rds_extended_support_vcpu_hour(region, engine, version, year=1):
    """
    Per-vCPU-hour extended support rate, or None.

    Returning None matters here: RDS extended support is a real, ongoing charge
    that starts automatically at end of standard support, and quoting a made-up
    rate for it would be worse than admitting we could not price it.
    """
    token = _RDS_ES_ENGINE.get((engine or "").lower())
    if not token or not version:
        return None

    parts = str(version).split(".")
    # AWS names these by major version for Postgres/Aurora (PostgreSQL12) and
    # major.minor for MySQL (MySQL8.0), so try both.
    candidates = []
    if parts:
        candidates.append(f"{token}{parts[0]}")
    if len(parts) >= 2:
        candidates.insert(0, f"{token}{parts[0]}.{parts[1]}")

    term = "Yr1-Yr2" if year < 3 else "Yr3"
    for candidate in candidates:
        found = price("AmazonRDS", region,
                      usagetype_suffix=f"ExtendedSupport:{term}:{candidate}",
                      label=f"RDS extended support {candidate} ({region})")
        if found:
            return found
    return None


def route53_hosted_zone_month():
    """
    Per-hosted-zone monthly charge. Billed globally, so no region filter.
    Tiered ($0.50 for the first 25 zones, $0.10 beyond); pick="first_tier"
    takes the entry rate, which is correct for all but very large estates.
    """
    return price("AmazonRoute53", "us-east-1", usagetype_suffix="HostedZone",
                 product_family="DNS Zone", global_service=True,
                 label="Route 53 hosted zone month")


def sqs_requests_per_million(region):
    return price("AWSQueueService", region, product_family="API Request",
                 label=f"SQS requests ({region})")
