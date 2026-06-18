# CLAUDE.md

Quick-reference for Claude Code sessions working on this repo. Base changes on the
code, not on assumptions — when in doubt, read the file named in each section.

## 1. Project Overview

Automation pipeline that publishes Pins to **A.won**'s Pinterest account. A.won is a
concept architecture studio in New Delhi (founded by Abhishek RK Khanna) designing
luxury residences, farmhouses, villas, retreats, hotels, and resorts for HNI clients
across India and the GCC.

High-level flow on each run (`main.py`):

1. Authenticate with Google (service account) and Pinterest (OAuth refresh).
2. Read the **Queue** tab of a Google Sheet for `Pending` rows.
3. Sync any new files in the Drive `Ready/` folder into the Queue as new `Pending` rows.
4. Generate missing captions (title/description/alt_text) with **Google Gemini** from the image.
5. For each row, download the asset from Drive, create a Pin via the **Pinterest API v5**,
   write `Posted`/`Failed`/`Skipped` + pin_id + timestamp back to the Sheet, and move the
   Drive file to `Posted/` or `Failed/`.
6. Print a summary block and `sys.exit(1)` if any row failed (so CI marks the run red).

Orchestrated by a **GitHub Actions** workflow (`.github/workflows/upload.yml`),
triggered **manually only** (`workflow_dispatch` — no cron schedule).

> NOTE: `README.md` predates the Gemini + queue-sync features and still says "no
> scheduling or AI generation." The code is authoritative — captions ARE auto-generated
> when `GEMINI_API_KEY` is set, and new Drive files ARE auto-synced into the Queue.

## 2. Architecture Map

```
main.py                  Orchestrator. Thin top-to-bottom recipe; all logic lives below.
sync_queue.py            Appends new Ready/ files to the Queue tab as Pending rows.
                         Importable (sync_queue_with_clients) + standalone (python sync_queue.py).

config/settings.py       Loads all env vars + every project-wide constant. No hardcoded
                         strings elsewhere. PinterestAccount dataclass + load_awon_account().

auth/google_auth.py      Parses GOOGLE_SERVICE_ACCOUNT_JSON → scoped creds → Sheets/Drive clients.
auth/pinterest_auth.py   Pinterest OAuth token lifecycle (load/refresh/save to _config tab).

services/sheets_service.py     Reads Pending rows (PinRow dataclass); mark_posted/failed/skipped.
services/drive_service.py      Resolve subfolder IDs, list Ready/ files, download to memory, move files.
services/caption_generator.py  Gemini multimodal captioning; fill_missing_captions() writes back.
services/pinterest_client.py   All Pinterest v5 HTTP calls (PinterestClient + RetryableError).

uploaders/image_uploader.py    upload_image_pin(): full single-row image flow, returns bool.
uploaders/video_uploader.py    upload_video_pin(): 4-step video flow (register→S3→poll→pin), returns bool.

utils/logger.py          setup_logger() — root logger, UTC ISO-8601 timestamps, stdout only.
utils/retry.py           with_retry() decorator — exponential backoff on RetryableError.
utils/validators.py      validate_row() — local pre-flight field checks; mask_token().

Standalone helper scripts (run by hand, not part of the main run):
get_token.py             One-time interactive OAuth code→token exchange.
create_boards.py         One-time bulk board creation from a hardcoded BOARDS_TO_CREATE list.
test_caption.py          Generate+print captions for a local image (python test_caption.py img.jpg).
```

**Call direction:** `main.py` builds all clients once via `auth/`, then passes them down
into `services/` and `uploaders/`. No service or uploader builds its own client or
authenticates. `uploaders/` call `services/` (Pinterest, Drive, Sheets) + `utils/` (retry,
validators). Retry is applied **at the call site** in the uploaders (`with_retry(max_attempts=5)(client.method)`),
not as a decorator inside `pinterest_client.py`.

## 3. Key Conventions & Gotchas

### Environment variables / secrets
Required (script aborts at import or `load_awon_account()` if missing — see `config/settings.py`):
- `GOOGLE_SERVICE_ACCOUNT_JSON` — full JSON key file contents as a **single line, no internal newlines**.
- `PINTEREST_CLIENT_ID`, `PINTEREST_CLIENT_SECRET`
- `GOOGLE_SHEET_ID`, `GOOGLE_DRIVE_FOLDER_ID` (root folder, NOT the `Ready/` subfolder)

Optional (graceful skip / defaults):
- `GEMINI_API_KEY` — absent → caption generation is skipped entirely; captions must be filled manually.
- `PINTEREST_API_BASE_URL` — defaults to `https://api.pinterest.com/v5`.
- `PINTEREST_DESTINATION_URL` — default destination_link, defaults to `https://awon.world`.
- `PINTEREST_DEFAULT_BOARD` — board name for synced rows, defaults to `Modern Farmhouse Design India`.

Local dev reads these from `.env` (gitignored) via `python-dotenv`. In GitHub Actions they
come from repo Secrets; `load_dotenv()` is a silent no-op there.

### Google Sheet structure
- **`Queue`** tab — header in row 1, data from row 2. Columns:
  A image_filename · B media_type · C title · D description · E board_name ·
  F destination_link · G alt_text · H status · I pin_id · J posted_at · K error_message ·
  **L cover_image_url** (video rows only — outside core A–K schema, batch-read separately).
- **`_config`** tab — Pinterest tokens in column B: B1 access_token, B2 refresh_token,
  B3 token_expiry (ISO 8601 UTC, e.g. `2026-06-28T08:00:00Z`).
- Column indices/letters are defined as constants in `sheets_service.py` — a schema change
  should only edit that file. Tab names come from `settings.py`.
- The Sheets API omits trailing empty cells; all reads go through `_get_cell()`/`_read_cell()`
  which return `""` for absent columns rather than raising.

### Google Drive structure
Root `GOOGLE_DRIVE_FOLDER_ID` must contain three subfolders named **exactly** (case-sensitive):
`Ready/`, `Posted/`, `Failed/`. Missing folders raise `RuntimeError` at startup.
Files are **moved** (single `files.update` changing parents), never copied+deleted.
`image_filename` in column A must match the Drive filename **exactly** (extension + capitalisation).

### Pinterest API quirks (`services/pinterest_client.py`, `auth/pinterest_auth.py`)
- Status mapping in `_handle_response()`: 2xx→JSON (204→`{}`); 400/401/403/404→`RuntimeError`
  (do NOT retry); **429/500/503→`RetryableError`** (retried by `with_retry`).
- **Token refresh returns a NEW refresh_token** that invalidates the old one — both
  access_token AND refresh_token must be saved back to `_config` every refresh. Refresh
  is triggered proactively when expiry is within `TOKEN_REFRESH_THRESHOLD_HOURS` (24h).
- Refresh tokens expire after 60 days unused (or on password change) → 401 → re-run OAuth.
- Rate limiting: after each write, `_check_rate_limit()` reads `X-RateLimit-Remaining`;
  if `<= RATE_LIMIT_BUFFER` (5), sleeps until `X-RateLimit-Reset`.
- **Video upload is 4 steps**: `POST /v5/media` (register) → POST to AWS S3 pre-signed URL
  (**no Pinterest Authorization header** — uses a separate Session; S3 expects HTTP 204) →
  poll `GET /v5/media/{id}` until `status=="succeeded"` (timeout = 60 × 5s = 5 min) →
  `POST /v5/pins` with `source_type=video_id`. The S3 upload is **single-use, never retried**.
- Video Pins **require a non-empty `cover_image_url`** (column L) — empty causes HTTP 400.
  Must be a public CDN/web URL; Google Drive share links do NOT work.
- Image Pins use `source_type=image_base64` with raw base64 (no `data:` prefix).
- Tokens are never logged in full — only first 12 chars via `mask_token()`.

### Error handling & retries
- `utils/retry.py`: 5 attempts total, backoff `2,4,8,16`s, catches `RetryableError` only;
  everything else propagates immediately.
- Uploaders **never re-raise** — they catch all exceptions, `mark_failed()`, move the file
  to `Failed/`, and return `False`. One bad row never aborts the batch.
- Validation failures and missing-Drive-file failures `mark_failed()` but do **not** move
  the Drive file (the file may not exist).
- `main.py` setup phase (steps 1–7) aborts the whole run on any exception; the row loop does not.

### Logging
- `utils/logger.py`: format `[<UTC ISO8601>] [<LEVEL padded 8>] [<module>] message`, stdout only
  (GitHub Actions captures it). `setup_logger(__name__)` is idempotent; call it once per module.
- Per-row log convention: `Row %d | %s | ...` and `Row %d | %s → Posted/Failed | ...`.

## 4. Commands

```bash
# Local run (reads .env)
python main.py

# Sync new Drive files into the Queue only (also runs inside main.py)
python sync_queue.py

# One-time: interactive OAuth to mint the first token pair
python get_token.py

# One-time: create the production boards
python create_boards.py

# Manually test caption generation against a local image
python test_caption.py path/to/image.jpg
```

- Python **3.11+** required. Deps: `pip install -r requirements.txt`.
- **No automated test suite exists.** `test_caption.py` is a manual smoke script, not pytest.
- GitHub Actions: trigger via **Actions → Pinterest Upload → Run workflow** (manual
  `workflow_dispatch` only; pins Python 3.11, runs `python main.py`).

## 5. Coding Style / Patterns

- Module docstrings are thorough; public functions have full Google-style docstrings with
  Args/Returns/Raises. Match this density when adding code.
- `from __future__ import annotations` + modern typing (`str | None`, `dict[str, str]`).
  Google API clients are typed `Any` (dynamic library).
- All shared literals live in `config/settings.py` as module constants — never hardcode a
  URL, tab name, folder name, or tuning number elsewhere; import it.
- Private helpers are `_prefixed`; broad catches use `except Exception as exc:  # noqa: BLE001`.
- **Adding a new uploader** (e.g. a new media type): mirror `image_uploader.py` — take the
  same client/folder args, validate first, resolve file_id from the pre-fetched `drive_files`
  map, wrap Pinterest calls with `with_retry`, `mark_posted`+`move_to_posted` on success,
  `mark_failed`+`move_to_failed` on failure, return `bool`, never re-raise. Route it from the
  `media_type` branch in `main.py`.
- **Adding a new service**: take already-built clients as args (don't authenticate inside);
  keep it a pure library function where possible (`sync_queue_with_clients` is the model —
  no `sys.exit`, no auth).
- **Multi-account support** is designed-for: add a `load_<brand>_account()` returning a
  `PinterestAccount` — no other module should need changes.

## 6. Things to NOT Do

- **Never commit secrets** — `.env`, `service_account.json`, `*credentials*.json`,
  `*secret*.json`, `token.json`, `tokens.json` are gitignored. Keep it that way.
  (`tokens.json` exists locally and is untracked — do not add it.)
- **Don't log full tokens** — always go through `mask_token()` / the masked-prefix pattern.
- **Don't save only the access_token** after a refresh — the new refresh_token MUST be
  written back too, or the next run 401s.
- **Don't retry the S3 video upload** — pre-signed URLs are single-use.
- **Don't assume board names** — column E / `PINTEREST_DEFAULT_BOARD` must match an existing
  Pinterest board name **character-for-character, case-sensitive**. Note the default in
  `settings.py` (`Modern Farmhouse Design India`) does NOT match the names in
  `create_boards.py` (which use em-dashes, e.g. `Modern Farmhouse Design — New Delhi`) —
  verify the live board list before relying on a default.
- **Don't add retry for 400/403/404** — these are deliberately non-retryable `RuntimeError`s.
- **Don't break the run summary format** in `main.py` — downstream/CI reads it from stdout.
- **Don't paste `GOOGLE_SERVICE_ACCOUNT_JSON` with internal newlines** — it must be one line.
- TODO: `requirements.txt` contains duplicated lines and unpinned `google-genai`/`Pillow` —
  clean up / pin if you touch dependencies.
- TODO: `config/settings.py` defines `GEMINI_API_KEY` twice (top and bottom of file) — the
  second assignment is redundant; consolidate if editing that file.
```
