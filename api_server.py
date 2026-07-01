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
import html
import json
import logging
import os
import time
import urllib.error
import urllib.request
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

# Email notifications via Resend. Both must be set for emails to fire.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "").strip()
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "Juan Garcia <rebates@juansrebates.com>").strip()
SHEET_URL      = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

# Map landing-page payload keys → exact CRM column headers.
HEADER_MAP = {
    "firstName":         "First Name",
    "lastName":          "Last Name",
    "phone":             "Phone",
    "email":             "Email",
    "buying":            "Are you buying",
    "targetCity":        "Target City",
    "timeframe":         "Timeframe",
    "preferredLanguage": "Preferred Language",
    # UTM tracking fields (added by readUtms() on the frontend).
    "utm_source":        "utm_source",
    "utm_medium":        "utm_medium",
    "utm_campaign":      "utm_campaign",
    "utm_content":       "utm_content",
    "utm_term":          "utm_term",
}

# Async lock — sheet writes are serialized to avoid row-allocation races.
LOCK = asyncio.Lock()


# --------------------------------------------------------------------------
# Email notifications (Resend)
# --------------------------------------------------------------------------
def _send_email_sync(subject: str, html_body: str, text_body: str, to_email: str = "") -> None:
    """Blocking call to Resend's HTTP API. Swallows errors after logging.
    If to_email is empty, defaults to NOTIFY_EMAIL (agent notification)."""
    if not RESEND_API_KEY:
        log.info("email skipped: RESEND_API_KEY not set")
        return
    recipient = (to_email or NOTIFY_EMAIL or "").strip()
    if not recipient:
        log.info("email skipped: no recipient (NOTIFY_EMAIL not set and no to_email passed)")
        return
    payload = json.dumps({
        "from":    FROM_EMAIL,
        "to":      [recipient],
        "subject": subject,
        "html":    html_body,
        "text":    text_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "juansrebates-backend/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("email sent: subject=%r status=%s", subject, resp.status)
    except urllib.error.HTTPError as e:
        log.error("email failed (HTTP %s): %s", e.code, e.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        log.error("email failed: %s", e)


async def send_email(subject: str, html_body: str, text_body: str, to_email: str = "") -> None:
    """Fire-and-forget email send. Does not block or raise.
    Pass to_email to override the default NOTIFY_EMAIL recipient."""
    try:
        await asyncio.to_thread(_send_email_sync, subject, html_body, text_body, to_email)
    except Exception as e:
        log.error("send_email outer failed: %s", e)


def _esc(v) -> str:
    return html.escape(str(v)) if v else ""


def build_lead_email(d: dict, submission_id: str) -> tuple[str, str, str]:
    name = (d.get("firstName", "") + " " + d.get("lastName", "")).strip() or "(no name)"
    phone = d.get("phone", "") or "-"
    email = d.get("email", "") or "-"
    buying = d.get("buying", "") or "-"
    city = d.get("targetCity", "") or "-"
    timeframe = d.get("timeframe", "") or "-"
    language = d.get("preferredLanguage", "") or "-"
    sms = "Yes" if d.get("smsConsent") == "Yes" else "No"
    calc = build_calc_note(d) or "-"

    subject = f"\U0001F3E0 New rebate lead — {name}"

    text_body = (
        f"New lead from juansrebates.com\n"
        f"-----------------------------------\n"
        f"Name:       {name}\n"
        f"Phone:      {phone}\n"
        f"Email:      {email}\n"
        f"Buying:     {buying}\n"
        f"City:       {city}\n"
        f"Timeframe:  {timeframe}\n"
        f"Language:   {language}\n"
        f"SMS OK:     {sms}\n"
        f"Calc:       {calc}\n"
        f"Submitted:  {now_str()}\n"
        f"Lead ID:    {submission_id}\n\n"
        f"Open MiniCRM: {SHEET_URL}\n"
    )

    html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;max-width:560px;">
  <h2 style="margin:0 0 8px 0;color:#064C74;">\U0001F3E0 New rebate lead</h2>
  <p style="margin:0 0 16px 0;color:#475569;">From <strong>juansrebates.com</strong> — submitted {_esc(now_str())}</p>
  <table style="border-collapse:collapse;font-size:14px;width:100%;">
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Name</td><td style="padding:6px 0;font-weight:600;">{_esc(name)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Phone</td><td style="padding:6px 0;"><a href="tel:{_esc(phone)}" style="color:#0E7C66;text-decoration:none;">{_esc(phone)}</a></td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Email</td><td style="padding:6px 0;"><a href="mailto:{_esc(email)}" style="color:#0E7C66;text-decoration:none;">{_esc(email)}</a></td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Buying</td><td style="padding:6px 0;">{_esc(buying)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">City</td><td style="padding:6px 0;">{_esc(city)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Timeframe</td><td style="padding:6px 0;">{_esc(timeframe)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Language</td><td style="padding:6px 0;">{_esc(language)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">SMS OK</td><td style="padding:6px 0;">{_esc(sms)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;vertical-align:top;">Calc</td><td style="padding:6px 0;">{_esc(calc)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#64748b;">Lead ID</td><td style="padding:6px 0;font-family:ui-monospace,monospace;color:#94a3b8;font-size:12px;">{_esc(submission_id)}</td></tr>
  </table>
  <p style="margin:20px 0 0 0;">
    <a href="{SHEET_URL}" style="display:inline-block;background:#064C74;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;">Open MiniCRM</a>
  </p>
</div>
"""
    return subject, html_body, text_body


def build_prize_email(lead_id: str, prize: str, prize_detail: str) -> tuple[str, str, str]:
    subject = f"\U0001F389 Prize awarded: {prize} (lead {lead_id})"
    text_body = (
        f"Prize awarded\n"
        f"-----------------------------------\n"
        f"Prize:    {prize}\n"
        f"Lead ID:  {lead_id}\n"
        f"At:       {now_str()}\n"
        f"Detail:   {prize_detail or '-'}\n\n"
        f"Open MiniCRM: {SHEET_URL}\n"
    )
    html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;max-width:560px;">
  <h2 style="margin:0 0 8px 0;color:#064C74;">\U0001F389 Prize awarded: <span style="color:#0E7C66;">{_esc(prize)}</span></h2>
  <p style="margin:0 0 16px 0;color:#475569;">{_esc(now_str())} — Lead <span style="font-family:ui-monospace,monospace;color:#94a3b8;">{_esc(lead_id)}</span></p>
  <p style="margin:0 0 12px 0;">{_esc(prize_detail)}</p>
  <p style="margin:20px 0 0 0;">
    <a href="{SHEET_URL}" style="display:inline-block;background:#064C74;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;">Open MiniCRM</a>
  </p>
</div>
"""
    return subject, html_body, text_body


# --------------------------------------------------------------------------
# Lead-facing welcome email (bilingual EN/ES)
# --------------------------------------------------------------------------
BOOKING_URL = "https://calendar.app.google/NaBUBuYuQgpWE4jc8"
AGENT_NAME  = "Juan Garcia"
AGENT_PHONE = "209-605-9911"
BROKERAGE   = "West Wide Finance & Realty"
DRE_AGENT   = "01413283"
DRE_BROKER  = "02106609"
NMLS_ID     = "1928589"

def build_lead_welcome_email(d: dict, submission_id: str) -> tuple[str, str, str]:
    """Bilingual welcome email sent to the lead immediately after form submission.
    Detects language from preferredLanguage field (defaults to English)."""
    first    = (d.get("firstName") or "there").strip() or "there"
    prize    = (d.get("prize") or "").strip()
    lang_raw = (d.get("preferredLanguage") or "").strip().lower()
    is_es    = lang_raw.startswith("es") or lang_raw in ("spanish", "espa\u00f1ol", "espanol")

    if is_es:
        subject = f"\u00a1Bienvenido, {first}! Tu certificado de reembolso est\u00e1 listo"
        prize_line_text = f"Premio del giro: {prize}\n" if prize else ""
        prize_line_html = f'<p style="margin:0 0 12px 0;"><strong>Premio del giro:</strong> {_esc(prize)}</p>' if prize else ""
        text_body = (
            f"Hola {first},\n\n"
            f"\u00a1Gracias por usar juansrebates.com! Tu certificado de reembolso ya est\u00e1 reservado.\n\n"
            f"{prize_line_text}"
            f"Pr\u00f3ximo paso: reserva una consulta gratis de 15 minutos para confirmar tu premio y empezar a buscar casas.\n\n"
            f"Reserva aqu\u00ed: {BOOKING_URL}\n\n"
            f"\u00bfPreguntas? Llama o escr\u00edbeme: {AGENT_PHONE}\n\n"
            f"\u2014 {AGENT_NAME}\n"
            f"{BROKERAGE}\n"
            f"CA DRE #{DRE_AGENT}  \u00b7  Brokerage DRE #{DRE_BROKER}  \u00b7  NMLS #{NMLS_ID}\n"
            f"Lead ID: {submission_id}\n"
        )
        html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0F3D4A;max-width:560px;background:#F2EDDF;padding:32px 28px;border-radius:12px;">
  <h2 style="margin:0 0 6px 0;color:#0F3D4A;font-size:24px;">\u00a1Hola, {_esc(first)}! \ud83c\udf89</h2>
  <p style="margin:0 0 18px 0;color:#0F3D4A;font-size:16px;line-height:1.5;">Gracias por usar <strong>juansrebates.com</strong>. Tu certificado de reembolso ya est\u00e1 reservado.</p>
  {prize_line_html}
  <p style="margin:18px 0 22px 0;font-size:15px;line-height:1.5;"><strong>Pr\u00f3ximo paso:</strong> reserva una consulta gratis de 15 minutos por tel\u00e9fono. Confirmamos tu premio, contestamos tus preguntas, y empezamos a buscar casas.</p>
  <p style="margin:0 0 28px 0;">
    <a href="{BOOKING_URL}" style="display:inline-block;background:#0F3D4A;color:#fff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:700;font-size:16px;">Reservar mi consulta de 15 min</a>
  </p>
  <p style="margin:0 0 8px 0;font-size:14px;color:#0F3D4A;">\u00bfPreguntas? Llama o escr\u00edbeme: <a href="tel:{AGENT_PHONE}" style="color:#B89328;text-decoration:none;font-weight:600;">{AGENT_PHONE}</a></p>
  <hr style="border:none;border-top:1px solid #d4cab3;margin:24px 0 16px 0;">
  <p style="margin:0;font-size:13px;color:#0F3D4A;line-height:1.6;">
    \u2014 <strong>{_esc(AGENT_NAME)}</strong><br>
    {_esc(BROKERAGE)}<br>
    <span style="color:#6b7d80;">CA DRE #{DRE_AGENT} &nbsp;\u00b7&nbsp; Brokerage DRE #{DRE_BROKER} &nbsp;\u00b7&nbsp; NMLS #{NMLS_ID}</span>
  </p>
</div>
"""
    else:
        subject = f"Welcome {first} \u2014 your rebate certificate is locked in"
        prize_line_text = f"Spin prize: {prize}\n" if prize else ""
        prize_line_html = f'<p style="margin:0 0 12px 0;"><strong>Spin prize:</strong> {_esc(prize)}</p>' if prize else ""
        text_body = (
            f"Hi {first},\n\n"
            f"Thanks for using juansrebates.com! Your buyer rebate certificate is locked in.\n\n"
            f"{prize_line_text}"
            f"Next step: book a free 15-minute consult so we can confirm your prize and start touring homes.\n\n"
            f"Book here: {BOOKING_URL}\n\n"
            f"Questions? Call or text me: {AGENT_PHONE}\n\n"
            f"\u2014 {AGENT_NAME}\n"
            f"{BROKERAGE}\n"
            f"CA DRE #{DRE_AGENT}  \u00b7  Brokerage DRE #{DRE_BROKER}  \u00b7  NMLS #{NMLS_ID}\n"
            f"Lead ID: {submission_id}\n"
        )
        html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0F3D4A;max-width:560px;background:#F2EDDF;padding:32px 28px;border-radius:12px;">
  <h2 style="margin:0 0 6px 0;color:#0F3D4A;font-size:24px;">Hi {_esc(first)} \ud83c\udf89</h2>
  <p style="margin:0 0 18px 0;color:#0F3D4A;font-size:16px;line-height:1.5;">Thanks for using <strong>juansrebates.com</strong>. Your buyer rebate certificate is locked in.</p>
  {prize_line_html}
  <p style="margin:18px 0 22px 0;font-size:15px;line-height:1.5;"><strong>Next step:</strong> book a free 15-minute phone consult. We'll confirm your prize, answer questions, and start lining up homes to tour.</p>
  <p style="margin:0 0 28px 0;">
    <a href="{BOOKING_URL}" style="display:inline-block;background:#0F3D4A;color:#fff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:700;font-size:16px;">Book my 15-min consult</a>
  </p>
  <p style="margin:0 0 8px 0;font-size:14px;color:#0F3D4A;">Questions? Call or text me: <a href="tel:{AGENT_PHONE}" style="color:#B89328;text-decoration:none;font-weight:600;">{AGENT_PHONE}</a></p>
  <hr style="border:none;border-top:1px solid #d4cab3;margin:24px 0 16px 0;">
  <p style="margin:0;font-size:13px;color:#0F3D4A;line-height:1.6;">
    \u2014 <strong>{_esc(AGENT_NAME)}</strong><br>
    {_esc(BROKERAGE)}<br>
    <span style="color:#6b7d80;">CA DRE #{DRE_AGENT} &nbsp;\u00b7&nbsp; Brokerage DRE #{DRE_BROKER} &nbsp;\u00b7&nbsp; NMLS #{NMLS_ID}</span>
  </p>
</div>
"""
    return subject, html_body, text_body


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
        "Phone":       d.get("phone", ""),
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
        # UTM tracking fields — written if present in payload, empty otherwise.
        "utm_source":         d.get("utm_source", ""),
        "utm_medium":         d.get("utm_medium", ""),
        "utm_campaign":       d.get("utm_campaign", ""),
        "utm_content":        d.get("utm_content", ""),
        "utm_term":           d.get("utm_term", ""),
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
                # Fire-and-forget email notification.
                if result.get("ok"):
                    subj, html_b, text_b = build_prize_email(
                        body.get("leadId", ""),
                        body.get("prize", ""),
                        body.get("prizeDetail", ""),
                    )
                    asyncio.create_task(send_email(subj, html_b, text_b))
            else:
                result = await append_lead_row(body)
                if result.get("ok"):
                    # 1) Agent notification (to NOTIFY_EMAIL).
                    subj, html_b, text_b = build_lead_email(body, result.get("leadId", ""))
                    asyncio.create_task(send_email(subj, html_b, text_b))
                    # 2) Lead-facing welcome email with booking link.
                    lead_email = (body.get("email") or "").strip()
                    if lead_email and "@" in lead_email:
                        w_subj, w_html, w_text = build_lead_welcome_email(
                            body, result.get("leadId", "")
                        )
                        asyncio.create_task(send_email(w_subj, w_html, w_text, lead_email))
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
