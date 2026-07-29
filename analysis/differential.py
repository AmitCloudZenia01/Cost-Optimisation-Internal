import json
import os
from datetime import datetime
from glob import glob

from utils import utcnow


def save_snapshot(data, snapshots_dir, account_id):
    os.makedirs(snapshots_dir, exist_ok=True)
    timestamp = utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"{account_id}_{timestamp}.json"
    path = os.path.join(snapshots_dir, filename)
    with open(path, "w") as f:
        json.dump({"timestamp": timestamp, "account_id": account_id, "data": data}, f, indent=2, default=str)
    return path


def load_latest_snapshot(snapshots_dir, account_id, exclude_path=None):
    pattern = os.path.join(snapshots_dir, f"{account_id}_*.json")
    files = sorted(glob(pattern))
    if exclude_path:
        files = [f for f in files if f != exclude_path]
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def _flatten_resources(snapshot_data):
    flat = {}
    resources = snapshot_data.get("data", {}).get("resources", {})
    for service, items in resources.items():
        for item in items:
            flat[item["id"]] = item
    return flat


def _get_cost_map(snapshot_data):
    services = {}
    for row in snapshot_data.get("data", {}).get("monthly_costs", []):
        svc = row["service"]
        if svc not in services:
            services[svc] = 0.0
        services[svc] += row["cost"]
    return services


def compute_diff(current_snapshot, previous_snapshot):
    if not previous_snapshot:
        return {"has_previous": False}

    current_resources = _flatten_resources(current_snapshot)
    previous_resources = _flatten_resources(previous_snapshot)

    current_ids = set(current_resources.keys())
    previous_ids = set(previous_resources.keys())

    added = []
    removed = []
    changed = []

    for rid in current_ids - previous_ids:
        r = current_resources[rid]
        added.append({
            "id": rid,
            "name": r.get("name", rid),
            "type": r.get("type", ""),
            "region": r.get("region", ""),
            "monthly_cost_usd": r.get("monthly_cost_usd"),
        })

    for rid in previous_ids - current_ids:
        r = previous_resources[rid]
        removed.append({
            "id": rid,
            "name": r.get("name", rid),
            "type": r.get("type", ""),
            "region": r.get("region", ""),
            "monthly_cost_usd": r.get("monthly_cost_usd"),
        })

    for rid in current_ids & previous_ids:
        curr = current_resources[rid]
        prev = previous_resources[rid]
        curr_cost = curr.get("monthly_cost_usd") or 0
        prev_cost = prev.get("monthly_cost_usd") or 0
        curr_type = curr.get("instance_type", "")
        prev_type = prev.get("instance_type", "")

        cost_delta = round(curr_cost - prev_cost, 2)
        type_changed = curr_type != prev_type

        if abs(cost_delta) > 1 or type_changed:
            change = {
                "id": rid,
                "name": curr.get("name", rid),
                "type": curr.get("type", ""),
                "region": curr.get("region", ""),
                "prev_cost": prev_cost,
                "curr_cost": curr_cost,
                "cost_delta": cost_delta,
                "cost_delta_pct": round((cost_delta / prev_cost * 100), 1) if prev_cost else None,
                "prev_instance_type": prev_type,
                "curr_instance_type": curr_type,
                "type_changed": type_changed,
            }
            if type_changed and curr_type and prev_type:
                change["status"] = "Rightsized" if curr_cost < prev_cost else "Upgraded"
            elif cost_delta < 0:
                change["status"] = "Cost reduced"
            elif cost_delta > 0:
                change["status"] = "Cost increased"
            else:
                change["status"] = "No change"
            changed.append(change)

    current_costs = _get_cost_map(current_snapshot)
    previous_costs = _get_cost_map(previous_snapshot)

    all_services = set(current_costs) | set(previous_costs)
    service_diff = {}
    for svc in all_services:
        curr = current_costs.get(svc, 0)
        prev = previous_costs.get(svc, 0)
        delta = round(curr - prev, 2)
        service_diff[svc] = {
            "prev_cost": round(prev, 2),
            "curr_cost": round(curr, 2),
            "delta": delta,
            "delta_pct": round(delta / prev * 100, 1) if prev else None,
        }

    prev_total = sum(previous_costs.values())
    curr_total = sum(current_costs.values())

    return {
        "has_previous": True,
        "previous_timestamp": previous_snapshot.get("timestamp", ""),
        "current_timestamp": current_snapshot.get("timestamp", ""),
        "added_resources": sorted(added, key=lambda x: x.get("monthly_cost_usd") or 0, reverse=True),
        "removed_resources": sorted(removed, key=lambda x: x.get("monthly_cost_usd") or 0, reverse=True),
        "changed_resources": sorted(changed, key=lambda x: abs(x["cost_delta"]), reverse=True),
        "service_diff": dict(sorted(service_diff.items(), key=lambda x: abs(x[1]["delta"]), reverse=True)),
        "total_cost_prev": round(prev_total, 2),
        "total_cost_curr": round(curr_total, 2),
        "total_delta": round(curr_total - prev_total, 2),
        "total_delta_pct": round((curr_total - prev_total) / prev_total * 100, 1) if prev_total else None,
    }


def prune_old_snapshots(snapshots_dir, account_id, keep_last=10):
    pattern = os.path.join(snapshots_dir, f"{account_id}_*.json")
    files = sorted(glob(pattern))
    to_delete = files[:-keep_last] if len(files) > keep_last else []
    for f in to_delete:
        os.remove(f)
    return len(to_delete)
