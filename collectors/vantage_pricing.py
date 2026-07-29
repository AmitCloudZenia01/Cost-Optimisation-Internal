"""
Vantage Instances API pricing provider.

Why this exists: the AWS Price List API returns on-demand rates only, and it
needs an extra IAM permission that read-only report users often lack. The
Vantage Instances API returns, per instance type and region:

  - on-demand hourly rate
  - REAL reserved-instance rates for every term/payment option
  - spot averages
  - the supported CPU architectures (used to verify Graviton eligibility)

That lets the recommender price a rightsize/Graviton *target* and quote actual
RI discounts instead of the hardcoded 45% / 20% / 35% guesses it used before.

Auth — the token is never stored in this repo. Resolution order:
  1. VANTAGE_API_TOKEN environment variable
  2. `pricing.vantage_token` in config.yaml
  3. a local `.vantage_token` file (git-ignored)

If no token is present, or any call fails, every function returns None and
collectors/pricing.py falls back to the AWS Price List API.

Docs: https://instances-api.vantage.sh   (free; key issued against an email)
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

BASE_URL = "https://instances-api.vantage.sh"
TIMEOUT_S = 20

_token = None
_token_checked = False
_disabled = False   # set after a hard auth failure so we stop retrying

# Vantage encodes the RDS engine as a numeric platform key.
RDS_ENGINE_CODES = {
    "mysql":             "2",
    "oracle-ee":         "5",
    "oracle-se2":        "5",
    "sqlserver-ex":      "10",
    "sqlserver-web":     "11",
    "sqlserver-se":      "12",
    "postgres":          "14",
    "sqlserver-ee":      "15",
    "mariadb":           "18",
    "aurora-mysql":      "21",
    "aurora-postgresql": "21",
}

# EC2 platform keys in the Vantage pricing map.
EC2_PLATFORMS = {
    "linux":   "linux",
    "windows": "mswin",
    "rhel":    "rhel",
    "suse":    "sles",
    "ubuntu":  "ubuntu",
}

# Reserved-term keys, best discount first — we take the first one present.
RI_TERM_PREFERENCE = [
    "yrTerm1Standard.noUpfront",
    "yrTerm1Standard.partialUpfront",
    "yrTerm1Standard.allUpfront",
    "yrTerm1Convertible.noUpfront",
]


def set_token(token):
    """Explicitly set the API token (used when it comes from config.yaml)."""
    global _token, _token_checked, _disabled
    if token:
        _token = str(token).strip()
        _token_checked = True
        _disabled = False


def configured():
    """
    True when a token is available from any source.

    Tests use this to skip assertions that need a live pricing backend rather
    than reporting a missing token as a product failure.
    """
    return bool(_resolve_token())


def _resolve_token():
    global _token, _token_checked
    if _token_checked:
        return _token
    _token_checked = True

    env = os.environ.get("VANTAGE_API_TOKEN", "").strip()
    if env:
        _token = env
        return _token

    for candidate in (Path.cwd() / ".vantage_token",
                      Path(__file__).resolve().parent.parent / ".vantage_token"):
        try:
            if candidate.is_file():
                _token = candidate.read_text().strip()
                if _token:
                    return _token
        except Exception:
            pass

    _token = None
    return None


def is_available():
    return bool(_resolve_token()) and not _disabled


def _get(path, authenticated=True):
    global _disabled
    if _disabled:
        return None

    token = _resolve_token()
    if authenticated and not token:
        return None

    headers = {
        "Accept": "application/json",
        "User-Agent": "cloudzenia-cost-report/1.0",
    }
    if authenticated and token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(BASE_URL + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401/403 means the token is bad — stop hammering the API for the rest
        # of the run and let the AWS Price List fallback take over.
        if e.code in (401, 403):
            _disabled = True
        return None
    except Exception:
        return None


def _as_float(value):
    if value is None:
        return None
    try:
        f = float(value)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=512)
def _instance(service, instance_type):
    """Raw instance document, cached per (service, type) for the run."""
    if not instance_type:
        return None
    encoded = urllib.parse.quote(instance_type, safe="")
    return _get(f"/api/v1/instances/{service}/{encoded}/global")


@lru_cache(maxsize=128)
def family_instance_types(family, service="ec2"):
    """
    Every instance type in a family, e.g. "m7g" → [m7g.medium, m7g.large, ...].
    Unauthenticated endpoint. Used to confirm a rightsize/Graviton target
    actually exists before recommending it.
    """
    if not family:
        return ()
    encoded = urllib.parse.quote(family, safe="")
    data = _get(f"/api/v1/instances/{service}/families/{encoded}/global",
                authenticated=False)
    if not isinstance(data, list):
        return ()
    return tuple(i.get("instance_type", "") for i in data if i.get("instance_type"))


def _platform_key(platform):
    p = (platform or "Linux").lower()
    if "windows" in p:
        return "mswin"
    if "rhel" in p or "red hat" in p:
        return "rhel"
    if "suse" in p or "sles" in p:
        return "sles"
    if "ubuntu" in p:
        return "ubuntu"
    return "linux"


def _region_pricing(doc, region):
    if not doc:
        return None
    return (doc.get("pricing") or {}).get(region)


# ─── EC2 ──────────────────────────────────────────────────────────────────────

def ec2_hourly(instance_type, region, platform="Linux"):
    """On-demand hourly USD, or None."""
    prices = _region_pricing(_instance("ec2", instance_type), region)
    if not prices:
        return None
    entry = prices.get(_platform_key(platform)) or prices.get("linux") or {}
    return _as_float(entry.get("ondemand"))


def ec2_reserved_hourly(instance_type, region, platform="Linux"):
    """
    Best 1-year Standard reserved hourly USD, or None.
    Real rate — replaces the old flat 35% assumption.
    """
    prices = _region_pricing(_instance("ec2", instance_type), region)
    if not prices:
        return None
    entry = prices.get(_platform_key(platform)) or prices.get("linux") or {}
    reserved = entry.get("reserved") or {}
    for term in RI_TERM_PREFERENCE:
        price = _as_float(reserved.get(term))
        if price:
            return price
    return None


def ec2_architectures(instance_type):
    """
    Supported CPU architectures, e.g. ["arm64"] or ["x86_64"].
    Authoritative check that a Graviton target really is ARM.
    """
    doc = _instance("ec2", instance_type)
    if not doc:
        return ()
    arch = doc.get("arch") or []
    return tuple(arch) if isinstance(arch, list) else ()


def ec2_specs(instance_type):
    """
    (vcpu, memory_gib) or (None, None).

    The API field is "vCPU", not "vcpu" — reading the lower-case name returned
    None for every instance, which silently turned the rightsizing headroom
    check into "unable to verify" across the board.
    """
    doc = _instance("ec2", instance_type)
    if not doc:
        return None, None
    vcpu = doc.get("vCPU", doc.get("vcpu"))
    memory = doc.get("memory")
    return vcpu, memory


def ec2_supports_region(instance_type, region):
    doc = _instance("ec2", instance_type)
    if not doc:
        return None          # unknown, not "no"
    return region in (doc.get("pricing") or {})


# ─── RDS ──────────────────────────────────────────────────────────────────────

def rds_hourly(instance_type, region, engine, multi_az=False):
    prices = _region_pricing(_instance("rds", instance_type), region)
    if not prices:
        return None
    code = RDS_ENGINE_CODES.get((engine or "").lower())
    entry = prices.get(code) if code else None
    if entry is None:
        # Unknown engine — fall back to the cheapest quoted engine so we still
        # produce an order-of-magnitude figure rather than nothing.
        candidates = [v for v in prices.values() if isinstance(v, dict)]
        entry = min(candidates, key=lambda v: _as_float(v.get("ondemand")) or 1e9,
                    default=None)
    if not entry:
        return None
    price = _as_float(entry.get("ondemand"))
    # Vantage quotes Single-AZ; Multi-AZ is billed at roughly double.
    return round(price * 2, 6) if (price and multi_az) else price


def rds_reserved_hourly(instance_type, region, engine, multi_az=False):
    prices = _region_pricing(_instance("rds", instance_type), region)
    if not prices:
        return None
    code = RDS_ENGINE_CODES.get((engine or "").lower())
    entry = prices.get(code) if code else None
    if not entry:
        return None
    reserved = entry.get("reserved") or {}
    for term in RI_TERM_PREFERENCE:
        price = _as_float(reserved.get(term))
        if price:
            return round(price * 2, 6) if multi_az else price
    return None


# ─── ElastiCache ──────────────────────────────────────────────────────────────

def cache_hourly(instance_type, region, engine="Redis"):
    prices = _region_pricing(_instance("cache", instance_type), region)
    if not prices:
        return None
    wanted = (engine or "redis").lower()
    for key, entry in prices.items():
        if key.lower() == wanted and isinstance(entry, dict):
            price = _as_float(entry.get("ondemand"))
            if price:
                return price
    for entry in prices.values():
        if isinstance(entry, dict):
            price = _as_float(entry.get("ondemand"))
            if price:
                return price
    return None


def service_specs(service, instance_type):
    """
    (vcpu, memory_gib) for an RDS or ElastiCache class, or (None, None).

    Field naming differs by service — EC2 uses "vCPU", RDS and cache use
    "vcpu" — so both are tried rather than assumed.
    """
    doc = _instance(service, instance_type)
    if not doc:
        return None, None
    vcpu = doc.get("vcpu", doc.get("vCPU"))
    try:
        vcpu = int(float(vcpu)) if vcpu is not None else None
    except (TypeError, ValueError):
        vcpu = None
    try:
        memory = float(doc.get("memory")) if doc.get("memory") is not None else None
    except (TypeError, ValueError):
        memory = None
    return vcpu, memory


def rds_vcpu(instance_type):
    """vCPU count for an RDS instance class, or None."""
    doc = _instance("rds", instance_type)
    if not doc:
        return None
    try:
        return int(float(doc.get("vcpu")))
    except (TypeError, ValueError):
        return None


def monthly(hourly):
    return round(hourly * 730, 2) if hourly else None
