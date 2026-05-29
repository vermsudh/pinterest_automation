# A.won Pinterest Automation

Automated Pinterest content uploader for the architectural brand A.won.

Reads image and video content from Google Drive and metadata from Google Sheets,
then uploads Pins to Pinterest via the v5 API. Triggered manually via GitHub Actions.

## Setup

See `System_Instruction/SYSTEM_INSTRUCTION.md` for the full specification and
`docs/pinterest_api_reference.md` for all Pinterest API endpoint details.

### 1. Configure secrets

Copy `.env.example` to `.env` and fill in all values for local development.

Add the same five secrets to your GitHub repository under Settings → Secrets:

| Secret | Description |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the Google Service Account JSON key file |
| `PINTEREST_CLIENT_ID` | Pinterest app Client ID |
| `PINTEREST_CLIENT_SECRET` | Pinterest app Client Secret |
| `GOOGLE_SHEET_ID` | ID of the A.won Pinterest Queue Google Sheet |
| `GOOGLE_DRIVE_FOLDER_ID` | ID of the root Pinterest Queue folder in Google Drive |

### 2. One-time Pinterest OAuth setup

Before the first run, perform the one-time authorization flow described in
Section 1.1 of `docs/pinterest_api_reference.md` to obtain an initial
`access_token` and `refresh_token`. Store these values in the `_config` tab
of the Google Sheet (cells B1, B2, B3). The script refreshes tokens automatically
on every subsequent run.

### 3. Prepare the Google Sheet

Create a sheet with two tabs:
- `Queue` — upload queue (columns A–K as specified in SYSTEM_INSTRUCTION.md Section 4)
- `_config` — token storage (B1: access_token, B2: refresh_token, B3: token_expiry)

### 4. Prepare Google Drive

The root folder (`GOOGLE_DRIVE_FOLDER_ID`) must contain three sub-folders:
- `Ready/` — place images and videos here before running
- `Posted/` — script moves files here after a successful Pin
- `Failed/` — script moves files here after all retries are exhausted

### 5. Run locally

```bash
pip install -r requirements.txt
python main.py
```

### 6. Run via GitHub Actions

Go to Actions → Pinterest Upload → Run workflow.
# pinterest_automation
