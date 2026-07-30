"""
"Spend Patterns" tab — the time axis.

Every other tab is a snapshot. This one shows what the account is DOING:
spike days with their drivers, scheduled bursts, fast-growing lines, and
spend that started or stopped mid-window. All figures are daily billed cost
from Cost Explorer — measured, never modelled.
"""

from .writer import safe_add_worksheet, safe_update, apply_formats
from .data_transfer_page import _c, _merge, _rh, _cw, NAVY, WHITE, LTBLUE

TAB = {"red": 0.55, "green": 0.35, "blue": 0.12}
NCOLS = 4


def build_spend_patterns_page(spreadsheet, patterns):
    if not patterns or not patterns.get("available"):
        return None
    sections = []

    spikes = patterns.get("spikes", [])
    if spikes:
        rows = [["Date", "Day total (USD)", "Baseline day", "Driven by"]]
        for s in spikes:
            rows.append([s["date"], s["total"], s["baseline"],
                         ", ".join(f"{u.split(':')[-1]} ${c}" for u, c in s["drivers"])])
        sections.append(("Spike days — far above this account's own baseline", rows))

    bursts = patterns.get("bursts", [])
    if bursts:
        rows = [["Usage type", "Cost (USD)", "Cadence", "Active days"]]
        for b in bursts:
            rows.append([b["usage_type"], b["cost"], b["cadence"],
                         ", ".join(b["days"])])
        sections.append(("Scheduled bursts — scattered days at a regular cadence", rows))

    growing = patterns.get("growing", [])
    if growing:
        rows = [["Usage type", "First half (USD)", "Second half (USD)", "Growth"]]
        for g in growing:
            rows.append([g["usage_type"], g["first_half"], g["second_half"],
                         f"+{g['growth_pct']}%"])
        sections.append(("Fast-growing spend within the window", rows))

    for key, title in (("started", "Spend that STARTED mid-window"),
                       ("stopped", "Spend that STOPPED mid-window")):
        items = patterns.get(key, [])
        if items:
            day_field = "first_day" if key == "started" else "last_day"
            rows = [["Usage type", "Cost (USD)", "Date", ""]]
            for s in items:
                rows.append([s["usage_type"], s["cost"], s[day_field], ""])
            sections.append((title, rows))

    if not sections:
        return None

    all_rows = [["Spend Patterns — what this account is doing over time"] + [""] * (NCOLS - 1),
                [f"Daily billed cost by usage type. Median day: "
                 f"${patterns.get('baseline_daily', 0):,.2f}. Every figure is "
                 f"measured from billing — no estimates."] + [""] * (NCOLS - 1),
                [""] * NCOLS]
    header_rows = []
    for title, rows in sections:
        header_rows.append(len(all_rows))
        all_rows.append([title] + [""] * (NCOLS - 1))
        header_rows.append(len(all_rows))
        all_rows.append(rows[0])
        all_rows.extend(r + [""] * (NCOLS - len(r)) for r in rows[1:])
        all_rows.append([""] * NCOLS)

    ws = safe_add_worksheet(spreadsheet, "Spend Patterns",
                            rows=len(all_rows) + 10, cols=NCOLS + 1)
    if ws is None:
        return None
    sid = ws.id
    safe_update(ws, "A1", all_rows, value_input_option="RAW")

    R = [{"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColor": TAB}, "fields": "tabColor"}},
         _cw(sid, 0, 1, 300), _cw(sid, 1, 2, 140), _cw(sid, 2, 3, 140), _cw(sid, 3, 4, 420),
         _merge(sid, 0, 1, 0, NCOLS), _rh(sid, 0, 1, 44),
         _c(sid, 0, 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True, size=15, halign="CENTER"),
         _merge(sid, 1, 2, 0, NCOLS),
         _c(sid, 1, 2, 0, NCOLS, bg=LTBLUE, size=10, wrap=True)]
    for hr in header_rows:
        R.append(_c(sid, hr, hr + 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True,
                    size=10, halign="LEFT"))
    apply_formats(spreadsheet, R)
    return ws
