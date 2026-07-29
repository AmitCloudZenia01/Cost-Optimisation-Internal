"""
Turning swallowed AWS errors into visible gaps.

The collectors are full of broad `except Exception: pass` blocks. Most were
written so one missing optional field could not kill a whole scan — reasonable
— but the effect is that "AWS refused to tell us" and "there is nothing here"
produce byte-identical output: an empty list, an empty tab, no mention anywhere.
That is the silent wrongness this project exists to eliminate.

The important distinction is NOT failure vs success. It is:

    AWS answered "there is none"   -> that IS the answer. Stay silent.
    AWS refused, throttled or broke -> we do not know. Record a gap.

A bucket with no lifecycle configuration raises NoSuchLifecycleConfiguration;
that is a fact, not a gap. A denied DescribeVolumes means the volumes might be
there and we cannot see them; the report must say so.

botocore exceptions carry `operation_name` and an error code, so the caller
does not have to name the operation — the exception already knows.
"""

from analysis.provenance import gaps

# AWS's way of saying "that thing does not exist / is not configured".
# These are answers, not failures, and must not clutter the Data Gaps tab.
_MEANS_NONE = (
    "NoSuch", "NotFound", "NotFoundException", "ResourceNotFoundException",
    "does not exist", "DoesNotExist", "NoSuchEntity", "InvalidParameterValue",
    "ValidationException", "NoSuchLifecycleConfiguration",
    "NoSuchTagSet", "NoSuchBucketPolicy", "NoSuchPublicAccessBlockConfiguration",
    "NoSuchReplicationConfiguration", "ServerSideEncryptionConfigurationNotFound",
    "LifecyclePolicyNotFoundException", "RepositoryPolicyNotFoundException",
    "InvalidAction", "UnsupportedOperation", "NoSuchConfiguration",
)

_MEANS_DENIED = (
    "AccessDenied", "UnauthorizedOperation", "AuthorizationError",
    "AccessDeniedException", "not authorized", "Forbidden",
    "InvalidClientTokenId", "UnrecognizedClientException",
    "ExpiredToken", "ExpiredTokenException", "SignatureDoesNotMatch",
)

_MEANS_THROTTLED = (
    "Throttling", "ThrottlingException", "TooManyRequests",
    "RequestLimitExceeded", "RateExceeded", "SlowDown",
)


def _describe(exc):
    """(operation, code, text) for any exception, botocore or not."""
    operation = getattr(exc, "operation_name", "") or ""
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code", "") or ""
    return operation, code, str(exc)


def classify(exc):
    """'none' | 'denied' | 'throttled' | 'error'"""
    _, code, text = _describe(exc)
    haystack = f"{code} {text}"
    if any(k in haystack for k in _MEANS_DENIED):
        return "denied"
    if any(k in haystack for k in _MEANS_THROTTLED):
        return "throttled"
    if any(k in haystack for k in _MEANS_NONE):
        return "none"
    return "error"


def note(exc, context="", region="", resource_type="", optional=True):
    """
    Record an AWS failure as a gap, unless AWS simply answered "there is none".

    optional=True marks an enrichment call — one missing tag set does not
    invalidate the resource. Even so, a DENIED enrichment is still recorded,
    because a permission gap affects every resource of that type, not one.

    Gaps deduplicate on (category, what, type, region), so a denial repeated
    across 500 resources produces exactly one row.
    """
    kind = classify(exc)
    if kind == "none":
        return kind          # AWS answered; nothing to report

    operation, code, text = _describe(exc)
    label = operation or context or "AWS API call"

    if kind == "denied":
        why = (f"Permission denied calling {label}"
               + (f" in {region}" if region else "") + ".")
        fix = f"Grant read access to {label}."
        impact = ("Affected values are missing from the report — this is "
                  "'not checked', not 'nothing found'.")
    elif kind == "throttled":
        why = f"{label} was throttled by AWS" + (f" in {region}" if region else "") + "."
        fix = "Re-run; if it persists, reduce the number of regions per run."
        impact = "Some values may be missing from this run."
    else:
        why = f"{label} failed: {text[:160]}"
        fix = "Re-run; if it persists, report the error."
        impact = ("Affected values are missing from the report."
                  if not optional else
                  "Some optional detail is missing; core figures are unaffected.")

    gaps.add(
        category="Collection",
        what=f"{label}{f' ({region})' if region else ''}",
        why=why,
        how_to_fix=fix,
        region=region,
        resource_type=resource_type,
        impact=impact,
    )
    return kind


class attempt:
    """
    Context manager for an AWS call whose failure must not abort the caller.

        with attempt(region=region, context="s3:GetBucketTagging"):
            tags = {...}

    Swallows the exception exactly as `except Exception: pass` did, but routes
    it through note() so a denial or an error becomes visible.
    """

    __slots__ = ("context", "region", "resource_type", "optional", "outcome")

    def __init__(self, context="", region="", resource_type="", optional=True):
        self.context = context
        self.region = region
        self.resource_type = resource_type
        self.optional = optional
        self.outcome = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        self.outcome = note(exc, self.context, self.region,
                            self.resource_type, self.optional)
        return True          # suppress, as the original bare handler did
