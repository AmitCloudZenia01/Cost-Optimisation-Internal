"""
"Data Gaps" tab — everything the report could not measure or price, and how to
fix it.

This is a deliberate output, not a debug dump. A blank cost cell is only honest
if the reader can find out why it is blank; this tab is that answer. It also
doubles as a scoping list: each row is something the client can enable to make
the next report stronger.
"""

from collections import Counter

from .writer import safe_add_worksheet, safe_update, apply_formats

NAVY   = {"red": 0.098, "green": 0.216, "blue": 0.412}
WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
LTBLUE = {"red": 0.937, "green": 0.953, "blue": 0.984}
AMBER  = {"red": 1.000, "green": 0.949, "blue": 0.800}
DKGREY = {"red": 0.200, "green": 0.200, "blue": 0.200}
GREEN  = {"red": 0.851, "green": 0.961, "blue": 0.878}
TAB    = {"red": 0.850, "green": 0.500, "blue": 0.100}

NCOLS = 7


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


def _row_height(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _col_width(sid, c1, c2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def build_data_gaps_page(spreadsheet, gap_list, cost_quality=None,
                         pricing_provider="", unresolved_prices=None):
    ws = safe_add_worksheet(spreadsheet, "Data Gaps",
                            rows=len(gap_list) + 30, cols=NCOLS + 1)
    sid = ws.id
    rows = []

    rows.append(["Data Gaps — what this report could not measure"] + [""] * (NCOLS - 1))
    rows.append(["Every blank cost or omitted saving in this report is listed "
                 "here with its reason. Nothing was estimated to fill a gap."]
                + [""] * (NCOLS - 1))
    rows.append([""] * NCOLS)

    # Data-source status block
    rows.append(["Cost data source", "", "", "", "", "", ""])
    if cost_quality:
        rows.append(["Status", cost_quality.get("message", ""), "", "", "", "", ""])
        rows.append(["Accuracy",
                     "Actual billed cost (CUR)" if cost_quality.get("quality") == "cur"
                     else "List price estimates (Cost Explorer) — excludes existing "
                          "Reserved Instances, Savings Plans, credits and discounts",
                     "", "", "", "", ""])
    rows.append(["Pricing source", pricing_provider or "unknown", "", "", "", "", ""])
    rows.append([""] * NCOLS)

    counts = Counter(g["category"] for g in gap_list)
    rows.append(["Summary by category", "", "", "", "", "", ""])
    rows.append(["Category", "Gaps", "", "", "", "", ""])
    summary_start = len(rows)
    for category, count in counts.most_common():
        rows.append([category, count, "", "", "", "", ""])
    if not counts:
        rows.append(["No gaps — every resource was measured and priced", "", "", "", "", "", ""])
    summary_end = len(rows)
    rows.append([""] * NCOLS)

    rows.append(["Detail", "", "", "", "", "", ""])
    header_row = len(rows)
    rows.append(["Category", "What", "Why", "How to fix",
                 "Impact", "Resource type", "Region"])
    detail_start = len(rows)
    for gap in sorted(gap_list, key=lambda g: (g["category"], g["what"])):
        rows.append([
            gap.get("category", ""),
            gap.get("what", ""),
            gap.get("why", ""),
            gap.get("how_to_fix", ""),
            gap.get("impact", ""),
            gap.get("resource_type", ""),
            gap.get("region", ""),
        ])
    detail_end = len(rows)

    if unresolved_prices:
        rows.append([""] * NCOLS)
        rows.append(["Prices that could not be resolved", "", "", "", "", "", ""])
        for item in unresolved_prices:
            rows.append(["Pricing", item, "No published rate matched this SKU "
                         "for the region.", "Grant pricing:GetProducts, or check "
                         "whether the service is offered in that region.",
                         "Affected resources show no cost.", "", ""])

    safe_update(ws, "A1", rows, value_input_option="USER_ENTERED")

    R = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "tabColor": TAB}, "fields": "tabColor"}},
        _col_width(sid, 0, 1, 130), _col_width(sid, 1, 2, 230),
        _col_width(sid, 2, 3, 330), _col_width(sid, 3, 4, 330),
        _col_width(sid, 4, 5, 240), _col_width(sid, 5, 6, 120),
        _col_width(sid, 6, 7, 110),
        _merge(sid, 0, 1, 0, NCOLS), _row_height(sid, 0, 1, 44),
        _c(sid, 0, 1, 0, NCOLS, bg=NAVY, fg=WHITE, bold=True, size=15, halign="CENTER"),
        _merge(sid, 1, 2, 0, NCOLS), _row_height(sid, 1, 2, 32),
        _c(sid, 1, 2, 0, NCOLS, bg=LTBLUE, fg=DKGREY, size=10, halign="CENTER", wrap=True),
        _c(sid, 3, 4, 0, NCOLS, bg=LTBLUE, fg=NAVY, bold=True, size=11),
        _c(sid, header_row, header_row + 1, 0, NCOLS,
           bg=NAVY, fg=WHITE, bold=True, size=10, halign="CENTER"),
        _row_height(sid, header_row, header_row + 1, 30),
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": header_row + 1}},
            "fields": "gridProperties.frozenRowCount"}},
    ]

    if summary_end > summary_start:
        R.append(_c(sid, summary_start, summary_end, 0, 2, bg=AMBER))
    if detail_end > detail_start:
        R.append(_c(sid, detail_start, detail_end, 0, NCOLS, wrap=True, valign="TOP"))
        R.append(_row_height(sid, detail_start, detail_end, 46))
        R.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": sid, "startRowIndex": header_row,
                      "endRowIndex": detail_end,
                      "startColumnIndex": 0, "endColumnIndex": NCOLS}}}})
    else:
        R.append(_c(sid, detail_start, detail_start + 1, 0, NCOLS, bg=GREEN))

    apply_formats(spreadsheet, R)
    return ws
