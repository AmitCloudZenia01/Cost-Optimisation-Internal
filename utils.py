"""Small shared helpers."""

from datetime import datetime, timezone


def utcnow():
    """
    Naive UTC 'now'.

    Replaces datetime.utcnow(), which is deprecated from Python 3.12.
    Returns a naive datetime so it stays drop-in compatible with the
    boto3 StartTime/EndTime arguments and the existing comparisons.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_aware():
    """Timezone-aware UTC 'now' — use when comparing against AWS timestamps."""
    return datetime.now(timezone.utc)
