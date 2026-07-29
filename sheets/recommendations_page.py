from analysis import provenance as prov
from .writer import safe_add_worksheet, safe_update, apply_formats


def _collapse_aggregated(buckets):
    """
    Turn many identical findings into one summary row carrying measured totals.

    A rule like "set a log retention policy" fires once per log group. In a real
    account that produced 26 rows, and S3 lifecycle another 15 — 41 of 94 rows,
    all unpriced, burying the ~20 rows that carry actual money. The per-resource
    detail is still on the service tab; this is purely about what leads the
    client's eye.
    """
    rows = []
    for (phase, service, rule_name), items in buckets.items():
        count = len(items)
        first = items[0]["rec"]
        total_cost = sum(c for c in (i["cost"] for i in items) if c)
        ids = [i["id"] for i in items]

        evidence = [f"{count} affected resource(s)"]
        if total_cost:
            evidence.append(f"combined current spend ${total_cost:,.2f}/mo")
        evidence.append("per-resource detail is on the service tab")

        rows.append({
            "phase": phase,
            "service": service,
            "name": f"{count} x {service}",
            "id": ", ".join(ids[:6]) + (f" (+{count - 6} more)" if count > 6 else ""),
            "region": ", ".join(sorted({i["region"] for i in items if i["region"]})[:4]),
            "action": f"{count} resources: {first.get('action', rule_name)}",
            "risk": first.get("risk", ""),
            # Aggregated rules are unpriced by construction, so there is no
            # total to claim — only the spend currently flowing through them.
            "saving_usd": None,
            "confidence": first.get("confidence", ""),
            "evidence": " | ".join(evidence),
            "basis": "",
            "caveats": " | ".join(first.get("caveats", [])),
            "validation": " | ".join(first.get("validation_steps", [])),
            "blockers": "",
            "assumptions": "",
        })
    return rows

# ─── Tokens ───────────────────────────────────────────────────────────────────
NAVY    = {"red": 0.098, "green": 0.216, "blue": 0.412}
WHITE   = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
DKGREY  = {"red": 0.200, "green": 0.200, "blue": 0.200}
MIDGREY = {"red": 0.650, "green": 0.650, "blue": 0.650}

# Phase row tints
LCYC_BG  = {"red": 0.996, "green": 0.937, "blue": 0.878}  # light orange  – Lifecycle
PH1_BG   = {"red": 1.000, "green": 0.990, "blue": 0.878}  # light yellow  – Phase 1
PH2_BG   = {"red": 0.906, "green": 0.929, "blue": 0.980}  # light blue    – Phase 2
PH1_BG2  = {"red": 1.000, "green": 0.996, "blue": 0.937}  # alt Phase 1
PH2_BG2  = {"red": 0.945, "green": 0.957, "blue": 0.992}  # alt Phase 2
LCYC_BG2 = {"red": 1.000, "green": 0.961, "blue": 0.925}  # alt Lifecycle

# Phase header band colours
HDR_LCYC = {"red": 0.820, "green": 0.431, "blue": 0.118}  # burnt orange
HDR_PH1  = {"red": 0.886, "green": 0.647, "blue": 0.000}  # amber
HDR_PH2  = {"red": 0.129, "green": 0.302, "blue": 0.608}  # royal blue

# Risk badge colours
RISK_CLR = {
    "Low":      {"red": 0.055, "green": 0.459, "blue": 0.188},  # dark green
    "Medium":   {"red": 0.800, "green": 0.478, "blue": 0.000},  # orange
    "High":     {"red": 0.729, "green": 0.098, "blue": 0.098},  # red
    "Critical": {"red": 0.545, "green": 0.000, "blue": 0.000},
    "N/A":      {"red": 0.600, "green": 0.600, "blue": 0.600},
}

# KPI card accent colours
KPI_LCYC = {"red": 0.996, "green": 0.929, "blue": 0.863}  # warm orange
KPI_P1   = {"red": 1.000, "green": 0.976, "blue": 0.796}  # gold
KPI_P2   = {"red": 0.851, "green": 0.918, "blue": 0.980}  # soft blue
KPI_SAV  = {"red": 0.851, "green": 0.961, "blue": 0.878}  # soft green

GOLD_TAB = {"red": 0.925, "green": 0.718, "blue": 0.000}  # tab colour


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _m(sid, r1, r2, c1, c2):
    return {"mergeCells": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "mergeType": "MERGE_ALL"}}

def _c(sid, r1, r2, c1, c2, bg=None, fg=None, bold=False, size=10,
       halign="LEFT", valign="MIDDLE", wrap=False, nfmt=None, italic=False):
    tf = {"bold": bold, "fontSize": size, "italic": italic}
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

def _bord(sid, r1, r2, c1, c2, color=None):
    line = {"style": "SOLID", "width": 1, "color": color or MIDGREY}
    return {"updateBorders": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "top": line, "bottom": line, "left": line, "right": line,
        "innerHorizontal": {"style":"SOLID","width":1,"color":{"red":0.9,"green":0.9,"blue":0.9}},
        "innerVertical":   line}}

def _cond_text(sid, r1, r2, c1, c2, val, fmt):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2}],
        "booleanRule": {"condition": {"type": "TEXT_CONTAINS",
                                      "values": [{"userEnteredValue": val}]},
                        "format": fmt}}, "index": 0}}

def _cond_num(sid, r1, r2, c1, c2, op, val, fmt):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2}],
        "booleanRule": {"condition": {"type": op,
                                      "values": [{"userEnteredValue": str(val)}]},
                        "format": fmt}}, "index": 0}}


# ─── Flatten all recommendations into one flat list ──────────────────────────
def _flatten(resources):
    rows = []
    aggregated = {}          # (phase, service, rule) -> [findings]
    p1_total = p2_total = lc_count = p1_count = p2_count = 0

    for service, items in resources.items():
        for item in items:
            recs = item.get("recommendations", {})
            name = item.get("name", item["id"])
            iid  = item["id"]
            reg  = item.get("region", "")

            for w in recs.get("lifecycle_warnings", []):
                rows.append({"phase": "Lifecycle", "service": service,
                              "name": name, "id": iid, "region": reg,
                              "action": w, "risk": "High",
                              "saving_usd": None, "confidence": "Unpriced",
                              "evidence": "", "basis": "", "caveats": "",
                              "validation": "Upgrade immediately — check AWS EOL calendar",
                              "blockers": "", "assumptions": ""})
                lc_count += 1

            for phase_key, phase_label in (("phase1", "Phase 1"), ("phase2", "Phase 2")):
                for rec in recs.get(phase_key, []):
                    if not isinstance(rec, dict):
                        continue
                    saving = rec.get("saving_usd")
                    # Informational "cannot evaluate" rows stay out of the
                    # action list; they are reported on the Data Gaps tab.
                    if rec.get("risk") == "N/A" and not saving:
                        continue
                    # Repeated identical findings are collapsed after the loop.
                    if rec.get("aggregate"):
                        key = (phase_label, service, rec.get("rule", rec.get("action", "")))
                        aggregated.setdefault(key, []).append({
                            "rec": rec, "id": iid, "region": reg,
                            "cost": prov.cost_of(item),
                        })
                        if phase_key == "phase1":
                            p1_count += 1
                        else:
                            p2_count += 1
                        continue

                    basis = rec.get("saving_basis") or {}
                    rows.append({
                        "phase": phase_label, "service": service,
                        "name": name, "id": iid, "region": reg,
                        "action": rec.get("action", ""),
                        "risk": rec.get("risk", ""),
                        # None stays None so the cell renders blank, not $0.00
                        "saving_usd": saving,
                        "confidence": rec.get("confidence", ""),
                        "evidence": " | ".join(rec.get("evidence", [])),
                        "basis": basis.get("description", ""),
                        "caveats": " | ".join(rec.get("caveats", [])),
                        "validation": " | ".join(rec.get("validation_steps", [])),
                        "blockers": " | ".join(rec.get("blockers", [])),
                        "assumptions": " | ".join(rec.get("assumptions", [])),
                    })
                    if saving:
                        if phase_key == "phase1":
                            p1_total += saving
                            p1_count += 1
                        else:
                            p2_total += saving
                            p2_count += 1
                    elif phase_key == "phase1":
                        p1_count += 1
                    else:
                        p2_count += 1

    rows.extend(_collapse_aggregated(aggregated))

    # Lifecycle first, then by phase, and within a phase by saving descending so
    # the rows that carry money lead. Unpriced actions sort last within a phase
    # rather than being scattered through it.
    order = {"Lifecycle": 0, "Phase 1": 1, "Phase 2": 2}
    rows.sort(key=lambda x: (order.get(x["phase"], 9),
                             0 if x["saving_usd"] else 1,
                             -(x["saving_usd"] or 0)))

    return rows, lc_count, p1_count, p2_count, p1_total, p2_total


# ─── Page builder ─────────────────────────────────────────────────────────────
def build_recommendations_page(spreadsheet, resources, all_recs):
    data, lc_cnt, p1_cnt, p2_cnt, p1_tot, p2_tot = _flatten(resources)

    NCOLS = 14
    ws = safe_add_worksheet(spreadsheet, "Recommendations",
                            rows=len(data) + 15, cols=NCOLS + 2)
    sid = ws.id

    sheet_rows = []

    # ── Row 0 : Title banner ──────────────────────────────────────────────────
    sheet_rows.append(["Cost Optimization Recommendations"] + [""] * (NCOLS - 1))

    # ── Row 1 : Sub-title ─────────────────────────────────────────────────────
    total = p1_tot + p2_tot
    sheet_rows.append([
        f"Phase 1: ${p1_tot:,.2f}/mo ({p1_cnt} actions)   |   "
        f"Phase 2: ${p2_tot:,.2f}/mo ({p2_cnt} actions)   |   "
        f"Lifecycle warnings: {lc_cnt}   |   "
        f"See the Confidence column — Estimated figures use list price"
    ] + [""] * (NCOLS - 1))

    # ── Row 2 : Spacer ────────────────────────────────────────────────────────
    sheet_rows.append([""] * NCOLS)

    # ── Rows 3–4 : KPI cards (one card per column) ────────────────────────────
    sheet_rows.append(["Lifecycle Warnings", "Phase 1 Items", "Phase 2 Items",
                       "Total Savings/mo", "", "", "", "", "", "", ""])
    sheet_rows.append([lc_cnt, p1_cnt, p2_cnt, p1_tot + p2_tot,
                       "", "", "", "", "", "", ""])

    # ── Row 5 : Spacer ────────────────────────────────────────────────────────
    sheet_rows.append([""] * NCOLS)

    # ── Row 6 : Table header ──────────────────────────────────────────────────
    sheet_rows.append([
        "Phase", "Service", "Resource", "ID", "Region",
        "Action", "Risk", "Savings/mo (USD)", "Confidence",
        "Basis (how the saving was priced)", "Evidence",
        "Caveats", "Validation Steps", "Blockers / Assumptions",
    ])

    # ── Rows 7+ : Data ────────────────────────────────────────────────────────
    data_start = len(sheet_rows)  # = 7
    for rec in data:
        sheet_rows.append([
            rec["phase"],
            rec["service"],
            rec["name"],
            rec["id"],
            rec["region"],
            rec["action"],
            rec["risk"],
            "" if rec["saving_usd"] is None else rec["saving_usd"],
            rec.get("confidence", ""),
            rec.get("basis", ""),
            rec.get("evidence", ""),
            rec["caveats"],
            rec["validation"],
            (rec["blockers"] + ("  //  " + rec["assumptions"] if rec["assumptions"] else "")).strip(),
        ])
    data_end = len(sheet_rows)

    safe_update(ws, "A1", sheet_rows, value_input_option="USER_ENTERED")

    # ── Formatting ────────────────────────────────────────────────────────────
    R = []

    # Tab colour — gold
    R.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "tabColor": GOLD_TAB},
        "fields": "tabColor"}})

    # Column widths
    widths = [105, 95, 155, 130, 95, 285, 82, 122, 95, 260, 240, 255, 255, 230]
    for i, w in enumerate(widths):
        R.append(_cw(sid, i, i+1, w))

    # ── Row 0: Title banner ───────────────────────────────────────────────────
    R += [_m(sid,0,1,0,NCOLS), _rh(sid,0,1,48),
          _c(sid,0,1,0,NCOLS, bg=NAVY, fg=WHITE, bold=True, size=16, halign="CENTER")]

    # ── Row 1: Sub-title ──────────────────────────────────────────────────────
    R += [_m(sid,1,2,0,NCOLS), _rh(sid,1,2,26),
          _c(sid,1,2,0,NCOLS,
             bg={"red":0.949,"green":0.957,"blue":0.976},
             fg=NAVY, size=10, halign="CENTER")]

    # ── Row 2: Spacer ─────────────────────────────────────────────────────────
    R.append(_rh(sid,2,3,8))

    # ── KPI cards (rows 3-4) ──────────────────────────────────────────────────
    R += [_rh(sid,3,4,26), _rh(sid,4,5,46)]
    kpi_colors = [KPI_LCYC, KPI_P1, KPI_P2, KPI_SAV]
    kpi_nfmt   = [{"type":"NUMBER","pattern":"#,##0"},
                  {"type":"NUMBER","pattern":"#,##0"},
                  {"type":"NUMBER","pattern":"#,##0"},
                  {"type":"NUMBER","pattern":'"$"#,##0.00'}]
    for i, (bg, nfmt) in enumerate(zip(kpi_colors[:4], kpi_nfmt)):
        R.append(_c(sid,3,4,i,i+1, bg=bg, fg=DKGREY, bold=True, size=9,
                    halign="CENTER", wrap=True))
        R.append(_c(sid,4,5,i,i+1, bg=bg, fg=NAVY, bold=True, size=22,
                    halign="CENTER", valign="MIDDLE", nfmt=nfmt))
    R.append(_bord(sid,3,5,0,4))

    # ── Row 5: Spacer ─────────────────────────────────────────────────────────
    R.append(_rh(sid,5,6,10))

    # ── Row 6: Table header ───────────────────────────────────────────────────
    R += [_rh(sid,6,7,34),
          _c(sid,6,7,0,NCOLS, bg=NAVY, fg=WHITE, bold=True, size=10,
             halign="CENTER", valign="MIDDLE")]

    # Freeze row 6 as the interactive filter row
    R.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 7}},
        "fields": "gridProperties.frozenRowCount"}})

    # Filter dropdowns on the table header (row 6) and data
    if data_end > data_start:
        R.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": sid,
                      "startRowIndex": 6, "endRowIndex": data_end,
                      "startColumnIndex": 0, "endColumnIndex": NCOLS}}}})

    # ── Data rows: base formatting ────────────────────────────────────────────
    if data_end > data_start:
        R.append(_rh(sid, data_start, data_end, 52))   # taller rows — room for wrapped text
        # All data cells: wrap text + vertical top alignment
        R.append(_c(sid, data_start, data_end, 0, NCOLS,
                    valign="TOP", wrap=True))
        # Action column: slightly larger font, bold
        R.append(_c(sid, data_start, data_end, 5, 6,
                    bold=True, size=10, wrap=True, valign="TOP"))
        # Savings column: right-aligned, currency
        R.append(_c(sid, data_start, data_end, 7, 8,
                    nfmt={"type":"NUMBER","pattern":'"$"#,##0.00'},
                    halign="RIGHT", bold=True, valign="TOP"))
        # ID column: smaller font, grey
        R.append(_c(sid, data_start, data_end, 3, 4,
                    fg={"red":0.45,"green":0.45,"blue":0.45}, size=9, valign="TOP"))

    # ── Phase-based row background colour ─────────────────────────────────────
    for i, rec in enumerate(data):
        r = data_start + i
        if rec["phase"] == "Lifecycle":
            bg = LCYC_BG if i % 2 == 0 else LCYC_BG2
        elif rec["phase"] == "Phase 1":
            bg = PH1_BG if i % 2 == 0 else PH1_BG2
        else:
            bg = PH2_BG if i % 2 == 0 else PH2_BG2
        R.append(_c(sid, r, r+1, 0, NCOLS, bg=bg))

    # ── Conditional: Phase column — bold coloured badge ───────────────────────
    if data_end > data_start:
        for val, fg in [("Lifecycle", {"red":0.72,"green":0.25,"blue":0.05}),
                         ("Phase 1",   {"red":0.65,"green":0.45,"blue":0.00}),
                         ("Phase 2",   {"red":0.13,"green":0.27,"blue":0.53})]:
            R.append(_cond_text(sid, data_start, data_end, 0, 1, val,
                                {"textFormat": {"foregroundColor": fg, "bold": True}}))

    # ── Conditional: Risk column ──────────────────────────────────────────────
    if data_end > data_start:
        for risk_val, fg in RISK_CLR.items():
            R.append(_cond_text(sid, data_start, data_end, 6, 7, risk_val,
                                {"textFormat": {"foregroundColor": fg, "bold": True}}))

    # ── Conditional: Savings column — green highlight for big wins ────────────
    if data_end > data_start:
        GRN_BG = {"red":0.851,"green":0.961,"blue":0.878}
        YLW_BG = {"red":1.000,"green":0.976,"blue":0.796}
        R.append(_cond_num(sid, data_start, data_end, 7, 8,
                           "NUMBER_GREATER_THAN_EQ", 500,
                           {"backgroundColor": GRN_BG}))
        R.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": sid, "startRowIndex": data_start,
                        "endRowIndex": data_end, "startColumnIndex": 7, "endColumnIndex": 8}],
            "booleanRule": {"condition": {"type": "NUMBER_BETWEEN",
                                          "values": [{"userEnteredValue": "1"},
                                                     {"userEnteredValue": "499"}]},
                            "format": {"backgroundColor": YLW_BG}}},
            "index": 0}})

    # ── Phase section dividers (thick border between phase changes) ───────────
    if data_end > data_start:
        prev_phase = None
        for i, rec in enumerate(data):
            if prev_phase and rec["phase"] != prev_phase:
                r = data_start + i
                thick = {"style": "SOLID_MEDIUM", "width": 2, "color": NAVY}
                R.append({"updateBorders": {
                    "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1,
                              "startColumnIndex": 0, "endColumnIndex": NCOLS},
                    "top": thick}})
            prev_phase = rec["phase"]

    # Overall table border
    if data_end > data_start:
        R.append(_bord(sid, 6, data_end, 0, NCOLS))

    apply_formats(spreadsheet, R)
    return ws
