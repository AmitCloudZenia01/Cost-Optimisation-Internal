"""
Version lifecycle: which engine/cluster versions are out of support, when
support ends, and what extended support costs.

Everything that AWS exposes through an API is fetched live:

    EKS  eks:DescribeClusterVersions   → endOfStandardSupportDate,
                                         endOfExtendedSupportDate, status
    RDS  rds:DescribeDBMajorEngineVersions
                                       → SupportedEngineLifecycles
                                         (name, start date, end date)

Extended-support *prices* come from collectors.aws_pricing, per region.

Two things have no API and are therefore explicit REFERENCE data: ElastiCache
engine EOL and Lambda runtime deprecation. Both carry an as-of date, are
clearly labelled in the report, and never contribute a dollar figure.
"""

from datetime import date, datetime

from analysis.provenance import gaps, reference_basis

# ─── Live lookups ────────────────────────────────────────────────────────────

_eks_versions_cache = {}
_rds_lifecycle_cache = {}


def _parse_api_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        return None


def load_eks_versions(session, region):
    """
    {version: {standard_end, extended_end, status}} straight from the EKS API.
    Returns {} if the API is unavailable, and records the gap.
    """
    if region in _eks_versions_cache:
        return _eks_versions_cache[region]

    versions = {}
    try:
        eks = session.client("eks", region_name=region)
        paginator = eks.get_paginator("describe_cluster_versions")
        pages = paginator.paginate()
        for page in pages:
            for entry in page.get("clusterVersions", []):
                versions[entry.get("clusterVersion", "")] = {
                    "standard_end": _parse_api_date(entry.get("endOfStandardSupportDate")),
                    "extended_end": _parse_api_date(entry.get("endOfExtendedSupportDate")),
                    "status": entry.get("versionStatus") or entry.get("status") or "",
                    "release_date": _parse_api_date(entry.get("releaseDate")),
                }
    except Exception as e:
        gaps.add(
            category="Lifecycle",
            what="EKS version support dates",
            why=f"eks:DescribeClusterVersions unavailable in {region} ({e}).",
            how_to_fix="Grant eks:DescribeClusterVersions, or upgrade boto3 — "
                       "the API is required for support-date checks.",
            region=region,
            impact="EKS extended-support findings are omitted for this region.")

    _eks_versions_cache[region] = versions
    return versions


def load_rds_lifecycle(session, region, engine, major_version):
    """
    Support windows for one RDS major engine version, from
    rds:DescribeDBMajorEngineVersions. Returns [] when unavailable.
    """
    key = (region, engine, major_version)
    if key in _rds_lifecycle_cache:
        return _rds_lifecycle_cache[key]

    cycles = []
    try:
        rds = session.client("rds", region_name=region)
        resp = rds.describe_db_major_engine_versions(
            Engine=engine, MajorEngineVersion=major_version)
        for entry in resp.get("DBMajorEngineVersions", []):
            for cycle in entry.get("SupportedEngineLifecycles", []) or []:
                cycles.append({
                    "name": cycle.get("LifecycleSupportName", ""),
                    "start": _parse_api_date(cycle.get("LifecycleSupportStartDate")),
                    "end": _parse_api_date(cycle.get("LifecycleSupportEndDate")),
                })
    except Exception:
        # Not every engine/version combination is queryable; this is common
        # and not worth a gap entry per database.
        pass

    _rds_lifecycle_cache[key] = cycles
    return cycles


# ─── EKS ─────────────────────────────────────────────────────────────────────

def check_eks_version(version, session=None, region="us-east-1",
                      extended_hourly=None):
    """
    Support status for an EKS cluster version.

    version:          e.g. "1.29"
    extended_hourly:  live extended-support rate from aws_pricing, or None.
                      When None the finding still fires, but with no dollar
                      figure attached rather than an invented one.
    """
    major_minor = ".".join(str(version).split(".")[:2])
    unknown = {"status": "unknown", "warning": None,
               "extended_cost_mo": None, "urgency": None, "source": "unavailable"}

    if session is None:
        return unknown

    versions = load_eks_versions(session, region)
    info = versions.get(major_minor)

    if not info or not info.get("standard_end"):
        # AWS delists versions once they are past extended support, so an
        # absent version that sorts below everything AWS still lists is not
        # "unknown" — it is older than the oldest supported release. Inferred
        # from the live list, not from a hardcoded floor.
        listed = []
        for known in versions:
            try:
                listed.append(tuple(int(p) for p in known.split(".")))
            except ValueError:
                continue
        try:
            current = tuple(int(p) for p in major_minor.split("."))
        except ValueError:
            return unknown
        if listed and current < min(listed):
            oldest = ".".join(str(p) for p in min(listed))
            return {
                "status": "end_of_life",
                "warning": (f"CRITICAL: EKS {version} is older than {oldest}, the "
                            f"oldest version AWS still publishes support dates for. "
                            f"It is past end of extended support."),
                "extended_cost_mo": None,
                "urgency": "Critical",
                "source": "eks:DescribeClusterVersions (version delisted)",
            }
        return unknown

    today = date.today()
    std_end = info["standard_end"]
    ext_end = info["extended_end"]
    extended_cost_mo = round(extended_hourly * 730, 2) if extended_hourly else None
    cost_phrase = (f"AWS charges ~${extended_cost_mo:,.0f}/mo per cluster"
                   if extended_cost_mo is not None
                   else "AWS applies an extended-support charge per cluster "
                        "(rate could not be resolved)")

    if ext_end and today > ext_end:
        return {
            "status": "end_of_life",
            "warning": (f"CRITICAL: EKS {version} is past extended support "
                        f"({ext_end}). Cluster may be forcibly upgraded."),
            "extended_cost_mo": None,
            "urgency": "Critical",
            "source": "eks:DescribeClusterVersions",
        }

    if today > std_end:
        days_left = (ext_end - today).days if ext_end else None
        tail = f" Extended support ends {ext_end} ({days_left} days)." if ext_end else ""
        return {
            "status": "extended_support",
            "warning": (f"EKS {version} is on EXTENDED SUPPORT (standard support "
                        f"ended {std_end}). {cost_phrase}.{tail} "
                        f"Upgrade to remove the ongoing charge."),
            "extended_cost_mo": extended_cost_mo,
            "urgency": "High" if (days_left is not None and days_left < 90) else "Medium",
            "source": "eks:DescribeClusterVersions",
        }

    days_to_std_end = (std_end - today).days
    if days_to_std_end <= 90:
        return {
            "status": "expiring_soon",
            "warning": (f"EKS {version} standard support ends {std_end} "
                        f"({days_to_std_end} days). After that {cost_phrase}."),
            "extended_cost_mo": None,
            "urgency": "Medium",
            "source": "eks:DescribeClusterVersions",
        }

    return {"status": "active", "warning": None, "extended_cost_mo": None,
            "urgency": None, "source": "eks:DescribeClusterVersions"}


# ─── RDS ─────────────────────────────────────────────────────────────────────

def check_rds_version(engine, version, session=None, region="us-east-1"):
    """Support status for an RDS engine version, from the RDS API."""
    unknown = {"status": "unknown", "warning": None,
               "extended_cost_mo": None, "urgency": None, "source": "unavailable"}
    if session is None or not engine or not version:
        return unknown

    parts = str(version).split(".")
    candidates = [".".join(parts[:2]), parts[0]]
    cycles = []
    for candidate in candidates:
        cycles = load_rds_lifecycle(session, region, engine.lower(), candidate)
        if cycles:
            break
    if not cycles:
        return unknown

    today = date.today()
    standard = next((c for c in cycles if "extended" not in c["name"].lower()), None)
    extended = next((c for c in cycles if "extended" in c["name"].lower()), None)

    if extended and extended["end"] and today > extended["end"]:
        return {
            "status": "end_of_life",
            "warning": (f"CRITICAL: {engine} {version} is past end of extended "
                        f"support ({extended['end']}). No security patches."),
            "extended_cost_mo": None, "urgency": "Critical",
            "source": "rds:DescribeDBMajorEngineVersions",
        }

    if standard and standard["end"] and today > standard["end"]:
        days_left = (extended["end"] - today).days if extended and extended["end"] else None
        tail = (f" Extended support ends {extended['end']} ({days_left} days)."
                if days_left is not None else "")
        return {
            "status": "extended_support",
            "warning": (f"{engine} {version} is on EXTENDED SUPPORT (standard "
                        f"support ended {standard['end']}). AWS bills extended "
                        f"support per vCPU-hour.{tail} Upgrade to remove it."),
            # Priced by the recommender from the live rate and the instance vCPU
            # count; never assumed here.
            "extended_cost_mo": None,
            "urgency": "High" if (days_left is not None and days_left < 90) else "Medium",
            "source": "rds:DescribeDBMajorEngineVersions",
        }

    if standard and standard["end"]:
        days_left = (standard["end"] - today).days
        if days_left <= 90:
            return {
                "status": "expiring_soon",
                "warning": (f"{engine} {version} standard support ends "
                            f"{standard['end']} ({days_left} days). Plan the "
                            f"upgrade to avoid extended-support charges."),
                "extended_cost_mo": None, "urgency": "Medium",
                "source": "rds:DescribeDBMajorEngineVersions",
            }

    return {"status": "active", "warning": None, "extended_cost_mo": None,
            "urgency": None, "source": "rds:DescribeDBMajorEngineVersions"}


# ─── ElastiCache — no API exists, so this is declared reference data ─────────

ELASTICACHE_REFERENCE_AS_OF = date(2026, 1, 1)

ELASTICACHE_LIFECYCLE = {
    "redis":     {"5": "2024-01-31", "6": "2026-12-31", "7": "2028-12-31"},
    "memcached": {"1.5": "2023-06-30", "1.6": "2026-12-31"},
    "valkey":    {"7": "2028-12-31", "8": "2029-12-31"},
}


def check_elasticache_version(engine, version):
    """
    ElastiCache publishes no engine-lifecycle API, so this is a dated static
    table. It is labelled REFERENCE in the report and never priced.
    """
    engine_data = ELASTICACHE_LIFECYCLE.get((engine or "").lower())
    if not engine_data:
        return {"status": "unknown", "warning": None, "urgency": None}

    major = str(version).split(".")[0]
    eol_str = engine_data.get(major) or engine_data.get(
        ".".join(str(version).split(".")[:2]))
    if not eol_str:
        return {"status": "unknown", "warning": None, "urgency": None}

    eol = _parse_api_date(eol_str)
    today = date.today()
    stale_note = (f"Reference data as of {ELASTICACHE_REFERENCE_AS_OF} — "
                  f"AWS provides no lifecycle API for ElastiCache; verify "
                  f"against current AWS documentation.")

    if eol and today > eol:
        return {
            "status": "end_of_life",
            "warning": (f"ElastiCache {engine} {version} reached end of life "
                        f"({eol}). Upgrade required. {stale_note}"),
            "urgency": "Critical", "source": "reference",
            "basis": reference_basis(ELASTICACHE_REFERENCE_AS_OF),
        }
    if eol and (eol - today).days <= 180:
        return {
            "status": "expiring_soon",
            "warning": (f"ElastiCache {engine} {version} EOL: {eol} "
                        f"({(eol - today).days} days). {stale_note}"),
            "urgency": "Medium", "source": "reference",
            "basis": reference_basis(ELASTICACHE_REFERENCE_AS_OF),
        }
    return {"status": "active", "warning": None, "urgency": None}


# ─── RDS Graviton support ────────────────────────────────────────────────────

RDS_GRAVITON_SUPPORT = {
    "mysql":             {"min_version": "8.0.17", "supported": True},
    "postgres":          {"min_version": "12.0",   "supported": True},
    "aurora-mysql":      {"min_version": "3.0.0",  "supported": True},
    "aurora-postgresql": {"min_version": "13.0",   "supported": True},
    "mariadb":           {"min_version": "10.6",   "supported": True},
    "oracle-ee":         {"min_version": None,     "supported": False},
    "oracle-se2":        {"min_version": None,     "supported": False},
    "sqlserver-ex":      {"min_version": None,     "supported": False},
    "sqlserver-se":      {"min_version": None,     "supported": False},
    "sqlserver-ee":      {"min_version": None,     "supported": False},
    "sqlserver-web":     {"min_version": None,     "supported": False},
}


def _version_tuple(version):
    """Numeric-prefix version compare — fallback when `packaging` is absent."""
    parts = []
    for chunk in str(version).split("-")[0].split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def rds_graviton_compatible(engine, version):
    """Returns (compatible: bool, reason: str)"""
    info = RDS_GRAVITON_SUPPORT.get((engine or "").lower())
    if not info:
        return False, f"Graviton support unknown for engine: {engine}"
    if not info["supported"]:
        return False, f"{engine} does not support Graviton instances (x86 only)"
    min_ver = info.get("min_version")
    if min_ver:
        # Import inside the try: `packaging` is not a hard dependency of
        # boto3/gspread, and an ImportError here used to kill the whole run.
        try:
            from packaging.version import Version
            if Version(str(version).split("-")[0]) < Version(min_ver):
                return False, f"{engine} {version} < minimum {min_ver} required for Graviton"
        except ImportError:
            if _version_tuple(version) < _version_tuple(min_ver):
                return False, f"{engine} {version} < minimum {min_ver} required for Graviton"
        except Exception:
            pass
    return True, f"{engine} {version} supports Graviton"
