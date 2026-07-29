from collections import defaultdict


def add_service_pie_chart(spreadsheet, summary_ws, monthly_costs):
    """
    Add a pie chart (by service) and a column chart (monthly trend) to the summary page.
    Both charts are anchored to column 9 (col J) so they never overlap with the
    8-column content area (cols A–H).
    """
    sheet_id = summary_ws.id

    if not monthly_costs:
        return

    by_month = defaultdict(float)
    for row in monthly_costs:
        by_month[row["month"]] += row["cost"]
    sorted_months = sorted(by_month.items())
    if not sorted_months:
        return

    last_month = sorted_months[-1][0]
    by_service = defaultdict(float)
    for row in monthly_costs:
        if row["month"] == last_month:
            by_service[row["service"]] += row["cost"]

    top_services = sorted(by_service.items(), key=lambda x: x[1], reverse=True)[:10]
    if not top_services:
        return

    month_count = len(sorted_months)

    # ── New summary layout row offsets ────────────────────────────────────────
    # Row 0  : Title banner
    # Row 1  : Sub-header / account info
    # Row 2  : Spacer
    # Row 3  : KPI labels
    # Row 4  : KPI values
    # Row 5  : Spacer
    # Row 6  : Monthly section header
    # Row 7  : Monthly column headers
    # Row 8  : Monthly data start   ← trend chart source
    # Row 8+N: Spacer
    # Row 9+N: Service section header
    # Row 10+N: Service column headers
    # Row 11+N: Service data start  ← pie chart source
    monthly_data_start  = 8
    service_data_start  = 11 + month_count   # matches build_summary_page layout

    # ── Both charts sit to the RIGHT of the content (column 9 = col J) ───────
    CHART_COL   = 9    # safely outside the 8-column layout (A-H)
    CHART_W     = 460
    CHART_H     = 300

    pie_request = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": f"Cost by Service — {last_month}",
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "threeDimensional": False,
                        "domain": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": service_data_start,
                            "endRowIndex": service_data_start + len(top_services),
                            "startColumnIndex": 0, "endColumnIndex": 1,
                        }]}},
                        "series": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": service_data_start,
                            "endRowIndex": service_data_start + len(top_services),
                            "startColumnIndex": 1, "endColumnIndex": 2,
                        }]}},
                    },
                },
                "position": {"overlayPosition": {
                    "anchorCell": {"sheetId": sheet_id, "rowIndex": 3, "columnIndex": CHART_COL},
                    "widthPixels": CHART_W, "heightPixels": CHART_H,
                }},
            }
        }
    }

    trend_request = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Monthly Spend Trend",
                    "basicChart": {
                        "chartType": "COLUMN",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Month"},
                            {"position": "LEFT_AXIS",   "title": "Cost (USD)"},
                        ],
                        "domains": [{"domain": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": monthly_data_start,
                            "endRowIndex":   monthly_data_start + month_count,
                            "startColumnIndex": 0, "endColumnIndex": 1,
                        }]}}}],
                        "series": [{"series": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": monthly_data_start,
                            "endRowIndex":   monthly_data_start + month_count,
                            "startColumnIndex": 1, "endColumnIndex": 2,
                        }]}}, "targetAxis": "LEFT_AXIS"}],
                    },
                },
                "position": {"overlayPosition": {
                    "anchorCell": {"sheetId": sheet_id,
                                   "rowIndex": 3 + CHART_H // 21 + 2,  # below pie chart
                                   "columnIndex": CHART_COL},
                    "widthPixels": CHART_W, "heightPixels": CHART_H,
                }},
            }
        }
    }

    try:
        spreadsheet.batch_update({"requests": [pie_request, trend_request]})
    except Exception as e:
        print(f"  warn: could not add charts — {e}")
