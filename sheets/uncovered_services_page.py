"""
Sheet for AWS services that have billing spend but no deep collector.

Shows:
- Service name
- Monthly cost for each of the last N months
- Total spend
- Trend (up/down vs previous month)
- Coverage status (billing_only)
"""

from collections import defaultdict
from .writer import header_format, freeze_row, auto_resize, apply_formats, safe_add_worksheet, safe_update


def build_uncovered_services_page(spreadsheet, monthly_costs, billing_only_keys, covered_keys):
    """
    monthly_costs: raw list of {month, service, cost} from Cost Explorer
    billing_only_keys: set of CE service names / keys with no deep collector
    covered_keys: set of service keys we DO have full coverage for
    """
    ws = safe_add_worksheet(spreadsheet, "All Services (Billing)", rows=200, cols=20)
    sheet_id = ws.id

    # Aggregate cost per service per month
    by_service = defaultdict(lambda: defaultdict(float))
    for row in monthly_costs:
        by_service[row["service"]][row["month"]] += row["cost"]

    all_months = sorted({row["month"] for row in monthly_costs})
    last_month = all_months[-1] if all_months else ""
    prev_month = all_months[-2] if len(all_months) >= 2 else ""

    rows = []
    rows.append(["AWS Services — Full Billing Overview", "", "", "", ""])
    rows.append(["Services with full resource analysis are shown in their own tabs.", "", "", "", ""])
    rows.append(["Services listed here are billing-only — no resource-level data collected.", "", "", "", ""])
    rows.append(["", "", "", "", ""])

    # Header row
    header = ["Service", "Coverage"] + all_months + ["Total (Period)", "MoM Change", "MoM Change %"]
    rows.append(header)

    # Sort all services by total descending
    all_services_totals = {
        svc: sum(costs.values())
        for svc, costs in by_service.items()
    }

    from analysis.service_registry import CE_NAME_TO_KEY, FULLY_SUPPORTED, SKIP_SERVICES

    for svc, total in sorted(all_services_totals.items(), key=lambda x: x[1], reverse=True):
        if total == 0:
            continue

        key = CE_NAME_TO_KEY.get(svc, svc)

        if key in SKIP_SERVICES:
            coverage = "Skipped"
        elif key in FULLY_SUPPORTED:
            coverage = "Full Analysis"
        else:
            coverage = "Billing Only"

        monthly_vals = [round(by_service[svc].get(m, 0), 2) for m in all_months]
        total_val = round(sum(monthly_vals), 2)

        last_cost = by_service[svc].get(last_month, 0)
        prev_cost = by_service[svc].get(prev_month, 0)
        mom_delta = round(last_cost - prev_cost, 2) if prev_cost else ""
        mom_pct = f"{round((last_cost - prev_cost) / prev_cost * 100, 1):+.1f}%" if prev_cost else ""

        rows.append([svc, coverage] + monthly_vals + [total_val, mom_delta, mom_pct])

    # Grand total row
    grand_total_by_month = []
    for m in all_months:
        grand_total_by_month.append(round(sum(
            by_service[svc].get(m, 0) for svc in by_service
            if CE_NAME_TO_KEY.get(svc, svc) not in SKIP_SERVICES
        ), 2))
    rows.append(
        ["TOTAL (excl. Tax/Support)", ""] +
        grand_total_by_month +
        [round(sum(grand_total_by_month), 2), "", ""]
    )

    safe_update(ws, "A1", rows, value_input_option="USER_ENTERED")

    # Format header row (row 5 = index 4)
    col_count = len(header)
    formats = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 4, "endRowIndex": 5},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.18, "green": 0.34, "blue": 0.62},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True,
                        },
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        },
        # Freeze first 5 rows (title block + header)
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 5}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Color "Full Analysis" rows green, "Billing Only" rows yellow
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 200,
                                "startColumnIndex": 1, "endColumnIndex": 2}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Full Analysis"}]},
                        "format": {"backgroundColor": {"red": 0.85, "green": 0.95, "blue": 0.85}},
                    },
                },
                "index": 0,
            }
        },
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 200,
                                "startColumnIndex": 1, "endColumnIndex": 2}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Billing Only"}]},
                        "format": {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80}},
                    },
                },
                "index": 1,
            }
        },
        auto_resize(sheet_id),
    ]
    apply_formats(spreadsheet, formats)

    return ws
