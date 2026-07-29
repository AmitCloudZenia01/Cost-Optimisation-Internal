from .writer import header_format, freeze_row, auto_resize, apply_formats, color_delta_cells, safe_add_worksheet, safe_update


def build_differential_page(spreadsheet, diff):
    if not diff or not diff.get("has_previous"):
        return None

    ws = safe_add_worksheet(spreadsheet, "Changes", rows=500, cols=12)
    sheet_id = ws.id
    rows = []

    rows.append(["AWS Cost Changes vs Previous Report", "", "", "", ""])
    rows.append([f"Previous: {diff.get('previous_timestamp', 'N/A')}", "", "", "", ""])
    rows.append([f"Current:  {diff.get('current_timestamp', 'N/A')}", "", "", "", ""])
    rows.append(["", "", "", "", ""])

    # Overall delta
    delta = diff["total_delta"]
    delta_pct = diff.get("total_delta_pct", "")
    direction = "Increase" if delta > 0 else "Saving"
    rows.append([
        f"Total Cost Change ({direction})",
        f"${diff['total_cost_prev']:,.2f} -> ${diff['total_cost_curr']:,.2f}",
        f"${delta:+,.2f}",
        f"{delta_pct:+}%" if delta_pct else "",
        ""
    ])
    rows.append(["", "", "", "", ""])

    # Service-level diff
    rows.append(["Cost Change by Service", "", "", "", ""])
    rows.append(["Service", "Previous (USD)", "Current (USD)", "Delta (USD)", "Delta %"])
    for svc, data in diff.get("service_diff", {}).items():
        d = data["delta"]
        dp = data.get("delta_pct", "")
        rows.append([
            svc,
            data["prev_cost"], data["curr_cost"],
            round(d, 2),
            f"{dp:+}%" if dp is not None else "",
        ])
    rows.append(["", "", "", "", ""])

    # New resources
    added = diff.get("added_resources", [])
    if added:
        rows.append([f"New Resources ({len(added)})", "", "", "", ""])
        rows.append(["Type", "Name", "ID", "Region", "Monthly Cost (USD)"])
        for r in added:
            rows.append([
                r.get("type", ""), r.get("name", ""), r.get("id", ""),
                r.get("region", ""), r.get("monthly_cost_usd", ""),
            ])
        rows.append(["", "", "", "", ""])

    # Removed resources
    removed = diff.get("removed_resources", [])
    if removed:
        rows.append([f"Removed Resources ({len(removed)})", "", "", "", ""])
        rows.append(["Type", "Name", "ID", "Region", "Prev Monthly Cost (USD)"])
        for r in removed:
            rows.append([
                r.get("type", ""), r.get("name", ""), r.get("id", ""),
                r.get("region", ""), r.get("monthly_cost_usd", ""),
            ])
        rows.append(["", "", "", "", ""])

    # Changed resources
    changed = diff.get("changed_resources", [])
    if changed:
        rows.append([f"Changed Resources ({len(changed)})", "", "", "", ""])
        rows.append([
            "Type", "Name", "ID", "Region",
            "Prev Type", "Curr Type",
            "Prev Cost", "Curr Cost", "Delta (USD)", "Delta %", "Status"
        ])
        for r in changed:
            rows.append([
                r.get("type", ""), r.get("name", ""), r.get("id", ""),
                r.get("region", ""),
                r.get("prev_instance_type", ""), r.get("curr_instance_type", ""),
                r.get("prev_cost", ""), r.get("curr_cost", ""),
                r.get("cost_delta", ""),
                f"{r.get('cost_delta_pct', '')}%" if r.get("cost_delta_pct") is not None else "",
                r.get("status", ""),
            ])

    safe_update(ws, "A1", rows, value_input_option="USER_ENTERED")

    delta_col_index = 3
    data_start = 7
    data_end = data_start + len(diff.get("service_diff", {}))

    formats = [
        header_format(sheet_id),
        freeze_row(sheet_id, 1),
        auto_resize(sheet_id),
    ]
    formats.extend(color_delta_cells(sheet_id, data_start, data_end, delta_col_index))
    apply_formats(spreadsheet, formats)

    return ws
