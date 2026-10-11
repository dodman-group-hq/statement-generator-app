# Statement Generator

Merge a Word template (`.docx`) with an Excel spreadsheet (`.xlsx`) and get back **one personalized PDF per row**, optionally password-protected with each row's own ID. Results are delivered as a single zip.

Built with FastAPI, asyncpg (Postgres on Neon) and LibreOffice for PDF conversion. Part of the Dodman Group product suite and shares its `users` table with Vett.

## Features

- Word template + Excel data in, one PDF per spreadsheet row out
- Optional per-PDF password protection (the row's own ID is the password)
- Background jobs with a progress endpoint and a zip download
- Self-serve accounts: sign up, log in, create API keys from a dashboard
- API-key authentication (`X-API-Key`), keys stored only as SHA-256 hashes
- Per-plan limits: jobs per month, rows per job, password protection on/off
- Tenant isolation: Postgres Row-Level Security, per-client filesystem roots, owner checks on every job lookup
- Resilient conversion: adaptive batching, retries, bisection to isolate a bad document, circuit breaker
- Automatic cleanup of generated files after 2 hours
- Optional Sentry error tracking

## Requirements

- Python 3.12 (the version the Dockerfile uses)
- A Postgres database (developed against Neon)
- LibreOffice with Writer, plus the Liberation, Carlito and Caladea fonts, for docx to PDF conversion
- Python dependencies in `requirements.txt`

The Dockerfile installs all system dependencies, so Docker is the easiest way to get a working environment.

## Configuration

Copy `.env.example` to `.env` and fill it in.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | Yes | - | Postgres connection string (keep `sslmode=require` for Neon) |
| `SESSION_SECRET_KEY` | Yes in production | random per process in development | Signs login session cookies. The app **refuses to start** in production without it. Use the same value as Vett. |
| `APP_ENV` | No | `production` | `production` makes `SESSION_SECRET_KEY` mandatory and marks the session cookie `Secure`. Use `development` locally. Also tags Sentry events. |
| `APP_BASE_URL` | For billing / email links | unset | Public URL of the app; used in checkout callbacks and email buttons |
| `PAYSTACK_*`, `RESEND_*` | No | unset | Billing and email - see `.env.example` |
| `MAX_UPLOAD_MB` | No | `25` | Maximum upload size; larger requests get a 413 |
| `SENTRY_DSN` | No | unset | Enables Sentry. Leave unset to run without it. |
| `MAX_CONCURRENT_JOBS` | No | `3` | Statement jobs running at once |
| `LIBREOFFICE_CONCURRENCY` | No | `2` | LibreOffice processes running at once, across all jobs |
| `CONVERSION_BATCH_TARGET_BYTES` | No | `8388608` | Generated `.docx` bytes per LibreOffice call. Plain integer only. |
| `CONVERSION_MAX_BATCH_SIZE` | No | `30` | Maximum documents per LibreOffice call |
| `CONVERSION_BATCH_MAX_RETRIES` | No | `2` | Retries per failed batch before bisecting |
| `CONVERSION_CIRCUIT_BREAKER_THRESHOLD` | No | `2` | Consecutive failed batches before the job aborts |

Generate a session secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Running locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit it; set APP_ENV=development for local work

uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`. Interactive API docs are at `/docs`.

On startup the app creates its tables if they don't exist (`init_schema` is idempotent). If the database is unreachable at boot the app still starts, and requests get a 503 until it is reachable.

### Running with Docker

```bash
docker build -t statement-generator .
docker run --env-file .env -p 8000:8000 statement-generator
```

The container binds to `$PORT` (Render sets this) and falls back to 8000.

> **Always run a single worker (`--workers 1`).** Job progress and download state live in process memory, so a second worker would never see jobs started on the first. See *Known limitations*.

## Using the app

1. **Sign up** at `/accounts/signup`, then log in.
2. On the **dashboard** (`/accounts/dashboard`), create an API key. It is shown **once**, so copy it immediately. Only its hash is stored.
3. Open the **generator page** (`/api/statement_generator`), paste your key, upload your template and spreadsheet, and choose whether to password-protect the PDFs.
4. Watch the progress bar, then download the zip.

Keys can also be issued from the command line (no login account attached):

```bash
python scripts/create_api_key.py "Acme Corp" --plan paid
python scripts/create_api_key.py "Pilot Client" --plan free
python scripts/create_api_key.py "Acme Corp" --plan paid --limit 500 --max-rows 1000
```

### Plans and limits

Plans mirror Vett's structure. Limits live in one place, `PLAN_DEFAULTS` in `app/db.py`, and are used by the CLI, signup, billing and the dashboard's plan descriptions. **The starter numbers are placeholders - set your real pricing there.**

| Plan | Jobs / month | Rows / job | Password protection | How you get it |
|---|---|---|---|---|
| `free` | 5 | 25 | No | Default on signup |
| `starter` | 100 | 500 | Yes | Paystack subscription |
| `pro` | Unlimited | Unlimited | Yes | Paystack subscription |
| anything else (`internal`, `enterprise`, ...) | Unlimited | Unlimited | Yes | Issued by CLI |

The CLI can override any value per client with `--limit`, `--max-rows`, `--allow-password-protection` or `--no-password-protection`. Cancelling a subscription drops the user's keys to free limits; keys are never deactivated by billing.

### Billing and email

Billing (Paystack, via the shared `dodman-billing` package) and email (Resend) are optional: with their environment variables unset the app runs normally, billing routes are absent, and emails are only logged. See `.env.example`. Emails sent: welcome, new API key created, approaching / reaching the monthly job limit, and (through billing) receipts and subscription notices. All are transactional; there is no marketing email.

## Preparing your files

### Word template

- Write a normal `.docx` (or `.doc`) and mark the parts that change with double curly braces: `{{ name }}`, `{{ balance }}`.
- Every placeholder must have a matching column in the spreadsheet, or the job fails with a list of the missing fields.

### Excel spreadsheet

- First row is the header; one row per person. Only the active sheet is read.
- Headers are matched to placeholders after turning spaces into underscores, so a `Full Name` column fills `{{ Full_Name }}`.
- Two columns are required, in any position:
  - **ID**: one of `id`, `client id`, `member id`, `id number`
  - **Name**: one of `name`, `client name`, `member name`
- The name is used to name each PDF; the ID is the password when protection is on.

How values are rendered: numbers are shown with thousands separators and no decimals, `0` and empty cells become `-`, everything else is converted to text.

Edge cases:

- A row with no name fails the whole job (with the row number).
- A row with no ID still generates a PDF, but is **not** password-protected and appears in the job's warnings.
- Duplicate IDs or names get a numeric suffix so no file is overwritten.
- A document that still can't be converted after retries is skipped and listed in the warnings; the rest of the job continues.

An example template and spreadsheet are available at `/api/download_examples`.

## API

Authenticate every endpoint marked "key" with the `X-API-Key` header. Errors are returned as `{"message": "..."}`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/process_statement` | key | Start a job |
| `GET` | `/api/progress/{temp_id}` | key | Check job status |
| `GET` | `/api/download_statement/{temp_id}` | key | Download the zip of PDFs |
| `GET` | `/api/health` | none | Uptime check (verifies database connectivity) |
| `GET` | `/api/download_examples` | none | Example template + spreadsheet |
| `GET` | `/api/terms`, `/api/privacy` | none | Legal pages |
| `GET` | `/api/statement_generator` | none | The generator web page |
| `GET` | `/docs` | none | Interactive OpenAPI docs |

### Start a job

```http
POST /api/process_statement
X-API-Key: sg_live_...
Content-Type: multipart/form-data

template_file=<.docx>   data_file=<.xlsx>   password_protection=true|false (optional)
```

Returns `202 Accepted`:

```json
{
  "temp_id": "6f1b0c1e-....",
  "message": "Processing started.",
  "status_url": "/api/progress/6f1b0c1e-....",
  "download_link": "/api/download_statement/6f1b0c1e-...."
}
```

| Status | Meaning |
|---|---|
| 400 | Wrong file type, unreadable data file, or more rows than your plan allows |
| 401 | Missing or invalid API key |
| 403 | Password protection isn't available on your plan |
| 413 | Upload larger than `MAX_UPLOAD_MB` |
| 429 | Monthly job limit reached |
| 503 | Database temporarily unavailable |

### Check progress

```http
GET /api/progress/{temp_id}
```

```json
{ "state": "processing", "message": "Processing statement 12/40...", "progress": 34 }
```

`state` is `processing`, `completed` or `error`. A finished job may include a `warnings` array (skipped documents, rows left unprotected). An unknown, expired or someone else's ID returns `404` with `state: "unknown"`.

### Download

```http
GET /api/download_statement/{temp_id}
```

Returns a zip with one PDF per row, named after the person. `409` means the job isn't finished; `404` means the ID is unknown or expired. Files are deleted **2 hours** after the job is created.

### Example

```bash
curl -X POST http://localhost:8000/api/process_statement \
  -H "X-API-Key: $KEY" \
  -F "template_file=@template.docx" \
  -F "data_file=@data.xlsx" \
  -F "password_protection=true"

curl -H "X-API-Key: $KEY" http://localhost:8000/api/progress/$TEMP_ID
curl -H "X-API-Key: $KEY" -o statements.zip http://localhost:8000/api/download_statement/$TEMP_ID
```

## How a job runs

1. The request is validated: file types, plan permissions, monthly job limit, rows per job.
2. A job row is recorded and the API returns `202` straight away.
3. The blocking work runs in a dedicated thread pool (`MAX_CONCURRENT_JOBS`).
4. The template is checked against the spreadsheet headers.
5. Each row is rendered to `.docx` with `docxtpl` and converted to PDF by LibreOffice in **adaptive batches** (bounded by total bytes and document count), so disk use stays roughly constant however large the job is.
6. A failed batch is retried, then split in half repeatedly to isolate the bad document. If several top-level batches fail in a row, the job stops as a likely systemic problem.
7. PDFs are password-protected (if requested) and renamed after each person.
8. Uploaded source files are deleted as soon as the job finishes; the zip is built on download.

LibreOffice runs in its own process group so a timeout kills the whole tree, a semaphore caps concurrent instances, and a background reaper clears any orphaned processes.

## Project structure

```
main.py                          App entry point: lifespan, upload limit, error handler, routes
app/
  db.py                          Schema, queries, plan defaults, Row-Level Security
  auth.py                        X-API-Key dependency
  accounts.py                    Password hashing (bcrypt) and signed session tokens
  observability.py               Optional Sentry wrappers
  routes/
    generate_statements.py       Job routes and the StatementGenerator class
    accounts.py                  Signup, login, logout, dashboard, key creation
  templates/                     Landing page, generator, auth, dashboard, legal, 404
  static/                        Theme CSS, SVGs, example files
scripts/create_api_key.py        Issue an API key from the command line
tests/                           Test suite
Dockerfile                       Production image (Python 3.12 + LibreOffice + fonts)
render-build.sh                  Build script for Render's native runtime
```

## Security

- API keys and passwords are stored as one-way hashes (SHA-256 and bcrypt respectively).
- Every job lookup checks ownership; unknown and foreign IDs return the same 404.
- `statement_jobs` uses Postgres Row-Level Security, forced even for the table owner.
- Each client's files live under their own directory, and paths are built only through a guard that refuses to leave it.
- The session cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- Sessions are stateless signed tokens; the cookie expires after 7 days.

## Operations notes

- **Memory:** LibreOffice is the main consumer. Lower `LIBREOFFICE_CONCURRENCY` or `MAX_CONCURRENT_JOBS` on small instances. Job logs include RSS readings per phase, searchable by `temp_id`.
- **Logs:** failures are logged with the job ID and client label; with `SENTRY_DSN` set they are also reported to Sentry.

## Known limitations

- **Single worker only.** Job state is in process memory, so a restart loses in-flight jobs and multiple workers won't work until this moves to Redis or the database.
- **PDF fidelity.** Conversion uses LibreOffice, so complex Word layouts (tables, text boxes, custom fonts) may not match Word exactly. Always check output before sending it on.
- **Sessions can't be revoked.** Tokens carry no server-side expiry or revocation.

## Testing

```bash
pytest
```

## License

Proprietary. Copyright 2026 Dodman Group. All rights reserved.