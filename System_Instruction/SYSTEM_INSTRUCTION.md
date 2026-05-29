# System Instruction — A.won Pinterest Automation Script
**Project:** Automated Pinterest content uploader for the architectural brand A.won  
**Language:** Python 3.11+  
**Trigger:** GitHub Actions manual dispatch (`workflow_dispatch`)  
**Last updated:** May 2026

---

## 1. Project Overview

You are building a Python automation script that reads content from A.won's Google Drive and Google Sheets, then uploads that content as Pins to A.won's Pinterest account via the Pinterest API v5.

The script replaces a manual workflow where team members log into Pinterest and upload images and videos one by one. It must be robust, resumable, and safe to re-run without double-posting.

---

## 2. Architecture Overview

```
GitHub Actions (manual trigger)
        │
        ▼
  main.py (entrypoint)
        │
        ├── auth/
        │     ├── google_auth.py       # Google Service Account authentication
        │     └── pinterest_auth.py    # Pinterest OAuth token management
        │
        ├── services/
        │     ├── sheets_service.py    # Read queue + write status back to Google Sheet
        │     ├── drive_service.py     # Download images/videos from Google Drive
        │     └── pinterest_client.py  # All Pinterest API v5 calls
        │
        ├── uploaders/
        │     ├── image_uploader.py    # Handle image Pin creation (base64 method)
        │     └── video_uploader.py    # Handle 3-step video Pin creation
        │
        ├── utils/
        │     ├── logger.py            # Structured logging
        │     ├── retry.py             # Exponential backoff decorator
        │     └── validators.py        # Field length checks before API calls
        │
        ├── config/
        │     └── settings.py          # Load all env vars and constants
        │
        ├── docs/
        │     └── pinterest_api_reference.md   # API reference (read this before any API work)
        │
        ├── .github/
        │     └── workflows/
        │           └── upload.yml     # GitHub Actions workflow
        │
        ├── .env.example               # Template for local development
        ├── requirements.txt
        └── README.md
```

---

## 3. Environment & Secrets

### GitHub Secrets (used in GitHub Actions)
These are never hardcoded. Always read from environment variables.

| Secret Name | Description |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the Google Service Account JSON key file |
| `PINTEREST_CLIENT_ID` | Pinterest app Client ID |
| `PINTEREST_CLIENT_SECRET` | Pinterest app Client Secret |
| `GOOGLE_SHEET_ID` | ID of the A.won Pinterest Queue Google Sheet |
| `GOOGLE_DRIVE_FOLDER_ID` | ID of the `Ready/` folder in Google Drive |

### Token Persistence (Critical)
Pinterest tokens are stored in a dedicated hidden tab called `_config` in the same Google Sheet. The script reads and writes tokens here on every run.

| Sheet Cell | Value stored |
|---|---|
| `_config!B1` | `access_token` |
| `_config!B2` | `refresh_token` |
| `_config!B3` | `token_expiry` (ISO 8601 UTC string) |

On startup, the script must:
1. Read these three values from the Sheet
2. Check if `token_expiry` is within 24 hours of now
3. If yes, call Pinterest refresh endpoint and write new values back to `_config`
4. If refresh fails with 401, halt immediately and print a clear message instructing the operator to re-run the one-time OAuth setup

### Local Development
A `.env` file is used locally (loaded via `python-dotenv`). A `.env.example` must be committed to the repo showing all required keys with placeholder values. The `.env` file itself is always in `.gitignore`.

---

## 4. Google Sheet Structure

### Main Queue Tab — `Queue`

The script reads rows where `status` is `Pending` and processes them in order.

| Column | Header | Description |
|---|---|---|
| A | `image_filename` | Exact filename as it appears in Google Drive `Ready/` folder |
| B | `media_type` | `image` or `video` |
| C | `title` | Pin title. Max 100 characters. |
| D | `description` | Pin description. Max 500 characters. Hashtags supported. |
| E | `board_name` | Human-readable board name (e.g. `Interiors`). Script resolves to `board_id`. |
| F | `destination_link` | URL users land on when they click the Pin. Must start with `https://`. |
| G | `alt_text` | Accessibility description. Max 500 characters. |
| H | `status` | Script reads: `Pending`. Script writes: `Posted`, `Failed`, `Skipped`. |
| I | `pin_id` | Written by script after successful post. |
| J | `posted_at` | ISO 8601 UTC timestamp written by script after successful post. |
| K | `error_message` | Written by script when status is `Failed`. Human-readable error. |

### Config Tab — `_config`
Hidden tab. Contains Pinterest token storage (see Section 3).

---

## 5. Google Drive Structure

```
📁 Pinterest Queue/              ← Root folder (GOOGLE_DRIVE_FOLDER_ID points here)
    📁 Ready/                    ← Script reads images/videos from here
    📁 Posted/                   ← Script moves files here after successful Pin creation
    📁 Failed/                   ← Script moves files here after all retries exhausted
```

The script must move files between folders — not copy and delete — using the Google Drive API `files.update` method to change the parent folder ID.

---

## 6. Pinterest API Behaviour

**Reference file:** Always read `docs/pinterest_api_reference.md` before writing any Pinterest API call. Do not guess endpoint schemas.

### Image Pins
- Method: `POST /v5/pins` with `media_source.source_type: "image_base64"`
- Images are downloaded from Drive to memory (not disk) and base64-encoded before upload
- Do not write temporary files to disk

### Video Pins
- 4-step process: Register intent → Upload to AWS S3 → Poll status → Create Pin
- Poll every 5 seconds, timeout after 5 minutes (60 attempts)
- A cover image URL is required. Use the Google Drive direct-download URL of a companion thumbnail file if available, otherwise leave the field as the best available fallback
- Video files may be large — stream downloads from Drive rather than loading into memory

### Board Resolution
- On startup, call `GET /v5/boards` (paginated, max page_size=250) and build a dict: `{"Interiors": "549755885175", ...}`
- Cache this for the duration of the run
- If a row's `board_name` does not match any board, mark that row `Failed` with a clear error message — do not create a new board automatically

### Rate Limits
- After each write request, read `X-RateLimit-Remaining` from the response headers
- If `X-RateLimit-Remaining <= 5`, sleep until the Unix timestamp in `X-RateLimit-Reset`
- Log the sleep clearly: `"Rate limit nearly exhausted. Sleeping until {reset_time}."`

---

## 7. Retry & Error Handling

### Retry Policy
Apply exponential backoff **only** to HTTP 429, 500, and 503 responses.

```
Attempt 1: immediate
Attempt 2: wait 2s
Attempt 3: wait 4s
Attempt 4: wait 8s
Attempt 5: wait 16s
→ After 5 failed attempts: mark row Failed, log error, continue to next row
```

Implement this as a reusable decorator in `utils/retry.py`.

### Per-Row Failure Behaviour
- A single row failing must **never** stop the batch
- On failure: write `Failed` to column H, write the error message to column K, move the Drive file to `Failed/` folder
- On success: write `Posted` to column H, write Pin ID to column I, write timestamp to column J, move the Drive file to `Posted/` folder
- Log both outcomes with the filename and row number

### Do Not Retry
- HTTP 400 (bad request — configuration issue, retrying won't help)
- HTTP 403 (permissions — retrying won't help)
- HTTP 404 (resource not found — board doesn't exist)

### Validation Before API Calls
Before calling the Pinterest API for any row, validate locally in `utils/validators.py`:
- `title` length ≤ 100 characters
- `description` length ≤ 500 characters
- `alt_text` length ≤ 500 characters
- `destination_link` starts with `https://`
- `board_name` exists in the board map
- `media_type` is either `image` or `video`
- `image_filename` is not empty

If any validation fails, mark the row `Failed` with a descriptive message and skip — do not call the API.

---

## 8. Logging

Use Python's built-in `logging` module. Format every log line with:
```
[TIMESTAMP] [LEVEL] [MODULE] message
```

Example:
```
[2026-05-29T08:00:01Z] [INFO]  [main] Starting A.won Pinterest upload run
[2026-05-29T08:00:02Z] [INFO]  [pinterest_auth] Access token valid until 2026-06-28
[2026-05-29T08:00:03Z] [INFO]  [sheets_service] Found 12 rows with status Pending
[2026-05-29T08:00:05Z] [INFO]  [image_uploader] Row 2 | khan-living-room.jpg → Posted | Pin ID: 654321654321
[2026-05-29T08:00:06Z] [WARNING] [image_uploader] Row 3 | office-tower-facade.jpg → Failed | Error: board_name 'Facades' not found in board map
[2026-05-29T08:01:00Z] [INFO]  [main] Run complete. Posted: 10 | Failed: 2 | Skipped: 0
```

Print a **summary block** at the end of every run:
```
============================
A.won Pinterest Upload — Run Summary
============================
Total rows processed : 12
Successfully posted  : 10
Failed               : 2
Skipped              : 0
Run duration         : 00:01:42
============================
```

GitHub Actions captures stdout/stderr — no separate log file needed.

---

## 9. GitHub Actions Workflow

File: `.github/workflows/upload.yml`

```yaml
name: Pinterest Upload

on:
  workflow_dispatch:    # Manual trigger — button in GitHub Actions UI

jobs:
  upload:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run upload script
        env:
          GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          PINTEREST_CLIENT_ID: ${{ secrets.PINTEREST_CLIENT_ID }}
          PINTEREST_CLIENT_SECRET: ${{ secrets.PINTEREST_CLIENT_SECRET }}
          GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
          GOOGLE_DRIVE_FOLDER_ID: ${{ secrets.GOOGLE_DRIVE_FOLDER_ID }}
        run: python main.py
```

---

## 10. Dependencies — requirements.txt

```
google-api-python-client==2.131.0
google-auth==2.29.0
google-auth-httplib2==0.2.0
requests==2.31.0
python-dotenv==1.0.1
```

No Pinterest SDK. All Pinterest API calls are made using `requests` directly against the v5 REST endpoints documented in `docs/pinterest_api_reference.md`.

---

## 11. Multi-Account Design (Future-Ready)

Although only A.won's account is active in v1, the script must be designed so a second account can be added by:
1. Adding a new set of secrets with an account prefix (e.g. `BRAND2_PINTEREST_CLIENT_ID`)
2. Adding a new `_config` tab row set or a second Sheet
3. Passing an `account_profile` string to the main entrypoint

Do not implement multi-account switching in v1 — just ensure the code is not hardwired in a way that makes it impossible to add later. Use a `PinterestAccount` dataclass or similar to group account-specific config.

---

## 12. Code Quality Standards

- All functions must have docstrings
- Type hints on all function signatures
- No hardcoded strings — all constants go in `config/settings.py`
- No credentials ever in code or logs — mask token strings in log output (show only first 8 characters: `pina_xxxx...`)
- Each module handles one responsibility only
- `main.py` should be readable top-to-bottom like a recipe — it orchestrates, it does not implement

---

## 13. What This Script Does NOT Do

- It does not schedule posts at specific times (GitHub Actions manual trigger handles timing)
- It does not generate captions or titles using AI (content comes from the Sheet)
- It does not create Pinterest boards automatically (boards must exist before running)
- It does not handle Pinterest analytics or reporting
- It does not support carousel/collection Pins in v1 (image and video only)
- It does not send notifications or emails on completion in v1

---

## 14. Definition of Done

The script is complete when:
- [ ] A full batch of image Pins posts successfully from Drive → Pinterest with status written back to Sheet
- [ ] A full batch of video Pins posts successfully through the 4-step upload flow
- [ ] Token refresh works automatically without operator intervention
- [ ] A row that fails does not stop the rest of the batch
- [ ] Files move from `Ready/` to `Posted/` or `Failed/` correctly in Drive
- [ ] The GitHub Actions workflow runs end-to-end on a push to `main`
- [ ] `.env.example` and `README.md` contain clear setup instructions for a new developer
- [ ] No credentials appear in any log output

---

*End of System Instruction*
