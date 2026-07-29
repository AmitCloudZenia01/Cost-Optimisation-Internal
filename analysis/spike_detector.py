"""
Utilization safety checks.

The keys here MUST match what collectors/metrics_auto.py writes. They did not:
metrics_auto stored spikes under "spikes", while this module read "cpu_spikes".
Every spike was therefore invisible — spikes never blocked a rightsize, the
"Spike Notes" column was always empty, and the recommender printed
"No CPU spikes detected" without ever having looked.
"""

# Metrics reported as a percentage — anything else is a raw unit (ms, count).
_PCT_METRICS = {"CPUUtilization", "EngineCPUUtilization", "CpuUser",
                "MemoryUtilization"}


def _label_for(metric_name):
    if not metric_name:
        return "CPU", "%"
    if metric_name == "Duration":
        return "Duration", " ms"
    if metric_name in _PCT_METRICS:
        return "CPU", "%"
    return metric_name, ""


def format_spike_note(spikes, metric_name="CPUUtilization", window_days=90):
    if not spikes:
        return ""
    label, unit = _label_for(metric_name)
    count = len(spikes)
    max_spike = max(spikes, key=lambda x: x["ratio"])
    return (
        f"SPIKE: {count} spike(s) detected in {label} over {window_days} days. "
        f"Peak: {max_spike['value']:.1f}{unit} "
        f"({max_spike['ratio']}x avg of {max_spike['avg']:.1f}{unit}). "
        f"Do NOT rightsize without verifying spike cause."
    )


def get_spike_summary(resource_id, metrics):
    if not metrics:
        return ""
    spikes = metrics.get("spikes") or []
    if not spikes:
        return ""
    return format_spike_note(
        spikes,
        metrics.get("spike_metric", "CPUUtilization"),
        metrics.get("spike_window_days", 90),
    )


def spike_count(metrics):
    return len((metrics or {}).get("spikes") or [])


def has_safe_utilization(metrics, cpu_threshold=40, mem_threshold=50,
                         min_datapoints=0, peak_threshold=70):
    """
    Returns (safe: bool, reason: str).

    Judged on the PEAK, not just the average. An instance averaging 8% CPU
    while touching 95% cannot safely be downsized, but an average-only test
    calls it safe — which is how average-based rightsizing causes outages.

    p95 is the working figure: a single Maximum datapoint can be an
    unrepresentative blip, whereas p95 means the workload genuinely sat at that
    level 5% of the time. Maximum is still reported as evidence.

    Order of checks matters — cheapest and most decisive first.
    """
    if not metrics or not metrics.get("periods"):
        return False, "No metrics data"

    p30 = metrics["periods"].get("30d") or {}
    cpu = p30.get("cpu_avg_pct")

    if cpu is None:
        return False, "No CPU data"

    datapoints = metrics.get("datapoints", 0)
    if min_datapoints and datapoints and datapoints < min_datapoints:
        return False, (f"Only {datapoints} datapoints in the observation window "
                       f"(need {min_datapoints}) — not enough history to judge")

    if cpu > cpu_threshold:
        return False, f"CPU 30d avg {cpu:.1f}% > {cpu_threshold}% threshold"

    # Peak gate
    cpu_p95 = p30.get("cpu_p95_pct")
    cpu_max = p30.get("cpu_max_pct")
    if cpu_p95 is not None and cpu_p95 > peak_threshold:
        return False, (f"CPU p95 {cpu_p95:.1f}% > {peak_threshold}% peak threshold "
                       f"(30d average is only {cpu:.1f}% — the average hides the peak)")
    if cpu_p95 is None and cpu_max is not None and cpu_max > peak_threshold:
        return False, (f"CPU peak {cpu_max:.1f}% > {peak_threshold}% threshold "
                       f"(30d average is only {cpu:.1f}%)")

    mem = p30.get("mem_used_pct")
    mem_p95 = p30.get("mem_p95_pct")
    mem_max = p30.get("mem_max_pct")
    if mem is not None and mem > mem_threshold:
        return False, f"Memory 30d avg {mem:.1f}% > {mem_threshold}% threshold"
    if mem_p95 is not None and mem_p95 > peak_threshold:
        return False, (f"Memory p95 {mem_p95:.1f}% > {peak_threshold}% peak threshold "
                       f"(average is only {mem:.1f}%)")
    if mem_p95 is None and mem_max is not None and mem_max > peak_threshold:
        return False, f"Memory peak {mem_max:.1f}% > {peak_threshold}% threshold"

    spikes = metrics.get("spikes") or []
    if spikes:
        window = metrics.get("spike_window_days", 90)
        return False, f"{len(spikes)} utilization spike(s) detected over {window} days"

    parts = [f"CPU 30d avg {cpu:.1f}%"]
    if cpu_p95 is not None:
        parts.append(f"p95 {cpu_p95:.1f}%")
    if cpu_max is not None:
        parts.append(f"peak {cpu_max:.1f}%")
    if mem is not None:
        parts.append(f"memory {mem:.1f}%")
    return True, ", ".join(parts) + " — safe to rightsize"


def peak_utilisation(metrics, metric="cpu"):
    """
    Best available peak figure and how it was measured, or (None, "").

    Prefers p95 over Maximum; the caller must handle None rather than assume a
    value, since a missing peak means we cannot verify headroom.
    """
    p30 = ((metrics or {}).get("periods") or {}).get("30d") or {}
    p95 = p30.get(f"{metric}_p95_pct")
    if p95 is not None:
        return p95, "p95"
    peak = p30.get(f"{metric}_max_pct")
    if peak is not None:
        return peak, "maximum"
    return None, ""
