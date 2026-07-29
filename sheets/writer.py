import json
import os
import time
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from datetime import datetime

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TOKEN_PATH = os.path.expanduser("~/.config/gspread/cost-report-token.json")

# Color palette
COLOR = {
    "header_bg": {"red": 0.18, "green": 0.34, "blue": 0.62},
    "header_fg": {"red": 1.0, "green": 1.0, "blue": 1.0},
    "section_bg": {"red": 0.85, "green": 0.90, "blue": 0.97},
    "green": {"red": 0.20, "green": 0.66, "blue": 0.33},
    "red": {"red": 0.80, "green": 0.20, "blue": 0.20},
    "yellow": {"red": 0.95, "green": 0.76, "blue": 0.20},
    "white": {"red": 1.0, "green": 1.0, "blue": 1.0},
    "light_gray": {"red": 0.95, "green": 0.95, "blue": 0.95},
}


def connect(credentials_file):
    """
    Accepts either:
    - A service account JSON (type: service_account) — needs write permissions
    - An OAuth2 client secrets JSON (type: application) — opens browser on first run
    """
    with open(credentials_file) as f:
        cred_data = json.load(f)

    cred_type = cred_data.get("type", "")

    if cred_type == "service_account":
        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    else:
        # OAuth2 client secrets — open browser on first run, token cached after
        creds = None
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        if os.path.exists(TOKEN_PATH):
            creds = UserCredentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w") as token:
                token.write(creds.to_json())

    gc = gspread.authorize(creds)
    gc._creds = creds
    return gc


def _drive_service(gc):
    return build("drive", "v3", credentials=gc._creds, cache_discovery=False)
def create_spreadsheet(gc, title, share_with=None):
    # Use Sheets API v4 directly to bypass Drive storage quota
    sheets_svc = build("sheets", "v4", credentials=gc._creds, cache_discovery=False)
    body = {"properties": {"title": title}}
    resp = sheets_svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    spreadsheet_id = resp["spreadsheetId"]
    sh = gc.open_by_key(spreadsheet_id)
    if share_with:
        sh.share(share_with, perm_type="user", role="writer")
    return sh


def header_format(sheet_id):
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR["header_bg"],
                    "textFormat": {
                        "foregroundColor": COLOR["header_fg"],
                        "bold": True,
                        "fontSize": 10,
                    },
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    }


def freeze_row(sheet_id, rows=1):
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": rows},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def auto_resize(sheet_id):
    return {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 30,
            }
        }
    }
def color_delta_cells(sheet_id, start_row, end_row, delta_col_index):
    """Green for a negative delta (a saving), red for a positive one."""
    def rule(condition_type, color, index):
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": end_row,
                        "startColumnIndex": delta_col_index,
                        "endColumnIndex": delta_col_index + 1,
                    }],
                    "booleanRule": {
                        "condition": {"type": condition_type,
                                      "values": [{"userEnteredValue": "0"}]},
                        "format": {"textFormat": {"foregroundColor": color}},
                    },
                },
                "index": index,
            }
        }

    if end_row <= start_row:
        return []
    return [rule("NUMBER_LESS", COLOR["green"], 0),
            rule("NUMBER_GREATER", COLOR["red"], 1)]


# Google allows 60 write requests per minute per user. A large account builds
# ~30 tabs at ~3 writes each, which blows straight through it: the previous
# retry-only approach let the run die mid-report. Spacing writes just over one
# second apart keeps us under the limit by construction, so retries become the
# exception rather than the mechanism.
_MIN_WRITE_INTERVAL_S = 1.05
_last_write = [0.0]


def _throttle():
    elapsed = time.monotonic() - _last_write[0]
    if elapsed < _MIN_WRITE_INTERVAL_S:
        time.sleep(_MIN_WRITE_INTERVAL_S - elapsed)
    _last_write[0] = time.monotonic()


def _is_quota_error(exc):
    text = str(exc)
    return ("429" in text or "Quota exceeded" in text
            or "RESOURCE_EXHAUSTED" in text or "rateLimitExceeded" in text)


def _with_retry(fn, *args, **kwargs):
    """Run a Sheets write with throttling and quota-aware backoff."""
    for attempt in range(6):
        _throttle()
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if _is_quota_error(e) and attempt < 5:
                # Quota windows are 60s; back off across one before retrying.
                time.sleep(min(60, 10 * (attempt + 1)))
            else:
                raise


def safe_update(ws, cell, rows, **kwargs):
    return _with_retry(ws.update, cell, rows, **kwargs)


def apply_formats(spreadsheet, requests):
    if not requests:
        return
    return _with_retry(spreadsheet.batch_update, {"requests": requests})
def safe_add_worksheet(spreadsheet, title, rows=500, cols=30):
    return _with_retry(spreadsheet.add_worksheet, title=title, rows=rows, cols=cols)
