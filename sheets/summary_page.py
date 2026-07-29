from collections import defaultdict
from .writer import apply_formats, safe_update

# ─── Tokens ───────────────────────────────────────────────────────────────────
NAVY    = {"red": 0.098, "green": 0.216, "blue": 0.412}
WHITE   = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
LTBLUE  = {"red": 0.937, "green": 0.953, "blue": 0.984}
DKGREY  = {"red": 0.200, "green": 0.200, "blue": 0.200}
MIDGREY = {"red": 0.650, "green": 0.650, "blue": 0.650}
SECTION = {"red": 0.910, "green": 0.926, "blue": 0.953}
GRN_TXT = {"red": 0.055, "green": 0.459, "blue": 0.188}
RED_TXT = {"red": 0.729, "green": 0.098, "blue": 0.098}
GRN_BG  = {"red": 0.851, "green": 0.961, "blue": 0.878}
RED_BG  = {"red": 0.988, "green": 0.875, "blue": 0.875}

# Each KPI card has its own accent colour
KPI_COLORS = [
    {"red": 0.851, "green": 0.918, "blue": 0.980},  # blue   – Monthly Spend
    {"red": 1.000, "green": 0.976, "blue": 0.796},  # yellow – P1 Savings
    {"red": 0.851, "green": 0.961, "blue": 0.878},  # green  – P2 Savings
    {"red": 0.957, "green": 0.878, "blue": 0.980},  # purple – Resources
]


# ─── Tiny helpers ─────────────────────────────────────────────────────────────
def _m(sid, r1, r2, c1, c2):
    return {"mergeCells": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "mergeType": "MERGE_ALL"}}

def _c(sid, r1, r2, c1, c2, bg=None, fg=None, bold=False,
       size=10, halign="LEFT", valign="MIDDLE", wrap=False, nfmt=None):
    tf = {"bold": bold, "fontSize": size}
    if fg: tf["foregroundColor"] = fg
    uf = {"textFormat": tf, "horizontalAlignment": halign, "verticalAlignment": valign}
    if bg:   uf["backgroundColor"] = bg
    if wrap: uf["wrapStrategy"] = "WRAP"
    if nfmt: uf["numberFormat"] = nfmt
    flds = "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment"
    if bg:   flds += ",backgroundColor"
    if wrap: flds += ",wrapStrategy"
    if nfmt: flds += ",numberFormat"
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "cell": {"userEnteredFormat": uf}, "fields": flds + ")"}}

def _rh(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def _cw(sid, c1, c2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def _bord(sid, r1, r2, c1, c2):
    line = {"style": "SOLID", "width": 1, "color": MIDGREY}
    return {"updateBorders": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "top": line, "bottom": line, "left": line, "right": line,
        "innerHorizontal": line, "innerVertical": line}}

def _cond(sid, r1, r2, c1, c2, ctype, val, fmt):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2}],
        "booleanRule": {"condition": {"type": ctype,
                                      "values": [{"userEnteredValue": str(val)}]},
                        "format": fmt}}, "index": 0}}


# ─── Main ─────────────────────────────────────────────────────────────────────
def build_summary_page(spreadsheet, account_id, monthly_costs, daily_costs,
                       diff, report_date,
                       total_p1=0, total_p2=0, total_resources=0,
                       confirmed_savings=None, estimated_savings=None,
                       cost_quality=None, gap_count=0):
    ws  = spreadsheet.sheet1
    ws.update_title("Summary")
    sid = ws.id

    # ── Pre-compute ───────────────────────────────────────────────────────────
    by_month = defaultdict(float)
    for r in monthly_costs:
        by_month[r["month"]] += r["cost"]
    sorted_months = sorted(by_month.items())
    current_total = sorted_months[-1][1] if sorted_months else 0
    last_month    = sorted_months[-1][0] if sorted_months else ""

    by_service = defaultdict(float)
    for r in monthly_costs:
        if r["month"] == last_month:
            by_service[r["service"]] += r["cost"]
    top_svcs  = sorted(by_service.items(), key=lambda x: x[1], reverse=True)[:15]
    svc_total = sum(c for _, c in top_svcs)

    # ── Column layout (A–H, 8 cols)
    # A=220 B=160 C=155 D=110 E=110 F=110 G=110 H=110
    # KPI cards → one card per column (A,B,C,D) — NO merging, no overlap risk
    # Data tables → cols A–D only

    # ── Row 0 : Title banner ──────────────────────────────────────────────────
    rows = []
    rows.append(["AWS Cost Optimization Report", "", "", "", "", "", "", ""])

    # ── Row 1 : Account / date / data quality ────────────────────────────────
    # The cost basis is stated up front. A reader must never have to guess
    # whether a saving is measured against real spend or against list price.
    basis_note = ""
    if cost_quality:
        if cost_quality.get("quality") == "cur":
            basis_note = "   |   Cost basis: actual billed cost (CUR)"
        elif cost_quality.get("cur_read_failed"):
            # Configured but not yet delivering — saying "no CUR configured"
            # here would send the reader off to set up something that exists.
            basis_note = ("   |   Cost basis: LIST PRICE (CUR configured but no "
                          "data yet) — excludes existing commitments and discounts")
        else:
            basis_note = ("   |   Cost basis: LIST PRICE (no CUR configured) — "
                          "excludes existing commitments and discounts")
    gap_note = f"   |   {gap_count} data gaps (see Data Gaps tab)" if gap_count else ""
    rows.append([f"Account: {account_id}   |   Report Date: {report_date}"
                 f"{basis_note}{gap_note}",
                 "", "", "", "", "", "", ""])

    # ── Row 2 : thin navy divider ─────────────────────────────────────────────
    rows.append([""] * 8)

    # ── Rows 3-4 : KPI cards ──────────────────────────────────────────────────
    # Confirmed and Estimated are never added together. A saving priced against
    # list price can be materially wrong for an account holding Reserved
    # Instances or Savings Plans, so it is reported separately rather than
    # folded into one confident-looking headline number.
    show_confidence = confirmed_savings is not None or estimated_savings is not None
    if show_confidence:
        kpi_labels = ["Monthly Spend", "Confirmed Savings/mo",
                      "Estimated Savings/mo", "Resources"]
        kpi_values = [current_total, confirmed_savings or 0,
                      estimated_savings or 0, total_resources]
    else:
        kpi_labels = ["Monthly Spend", "P1 Savings Opp.", "P2 Savings Opp.", "Resources"]
        kpi_values = [current_total, total_p1, total_p2, total_resources]

    rows.append(kpi_labels + ["", "", "", ""])
    rows.append(kpi_values + ["", "", "", ""])

    # ── Row 5 : spacer ────────────────────────────────────────────────────────
    rows.append([""] * 8)

    # ── Row 6 : Monthly section header ───────────────────────────────────────
    rows.append(["Monthly Spend Trend", "", "", "", "", "", "", ""])
    # ── Row 7 : Column headers ────────────────────────────────────────────────
    rows.append(["Month", "Total Cost (USD)", "vs Prev Month (USD)", "Change %",
                 "", "", "", ""])

    # ── Rows 8+ : Monthly data ─────────────────────────────────────────────── (month_start=8)
    month_start = len(rows)
    prev = None
    for month, cost in sorted_months:
        delta = round(cost - prev, 2) if prev is not None else ""
        dpct  = round((cost - prev) / prev * 100, 1) if prev is not None else ""
        rows.append([month, round(cost, 2), delta, dpct, "", "", "", ""])
        prev = cost
    month_end = len(rows)

    rows.append([""] * 8)   # spacer

    # ── Service breakdown ─────────────────────────────────────────────────────
    svc_hdr = len(rows)
    rows.append([f"Top Services — {last_month}", "", "", "", "", "", "", ""])
    rows.append(["Service", "Monthly Cost (USD)", "% of Total", "", "", "", "", ""])
    svc_start = len(rows)   # = 11 + N  (matches charts.py constant)
    for svc, cost in top_svcs:
        pct = round(cost / svc_total * 100, 1) if svc_total else ""
        rows.append([svc, round(cost, 2), pct, "", "", "", "", ""])
    svc_end = len(rows)

    rows.append([""] * 8)   # spacer

    # ── Changes section ───────────────────────────────────────────────────────
    chg_hdr = len(rows)
    if diff and diff.get("has_previous"):
        rows.append(["Changes Since Last Report", "", "", "", "", "", "", ""])
        rows.append(["Previous Report",   diff.get("previous_timestamp","")[:19], "", "", "", "", "", ""])
        rows.append(["Previous Total",    diff["total_cost_prev"],  "", "", "", "", "", ""])
        rows.append(["Current Total",     diff["total_cost_curr"],  "", "", "", "", "", ""])
        d    = diff["total_delta"]
        dpct = diff.get("total_delta_pct","")
        rows.append(["Net Change", d, f"{dpct}%" if dpct != "" else "", "", "", "", "", ""])
        rows.append(["Resources Added",   len(diff.get("added_resources",   [])), "", "", "", "", "", ""])
        rows.append(["Resources Removed", len(diff.get("removed_resources", [])), "", "", "", "", "", ""])
        rows.append(["Resources Changed", len(diff.get("changed_resources", [])), "", "", "", "", "", ""])
        chg_end = len(rows)
    else:
        rows.append(["No previous snapshot — this is the baseline run.", "", "", "", "", "", "", ""])
        chg_end = len(rows)

    # RAW, not USER_ENTERED: Sheets parsed month labels like "2026-03" into
    # dates, so the Month column exported as serial numbers (46082).
    # Numbers are still sent as numbers; only strings stop being coerced.
    safe_update(ws, "A1", rows, value_input_option="RAW")

    # ── Format requests ───────────────────────────────────────────────────────
    R = []

    # Column widths — A=220 B=160 C=155 D=110 E-H=110 each
    R += [_cw(sid,0,1,220), _cw(sid,1,2,160), _cw(sid,2,3,155),
          _cw(sid,3,4,110), _cw(sid,4,8,110)]

    # ── Row 0: Title banner (A–H merged) ─────────────────────────────────────
    R += [_m(sid,0,1,0,8), _rh(sid,0,1,50),
          _c(sid,0,1,0,8, bg=NAVY, fg=WHITE, bold=True, size=18,
             halign="CENTER", valign="MIDDLE")]

    # ── Row 1: Sub-header (A–H merged) ───────────────────────────────────────
    R += [_m(sid,1,2,0,8), _rh(sid,1,2,26),
          _c(sid,1,2,0,8, bg=LTBLUE, fg=DKGREY, size=10, halign="CENTER")]

    # ── Row 2: Navy hairline divider ──────────────────────────────────────────
    R += [_rh(sid,2,3,6), _c(sid,2,3,0,8, bg=NAVY)]

    # ── Rows 3–4: KPI cards (one card per column, no merging) ────────────────
    R += [_rh(sid,3,4,26), _rh(sid,4,5,50)]
    NFMT_COST = {"type":"NUMBER","pattern":'"$"#,##0.00'}
    NFMT_INT  = {"type":"NUMBER","pattern":"#,##0"}
    kpi_nfmts   = [NFMT_COST, NFMT_COST, NFMT_COST, NFMT_INT]
    for i, (bg, lbl, nfmt) in enumerate(zip(KPI_COLORS, kpi_labels, kpi_nfmts)):
        # Label
        R.append(_c(sid,3,4,i,i+1, bg=bg, fg=DKGREY, bold=True, size=9,
                    halign="CENTER", valign="MIDDLE", wrap=True))
        # Value
        R.append(_c(sid,4,5,i,i+1, bg=bg, fg=NAVY, bold=True, size=22,
                    halign="CENTER", valign="MIDDLE", nfmt=nfmt))
    R.append(_bord(sid,3,5,0,4))

    # ── Row 5: spacer ─────────────────────────────────────────────────────────
    R.append(_rh(sid,5,6,12))

    # ── Row 6: Monthly section header (A–D merged) ───────────────────────────
    R += [_m(sid,6,7,0,4), _rh(sid,6,7,30),
          _c(sid,6,7,0,4, bg=SECTION, fg=NAVY, bold=True, size=11)]

    # ── Row 7: Table column headers ───────────────────────────────────────────
    R += [_rh(sid,7,8,28),
          _c(sid,7,8,0,4, bg=NAVY, fg=WHITE, bold=True, size=10, halign="CENTER")]

    # Monthly data: alternating bands + number formats
    for i in range(month_start, month_end):
        R.append(_c(sid,i,i+1,0,4, bg=(WHITE if (i-month_start)%2==0 else LTBLUE)))
    R.append(_c(sid,month_start,month_end,1,2, nfmt=NFMT_COST, halign="RIGHT"))
    R.append(_c(sid,month_start,month_end,2,3,
                nfmt={"type":"NUMBER","pattern":'"$"+#,##0.00;"-$"#,##0.00'},
                halign="RIGHT"))
    R.append(_c(sid,month_start,month_end,3,4,
                nfmt={"type":"NUMBER","pattern":"0.0"}, halign="RIGHT"))
    R.append(_bord(sid,7,month_end,0,4))

    # Delta conditional colouring
    if month_end > month_start:
        R.append(_cond(sid,month_start,month_end,2,4,"NUMBER_GREATER",0,
                       {"textFormat":{"foregroundColor":RED_TXT,"bold":True}}))
        R.append(_cond(sid,month_start,month_end,2,4,"NUMBER_LESS",0,
                       {"textFormat":{"foregroundColor":GRN_TXT,"bold":True}}))

    # ── Service breakdown ─────────────────────────────────────────────────────
    R += [_m(sid,svc_hdr,svc_hdr+1,0,3), _rh(sid,svc_hdr,svc_hdr+1,30),
          _c(sid,svc_hdr,svc_hdr+1,0,3, bg=SECTION, fg=NAVY, bold=True, size=11),
          _rh(sid,svc_hdr+1,svc_hdr+2,28),
          _c(sid,svc_hdr+1,svc_hdr+2,0,3, bg=NAVY, fg=WHITE, bold=True, size=10,
             halign="CENTER")]
    for i in range(svc_start, svc_end):
        R.append(_c(sid,i,i+1,0,3, bg=(WHITE if (i-svc_start)%2==0 else LTBLUE)))
    R.append(_c(sid,svc_start,svc_end,1,2, nfmt=NFMT_COST, halign="RIGHT"))
    R.append(_c(sid,svc_start,svc_end,2,3,
                nfmt={"type":"NUMBER","pattern":"0.0"}, halign="RIGHT"))
    R.append(_bord(sid,svc_hdr+1,svc_end,0,3))

    # ── Changes section ───────────────────────────────────────────────────────
    R += [_m(sid,chg_hdr,chg_hdr+1,0,3), _rh(sid,chg_hdr,chg_hdr+1,30),
          _c(sid,chg_hdr,chg_hdr+1,0,3, bg=SECTION, fg=NAVY, bold=True, size=11)]
    if diff and diff.get("has_previous"):
        cd = chg_hdr + 1
        for i in range(cd, chg_end):
            R.append(_c(sid,i,i+1,0,3, bg=(WHITE if (i-cd)%2==0 else LTBLUE)))
        for ri in [cd+1, cd+2]:
            R.append(_c(sid,ri,ri+1,1,2, nfmt=NFMT_COST, halign="RIGHT"))
        nc = cd + 3
        R.append(_c(sid,nc,nc+1,1,2,
                    nfmt={"type":"NUMBER","pattern":'"$"+#,##0.00;"-$"#,##0.00'},
                    halign="RIGHT"))
        d2 = diff["total_delta"]
        R.append(_c(sid,nc,nc+1,0,3, bg=GRN_BG if d2<=0 else RED_BG))
        R.append(_bord(sid,cd,chg_end,0,3))

    apply_formats(spreadsheet, R)
    return ws
