"""
"Commitments" tab — Reserved Instances and Savings Plans the account already holds.

Two reasons this is in the report rather than hidden in the analysis:

  1. It is the evidence behind every "already covered — no new reservation
     needed" finding. A reader must be able to check that claim.

  2. When commitments exist, every list-price saving elsewhere in the report is
     overstated. The banner at the top of this tab says so explicitly.
"""

from .writer import safe_add_worksheet, safe_update, apply_formats

NAVY   = {"red": 0.098, "green": 0.216, "blue": 0.412}
WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
LTBLUE = {"red": 0.937, "green": 0.953, "blue": 0.984}
AMBER  = {"red": 1.000, "green": 0.949, "blue": 0.800}
GREEN  = {"red": 0.851, "green": 0.961, "blue": 0.878}
DKGREY = {"red": 0.200, "green": 0.200, "blue": 0.200}
TAB    = {"red": 0.129, "green": 0.302, "blue": 0.608}

HEADERS = ["Type", "Region", "Identifier", "Instance Type", "Count",
           "Scope / Plan", "Payment", "Platform", "Start", "End", "State"]
NCOLS = len(HEADERS)


def _c(sid, r1, r2, c1, c2, bg=None, fg=None, bold=False, size=10,
       halign="LEFT", valign="MIDDLE", wrap=False):
    tf = {"bold": bold, "fontSize": size}
    if fg:
        tf["foregroundColor"] = fg
    uf = {"textFormat": tf, "horizontalAlignment": halign, "verticalAlignment": valign}
    if bg:
        uf["backgroundColor"] = bg
    if wrap:
        uf["wrapStrategy"] = "WRAP"
    fields = "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment"
    if bg:
        fields += ",backgroundColor"
    if wrap:
        fields += ",wrapStrategy"
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "cell": {"userEnteredFormat": uf}, "fields": fields + ")"}}


def _merge(sid, r1, r2, c1, c2):
    return {"mergeCells": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "mergeType": "MERGE_ALL"}}


def _rh(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def build_commitments_page(spreadsheet, commitment_data, purchase_data=None):
    items = commitment_data.get("items", [])
    has = commitment_data.get("has_commitments", False)

    ws = safe_add_worksheet(spreadsheet, "Commitments",
                            rows=len(items) + 20, cols=NCOLS + 1)
    sid = ws.id
    rows = [["Reserved Instances & Savings Plans"] + [""] * (NCOLS - 1)]

    if has:
        rows.append([
            f"{len(items)} active commitment(s) found. Savings elsewhere in this "
            f"report are quoted against ON-DEMAND LIST PRICE and are therefore "
            f"OVERSTATED for any resource these cover. Enable a Cost and Usage "
            f"Report to compute savings against what you actually pay."
        ] + [""] * (NCOLS - 1))
    else:
        rows.append([
            "No active Reserved Instances or Savings Plans were found. "
            "On-demand list price is therefore the correct baseline for this "
            "account, and the savings in this report are not distorted by "
            "existing commitments."
        ] + [""] * (NCOLS - 1))

    # AWS's own purchase recommendations, with the overlap stated explicitly.
    if purchase_data and purchase_data.get("total_monthly_savings"):
        rows.append([""] * NCOLS)
        rows.append(["AWS purchase recommendations (from your billing history)"]
                    + [""] * (NCOLS - 1))
        rows.append(["Option", "Term", "Commitment", "Est. Monthly Saving",
                     "Saving %", "ROI %", "", "", "", "", ""])
        for p in purchase_data.get("savings_plans", []):
            rows.append([f"Compute Savings Plan", p["term"].replace("_", " ").title(),
                         f"${p['hourly_commitment']:.4f}/hr",
                         round(p["monthly_savings"], 2),
                         round(p["savings_pct"] or 0, 1), round(p["roi_pct"] or 0, 1),
                         "", "", "", "", ""])
        for r in purchase_data.get("reservations", []):
            rows.append([r["service"][:40], "1 year", f"{len(r['details'])} types",
                         round(r["monthly_savings"], 2),
                         round(r["savings_pct"] or 0, 1), "", "", "", "", "", ""])
        if purchase_data.get("overlap_note"):
            rows.append([purchase_data["overlap_note"]] + [""] * (NCOLS - 1))
        rows.append([f"Non-overlapping total: "
                     f"${purchase_data['total_monthly_savings']:,.2f}/mo "
                     f"(compute via {purchase_data['compute_best_route']})"]
                    + [""] * (NCOLS - 1))

    rows.append([""] * NCOLS)
    header_row = len(rows)
    rows.append(HEADERS)
    data_start = len(rows)

    for item in sorted(items, key=lambda i: (i.get("service", ""), i.get("region", ""))):
        rows.append([
            item.get("service", ""),
            item.get("region", ""),
            item.get("id", ""),
            item.get("instance_type", ""),
            item.get("count", ""),
            item.get("scope", ""),
            item.get("offering_class", "") or item.get("commitment_hourly", ""),
            item.get("platform", ""),
            str(item.get("start", ""))[:10],
            str(item.get("end", ""))[:10],
            item.get("state", ""),
        ])
    data_end = len(rows)

    if not items:
        rows.append(["(none)"] + [""] * (NCOLS - 1))

    safe_update(ws, "A1", rows, value_input_option="RAW")

    R = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColor": TAB}, "fields": "tabColor"}},
        _merge(sid, 0, 1, 0, NCOLS), _rh(sid, 0, 1, 44),
        _c(sid, 0, 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True, size=15, halign="CENTER"),
        _merge(sid, 1, 2, 0, NCOLS), _rh(sid, 1, 2, 46),
        _c(sid, 1, 2, 0, NCOLS, bg=(AMBER if has else GREEN), fg=DKGREY,
           size=10, wrap=True),
        _c(sid, header_row, header_row + 1, 0, NCOLS,
           bg=NAVY, fg=WHITE, bold=True, size=10, halign="CENTER"),
        _rh(sid, header_row, header_row + 1, 30),
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": header_row + 1}},
            "fields": "gridProperties.frozenRowCount"}},
    ]
    if data_end > data_start:
        R.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": sid, "startRowIndex": header_row,
                      "endRowIndex": data_end,
                      "startColumnIndex": 0, "endColumnIndex": NCOLS}}}})

    apply_formats(spreadsheet, R)
    return ws
