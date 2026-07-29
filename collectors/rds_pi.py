"""
RDS Performance Insights.
CPU avg alone is misleading for RDS rightsizing.
A DB at 10% CPU could be 80% IO-wait — rightsize does nothing for that.
PI tells us what the DB is actually waiting on.
"""

from datetime import timedelta

from utils import utcnow

from utils import utcnow
from collectors import api_errors


def get_pi_data(session, db_instance_id, region, days=14):
    rds = session.client("rds", region_name=region)
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        inst = resp["DBInstances"][0]
        pi_enabled = inst.get("PerformanceInsightsEnabled", False)
        resource_id = inst.get("DbiResourceId", "")
    except Exception:
        return {"pi_enabled": False}

    if not pi_enabled or not resource_id:
        return {"pi_enabled": False}

    pi = session.client("pi", region_name=region)
    end = utcnow()
    start = end - timedelta(days=days)

    try:
        resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            StartTime=start,
            EndTime=end,
            PeriodInSeconds=3600,
            MetricQueries=[
                {"Metric": "db.load.avg",
                 "GroupBy": {"Group": "db.wait_event", "Limit": 10}},
                {"Metric": "db.cpu.avg"},
            ],
        )
    except Exception:
        return {"pi_enabled": True, "pi_data_available": False}

    load_by_event = {}
    cpu_vals = []

    for result in resp.get("MetricList", []):
        key  = result.get("Key", {})
        dims = key.get("Dimensions", {})
        name = key.get("Metric", "")
        vals = [dp.get("Value", 0) for dp in result.get("DataPoints", [])
                if dp.get("Value") is not None]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if name == "db.cpu.avg":
            cpu_vals = vals
        elif name == "db.load.avg":
            event = dims.get("db.wait_event.name", "unknown")
            load_by_event[event] = round(avg, 4)

    total_load = sum(load_by_event.values()) or 1
    cpu_pct = round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else None

    io_load   = sum(v for k, v in load_by_event.items()
                    if any(w in k.lower() for w in ["io", "read", "write", "datafile"]))
    lock_load = sum(v for k, v in load_by_event.items()
                    if any(w in k.lower() for w in ["lock", "latch", "mutex"]))
    cpu_load  = load_by_event.get("CPU", 0)

    io_pct   = round(io_load / total_load * 100, 1)
    lock_pct = round(lock_load / total_load * 100, 1)
    cpu_lp   = round(cpu_load / total_load * 100, 1)

    if io_pct > 40:
        bottleneck = "IO-bound"
    elif lock_pct > 30:
        bottleneck = "Lock-contention"
    elif cpu_lp > 50:
        bottleneck = "CPU-bound"
    elif total_load < 0.5:
        bottleneck = "Idle"
    else:
        bottleneck = "Mixed"

    top_waits = sorted(load_by_event.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "pi_enabled":       True,
        "pi_data_available": True,
        "db_load_avg":      round(total_load, 3),
        "cpu_pct_pi":       cpu_pct,
        "bottleneck":       bottleneck,
        "io_wait_pct":      io_pct,
        "lock_wait_pct":    lock_pct,
        "cpu_load_pct":     cpu_lp,
        "top_wait_events":  [f"{k}: {round(v/total_load*100,1)}%" for k, v in top_waits],
    }


def enrich_rds_with_pi(session, rds_resources, days=14):
    """
    Attach Performance Insights findings to every RDS instance that has PI on.

    CPU average alone is a weak basis for RDS rightsizing: a database at 10% CPU
    and 80% IO-wait is not over-provisioned on CPU, and downsizing it makes the
    IO problem worse. PI is the only read-only source that distinguishes the two.

    Instances without PI enabled are marked so the report can say the check was
    unavailable rather than implying it passed.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch(r):
        try:
            return r["id"], get_pi_data(session, r["id"], r.get("region", "us-east-1"), days)
        except Exception:
            return r["id"], {"pi_enabled": False}

    if not rds_resources:
        return rds_resources
    results = {}
    with ThreadPoolExecutor(max_workers=min(6, len(rds_resources))) as ex:
        futures = [ex.submit(fetch, r) for r in rds_resources]
        for future in as_completed(futures):
            try:
                rid, data = future.result()
                results[rid] = data
            except Exception as _e:
                api_errors.note(_e, region=locals().get('region', ''))
                pass

    for r in rds_resources:
        r["performance_insights"] = results.get(r["id"], {"pi_enabled": False})
    return rds_resources
