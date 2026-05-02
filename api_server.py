#!/usr/bin/env python3
"""
juansrebates.com — backend.

Receives form submissions from the landing page and writes them directly
to the MiniCRM Google Sheet via a Google service account.

Endpoints:
  GET  /api/health              health check
  POST /api/submit              accepts {type:"lead"|"prize", ...}

Environment variables (required):
  GOOGLE_SERVICE_ACCOUNT_JSON   the service account key, as a single-line JSON string
  SHEET_ID                      Google Sheet ID (defaults to MiniCRM)

Optional:
  ALLOWED_ORIGINS               comma-separated list of CORS origins; default "*"
  PORT                          server port (defaults to 8000; Render sets this automatically)
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("juansrebates")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SHEET_ID     = os.environ.get("SHEET_ID", "1ulM7OyW1eTkxErfqbmCHQaWYDYthk9FyGW4pHb-Na_k")
WORKSHEET_ID = 0           # CRM tab
SHEET_NAME   = "CRM"
TZ           = ZoneInfo("America/Los_Angeles")
SCOPES       = ["https://www.googleapis.com/auth/spreadsheets"]

# Map landing-page payload keys → exact CRM column headers.
HEADER_MAP = {
    "firstName":         "First Name",
    "lastName":          "Last Name",
    "phone":             "209-888-8888",
    "email":             "Email",
    "buying":            "Are you buying",
    "targetCity":        "Target City",
    "timeframe":         "Timeframe",
    "preferredLanguage": "Preferred Language",
}

# Async lock — sheet writes are serialized to avoid row-allocation races.
LOCK = asyncio.Lock()


# --------------------------------------------------------------------------
# Google Sheets client (service account)
# --------------------------------------------------------------------------
def load_credentials():
    """Load service account credentials from env or local file."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Local dev fallback: ./secrets/service-account.json
    local_path = os.path.join(os.path.dirname(__file__), "secrets", "service-account.json")
    if os.path.exists(local_path):
        return service_account.Credentials.from_service_account_file(local_path, scopes=SCOPES)

    raise RuntimeError(
        "No service account credentials found. "
        "Set GOOGLE_SERVICE_ACCOUNT_JSON env var or place key at secrets/service-account.json"
    )


# Initialize the Sheets service once at startup.
CREDS = load_credentials()
SHEETS = build("sheets", "v4", credentials=CREDS, cache_discovery=False)
log.info("Service account loaded: %s", CREDS.service_account_email)


# --------------------------------------------------------------------------
# Sheet helpers
# --------------------------------------------------------------------------
def now_str() -> str:
    return datetime.now(TZ).strftime("%-m/%-d/%Y %-H:%M:%S")


def build_calc_note(d: dict) -> str:
    parts = []
    if d.get("calcPrice"):
        try:
            parts.append(f"Price: ${int(float(d['calcPrice'])):,}")
        except (TypeError, ValueError):
            parts.append(f"Price: {d['calcPrice']}")
    if d.get("calcRate"):
        parts.append(f"Rate: {d['calcRate']}%")
    if d.get("calcRebate"):
        parts.append(f"Est. rebate: {d['calcRebate']}")
    if d.get("pageUrl"):
        parts.append(f"Page: {d['pageUrl']}")
    return " | ".join(parts)


def get_headers() -> list[str]:
    """Read the header row from the CRM tab."""
    res = SHEETS.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!1:1"
    ).execute()
    return res.get("values", [[]])[0]


def build_lead_row(d: dict) -> tuple[dict, str]:
    """Build dict keyed by exact column headers. Returns (row_dict, submission_id)."""
    sms_yes = d.get("smsConsent") == "Yes"
    submission_id = d.get("leadId") or f"PPLX-{int(time.time() * 1000)}"
    row = {
        "Submission ID":      submission_id,
        "Prize":              "",
        "Source":             "Landing Page",
        "Respondent ID":      submission_id,
        "Submitted at":       now_str(),
        "First Name":         d.get("firstName", ""),
        "Last Name":          d.get("lastName", ""),
        "209-888-8888":       d.get("phone", ""),
        "Email":              d.get("email", ""),
        "Are you buying":     d.get("buying", ""),
        "Target City":        d.get("targetCity", ""),
        "Timeframe":          d.get("timeframe", ""),
        "Preferred Language": d.get("preferredLanguage", ""),
        "SMS Consent": (
            "Yes, you can text me my certificate and updates about homes and rebates."
            if sms_yes else ""
        ),
        "SMS Consent (Yes, you can text me my certificate and updates about homes and rebates.)": (
            "TRUE" if sms_yes else "FALSE"
        ),
        "prize":              "",
        "Notes":              build_calc_note(d),
    }
    return row, submission_id


def dict_to_row_array(row_dict: dict, headers: list[str]) -> list:
    """Convert a header-keyed dict to a positional list aligned to the sheet's headers."""
    return [str(row_dict.get(h, "")) for h in headers]


def append_lead_row_sync(d: dict) -> dict:
    row_dict, submission_id = build_lead_row(d)
    headers = get_headers()
    values = dict_to_row_array(row_dict, headers)
    res = SHEETS.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_NAME}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
    updated = res.get("updates", {})
    log.info("lead inserted: id=%s range=%s", submission_id, updated.get("updatedRange"))
    return {
        "ok":      True,
        "action":  "lead_inserted",
        "leadId":  submission_id,
        "added":   updated.get("updatedRows", 0),
        "range":   updated.get("updatedRange", ""),
    }


def find_row_by_submission_id(submission_id: str) -> int | None:
    """Find the row number (1-indexed) where Submission ID == submission_id. Returns None if not found."""
    res = SHEETS.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A:A"
    ).execute()
    rows = res.get("values", [])
    # Search bottom-up so newest match wins (in case of duplicates).
    for i in range(len(rows) - 1, 0, -1):  # skip row 1 (header)
        if rows[i] and rows[i][0] == submission_id:
            return i + 1  # convert 0-indexed → 1-indexed
    return None


def update_prize_sync(d: dict) -> dict:
    lead_id = d.get("leadId")
    if not lead_id:
        return {"ok": False, "error": "Missing leadId"}
    prize        = d.get("prize", "")
    prize_detail = d.get("prizeDetail", "")

    row_num = find_row_by_submission_id(lead_id)
    if not row_num:
        # Orphan — never lose a prize event.
        log.warning("orphan prize: leadId=%s prize=%s", lead_id, prize)
        orphan_dict = {
            "Submission ID":  lead_id,
            "Prize":          prize,
            "Source":         "Landing Page — prize only",
            "Respondent ID":  lead_id,
            "Submitted at":   now_str(),
            "prize":          prize,
            "Notes":          f"Prize orphan — {prize_detail}",
        }
        headers = get_headers()
        values = dict_to_row_array(orphan_dict, headers)
        SHEETS.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
        return {"ok": True, "action": "prize_inserted_orphan", "leadId": lead_id}

    # Update the Prize column (B), lowercase prize column (P), and Notes (AA) in one batch.
    data = [
        {"range": f"{SHEET_NAME}!B{row_num}", "values": [[prize]]},
        {"range": f"{SHEET_NAME}!P{row_num}", "values": [[prize]]},
    ]
    if prize_detail:
        data.append({
            "range":  f"{SHEET_NAME}!AA{row_num}",
            "values": [[f"Prize: {prize} — {prize_detail}"]],
        })

    SHEETS.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    log.info("prize updated: leadId=%s row=%s prize=%s", lead_id, row_num, prize)
    return {"ok": True, "action": "prize_updated", "leadId": lead_id, "row": row_num}


# --------------------------------------------------------------------------
# Async wrappers — push blocking sheets calls to a thread.
# --------------------------------------------------------------------------
async def append_lead_row(d: dict) -> dict:
    return await asyncio.to_thread(append_lead_row_sync, d)


async def update_prize(d: dict) -> dict:
    return await asyncio.to_thread(update_prize_sync, d)


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    log.info("backend starting (sheet_id=%s)", SHEET_ID)
    yield
    log.info("backend shutting down")


app = FastAPI(lifespan=lifespan)

# CORS — allow the Netlify frontend (and your custom domain) to call this API.
allowed = os.environ.get("ALLOWED_ORIGINS", "*")
origins = [o.strip() for o in allowed.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {
        "ok":      True,
        "service": "juansrebates",
        "time":    now_str(),
        "sheet":   SHEET_ID,
        "sa":      CREDS.service_account_email,
    }


@app.post("/api/submit")
async def submit(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        if "payload" in form:
            try:
                body = json.loads(form["payload"])
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"bad payload: {e}"}, status_code=400)
        else:
            return JSONResponse({"ok": False, "error": "no JSON body"}, status_code=400)

    log.info("submit: type=%s leadId=%s", body.get("type"), body.get("leadId"))

    try:
        async with LOCK:
            if body.get("type") == "prize":
                result = await update_prize(body)
            else:
                result = await append_lead_row(body)
        return JSONResponse(result)
    except HttpError as e:
        log.exception("Google API error")
        return JSONResponse({"ok": False, "error": f"sheets api: {e}"}, status_code=502)
    except Exception as e:
        log.exception("submit failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
