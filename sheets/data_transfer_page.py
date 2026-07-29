"""
"Data Transfer" tab.

Data transfer has no resource to discover, so an inventory-driven scan misses
it entirely — it simply disappeared into the "EC2 - Other" line item. Every
figure here comes straight from Cost Explorer usage types, i.e. actual billed
money, which makes it the most trustworthy number in the report.

Guidance is offered per category, but no saving is claimed: how much of a
transfer bill is avoidable depends on architecture we cannot see.
"""

from .writer import safe_add_worksheet, safe_update, apply_formats

NAVY   = {"red": 0.098, "green": 0.216, "blue": 0.412}
WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
LTBLUE = {"red": 0.937, "green": 0.953, "blue": 0.984}
TAB    = {"red": 0.514, "green": 0.137, "blue": 0.718}

HEADERS = ["Category", "Monthly Cost (USD)", "How this is usually reduced",
           "Usage types included"]
NCOLS = len(HEADERS)


def _c(sid, r1, r2, c1, c2, bg=None, fg=None, bold=False, size=10,
       halign="LEFT", valign="MIDDLE", wrap=False, nfmt=None):
    tf = {"bold": bold, "fontSize": size}
    if fg:
        tf["foregroundColor"] = fg
    uf = {"textFormat": tf, "horizontalAlignment": halign, "verticalAlignment": valign}
    if bg:
        uf["backgroundColor"] = bg
    if wrap:
        uf["wrapStrategy"] = "WRAP"
    if nfmt:
        uf["numberFormat"] = nfmt
    fields = "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment"
    for flag, name in ((bg, ",backgroundColor"), (wrap, ",wrapStrategy"),
                       (nfmt, ",numberFormat")):
        if flag:
            fields += name
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "cell": {"userEnteredFormat": uf}, "fields": fields + ")"}}


def _merge(sid, r1, r2, c1, c2):
    return {"mergeCells": {"range": {"sheetId": sid, "startRowIndex": r1,
            "endRowIndex": r2, "startColumnIndex": c1, "endColumnIndex": c2},
            "mergeType": "MERGE_ALL"}}


def _rh(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _cw(sid, c1, c2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def build_data_transfer_page(spreadsheet, transfer):
    if not transfer or not transfer.get("available"):
        return None

    buckets = transfer.get("buckets") or []
    top = transfer.get("top_usage_types") or []
    ws = safe_add_worksheet(spreadsheet, "Data Transfer",
                            rows=len(buckets) + len(top) + 20, cols=NCOLS + 1)
    sid = ws.id

    rows = [["Data Transfer Spend"] + [""] * (NCOLS - 1)]
    rows.append([
        f"${transfer.get('total_usd', 0):,.2f}/month for billing period "
        f"{transfer.get('month', '')}. Taken directly from Cost Explorer usage "
        f"types — this is actual billed cost, not a list-price estimate. No "
        f"saving is claimed: how much is avoidable depends on the architecture."
    ] + [""] * (NCOLS - 1))
    rows.append([""] * NCOLS)

    header_row = len(rows)
    rows.append(HEADERS)
    data_start = len(rows)
    for b in buckets:
        rows.append([b["bucket"], b["cost_usd"], b.get("guidance", ""),
                     b.get("usage_types", "")])
    data_end = len(rows)

    rows.append([""] * NCOLS)
    detail_hdr = len(rows)
    rows.append(["Usage type", "Monthly Cost (USD)", "", ""])
    detail_start = len(rows)
    for u in top:
        rows.append([u["usage_type"], u["cost_usd"], "", ""])
    detail_end = len(rows)

    safe_update(ws, "A1", rows, value_input_option="RAW")

    money = {"type": "NUMBER", "pattern": '"$"#,##0.00'}
    R = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColor": TAB}, "fields": "tabColor"}},
        _cw(sid, 0, 1, 240), _cw(sid, 1, 2, 150), _cw(sid, 2, 3, 420), _cw(sid, 3, 4, 380),
        _merge(sid, 0, 1, 0, NCOLS), _rh(sid, 0, 1, 44),
        _c(sid, 0, 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True, size=15, halign="CENTER"),
        _merge(sid, 1, 2, 0, NCOLS), _rh(sid, 1, 2, 44),
        _c(sid, 1, 2, 0, NCOLS, bg=LTBLUE, size=10, wrap=True),
        _c(sid, header_row, header_row + 1, 0, NCOLS, bg=NAVY, fg=WHITE,
           bold=True, size=10, halign="CENTER"),
        _c(sid, detail_hdr, detail_hdr + 1, 0, NCOLS, bg=NAVY, fg=WHITE,
           bold=True, size=10, halign="CENTER"),
    ]
    if data_end > data_start:
        R.append(_c(sid, data_start, data_end, 1, 2, nfmt=money, halign="RIGHT"))
        R.append(_c(sid, data_start, data_end, 2, NCOLS, wrap=True, valign="TOP"))
        R.append(_rh(sid, data_start, data_end, 42))
    if detail_end > detail_start:
        R.append(_c(sid, detail_start, detail_end, 1, 2, nfmt=money, halign="RIGHT"))

    apply_formats(spreadsheet, R)
    return ws
