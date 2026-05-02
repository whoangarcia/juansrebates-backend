# juansrebates.com — backend

FastAPI service that receives lead/prize submissions from the landing page and
writes them to the MiniCRM Google Sheet via a Google service account.

## Local development

```bash
pip install -r requirements.txt

# Place service-account.json at:
#   secrets/service-account.json
# (gitignored — never commit)

python api_server.py
# → http://localhost:8000/api/health
```

## Deployment (Render)

1. Push this repo to GitHub.
2. In Render, create a new **Blueprint** and point at this repo. It picks up `render.yaml`.
3. After the first build, go to **Environment** for the service and paste the
   service-account JSON (the entire file contents, single line) into
   `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. Redeploy. Health check: `GET /api/health` should return 200.

## API

### `GET /api/health`
Returns `{ ok, service, time, sheet, sa }`.

### `POST /api/submit`
Accepts JSON. Two shapes:

**Lead**
```json
{
  "type": "lead",
  "firstName": "Maria", "lastName": "Garcia",
  "phone": "209-555-0199", "email": "maria@example.com",
  "buying": "Yes, in next 6 months",
  "targetCity": "Mountain House",
  "timeframe": "3-6 months",
  "preferredLanguage": "Bilingual (English/Spanish)",
  "smsConsent": "Yes",
  "calcPrice": 650000, "calcRate": 3.0, "calcRebate": "$9,250"
}
```
Response: `{ ok, action: "lead_inserted", leadId, added, range }`

**Prize**
```json
{ "type": "prize", "leadId": "PPLX-1777...", "prize": "$1,000", "prizeDetail": "..." }
```
Response: `{ ok, action: "prize_updated", leadId, row }`

## CORS

Set `ALLOWED_ORIGINS` env var to a comma-separated list of allowed origins (e.g.
`https://juansrebates.com,https://www.juansrebates.com`). Default `*` is fine for
local dev but you should lock it down in production.
