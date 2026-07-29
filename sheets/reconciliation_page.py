"""
"Reconciliation" tab — this report checked against the AWS bill.

Every other check in the project is internal: provenance present, columns
aligned, no saving larger than its resource. All of them pass on a report that
is uniformly wrong, and two did. 29 Elastic IPs read $0.00 against ~$105/mo of
real charges; a whole account reported $868.58 against a $1,394.85 bill. Both
were obvious the moment someone compared the total to Cost Explorer, and
nothing in the tool ever did.

This page is that comparison, on every run. It answers "can I trust the rest of
this?" with a number instead of an assurance.
"""

from .writer import safe_add_worksheet, safe_update, apply_formats
from .data_transfer_page import _c, _merge, _rh, _cw, NAVY, WHITE, LTBLUE

TAB   = {"red": 0.145, "green": 0.388, "blue": 0.537}
GOOD  = {"red": 0.851, "green": 0.918, "blue": 0.827}
WARN  = {"red": 0.996, "green": 0.898, "blue": 0.804}

NCOLS = 4


def build_reconciliation_page(spreadsheet, recon):
    if not recon or not recon.get("available"):
        return None

    services = list((recon.get("by_service") or {}).items())
    usage = recon.get("top_usage_types") or []
    ws = safe_add_worksheet(spreadsheet, "Reconciliation",
                            rows=len(services) + len(usage) + 30, cols=NCOLS + 1)
    if ws is None:
        return None
    sid = ws.id

    coverage = recon["coverage_pct"]
    rows = [["Reconciliation — this report vs the AWS bill"] + [""] * (NCOLS - 1)]
    rows.append([
        f"AWS billed ${recon['billed_usd']:,.2f} in {recon['month']}. This report "
        f"attributes ${recon['attributed_usd']:,.2f} of it to "
        f"{recon['resources_priced']} specific resources — {coverage}%. "
        f"The remaining ${recon['unexplained_usd']:,.2f} is broken down below. "
        f"Data transfer and per-request services bill activity rather than a "
        f"resource, so some gap is expected; a large or growing one is a charge "
        f"this report cannot see."
    ] + [""] * (NCOLS - 1))
    rows.append([""] * NCOLS)

    totals_hdr = len(rows)
    rows.append(["Billed (AWS)", "Attributed to resources", "Unexplained",
                 "Coverage %"])
    totals_row = len(rows)
    rows.append([recon["billed_usd"], recon["attributed_usd"],
                 recon["unexplained_usd"], coverage])
    rows.append([""] * NCOLS)

    svc_hdr = len(rows)
    rows.append(["Service (as AWS bills it)", "Monthly Cost (USD)", "", ""])
    svc_start = len(rows)
    for service, cost in services:
        rows.append([service, cost, "", ""])
    svc_end = len(rows)

    use_hdr = use_start = use_end = None
    if usage:
        rows.append([""] * NCOLS)
        use_hdr = len(rows)
        rows.append(["Largest usage types", "Monthly Cost (USD)",
                     "Should map to a resource?", ""])
        use_start = len(rows)
        for entry in usage:
            rows.append([entry["usage_type"], entry["cost_usd"],
                         "Yes" if entry["resource_backed"]
                         else "No — billed activity, not a resource", ""])
        use_end = len(rows)

    safe_update(ws, "A1", rows, value_input_option="RAW")

    money = {"type": "NUMBER", "pattern": '"$"#,##0.00'}
    pct = {"type": "NUMBER", "pattern": '0.0"%"'}
    band = GOOD if coverage >= 90 else WARN

    R = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColor": TAB}, "fields": "tabColor"}},
        _cw(sid, 0, 1, 340), _cw(sid, 1, 2, 170), _cw(sid, 2, 3, 300), _cw(sid, 3, 4, 130),
        _merge(sid, 0, 1, 0, NCOLS), _rh(sid, 0, 1, 44),
        _c(sid, 0, 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True, size=15, halign="CENTER"),
        _merge(sid, 1, 2, 0, NCOLS), _rh(sid, 1, 2, 66),
        _c(sid, 1, 2, 0, NCOLS, bg=LTBLUE, size=10, wrap=True),
        _c(sid, totals_hdr, totals_hdr + 1, 0, NCOLS, bg=NAVY, fg=WHITE,
           bold=True, size=10, halign="CENTER"),
        _c(sid, totals_row, totals_row + 1, 0, 3, nfmt=money, bold=True,
           size=12, halign="RIGHT", bg=band),
        _c(sid, totals_row, totals_row + 1, 3, 4, nfmt=pct, bold=True,
           size=12, halign="RIGHT", bg=band),
        _rh(sid, totals_row, totals_row + 1, 34),
        _c(sid, svc_hdr, svc_hdr + 1, 0, NCOLS, bg=NAVY, fg=WHITE,
           bold=True, size=10, halign="CENTER"),
    ]
    if svc_end > svc_start:
        R.append(_c(sid, svc_start, svc_end, 1, 2, nfmt=money, halign="RIGHT"))
    if use_hdr is not None:
        R.append(_c(sid, use_hdr, use_hdr + 1, 0, NCOLS, bg=NAVY, fg=WHITE,
                    bold=True, size=10, halign="CENTER"))
        if use_end > use_start:
            R.append(_c(sid, use_start, use_end, 1, 2, nfmt=money, halign="RIGHT"))
            R.append(_c(sid, use_start, use_end, 2, 3, wrap=True))

    apply_formats(spreadsheet, R)
    return ws
