# A.won Pinterest Automation

## 1. Project Overview

This script automates publishing content to A.won's Pinterest account. On each run it reads a queue of pending Pins from a Google Sheet, downloads the corresponding image or video files from Google Drive, and uploads them to Pinterest via the v5 API. After each upload it writes the result (Posted, Failed, or Skipped) and the Pinterest Pin ID back to the Sheet, and moves the media file to a `Posted/` or `Failed/` sub-folder in Drive. The script is triggered manually via the GitHub Actions `workflow_dispatch` button — no scheduling or AI generation is involved. All content (titles, descriptions, board names, destination links) must be filled in the Sheet before the run starts.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Earlier versions are not supported |
| Google Cloud project | Free tier is sufficient |
| Google Sheets API | Must be enabled in the Cloud project |
| Google Drive API | Must be enabled in the Cloud project |
| Google Service Account | With a downloaded JSON key file |
| Pinterest developer account | At [developers.pinterest.com](https://developers.pinterest.com) |
| Pinterest app | With `boards:read`, `boards:write`, `pins:read`, `pins:write` scopes |

---

## 3. First-Time Setup

### Step 1 — Clone the repository

```bash
git clone <your-repo-url>
cd pinterest-automation
```

### Step 2 — Create and activate a virtual environment

```bash
python3.11 -m venv venv
source venv/bin/activate          # macOS / Linux
# venv\Scripts\activate           # Windows
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Create a Google Cloud project and enable APIs

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (or select an existing one).
2. In **APIs & Services → Library**, search for and enable both:
   - **Google Sheets API**
   - **Google Drive API**

### Step 5 — Create a Service Account and download the JSON key

1. In **APIs & Services → Credentials**, click **Create Credentials → Service Account**.
2. Give it a descriptive name (e.g. `pinterest-automation`), click through to **Done**.
3. Click the new service account row, open the **Keys** tab, and click **Add Key → Create new key → JSON**.
4. A `.json` file downloads automatically. Keep it safe — you will need its full contents as a secret in Steps 10 and 11.

> This file grants access to any Google resource the service account is shared with. Never commit it to version control.

### Step 6 — Share the Sheet and Drive folder with the service account

The service account has an email address like `pinterest-automation@your-project.iam.gserviceaccount.com`. Find it in the Cloud Console under **IAM & Admin → Service Accounts**. Grant it access to:

1. **Google Sheet** — open the sheet, click **Share**, add the service account email with **Editor** access.
2. **Google Drive root folder** (`Pinterest Queue/`) — right-click the folder in Drive, click **Share**, add the service account email with **Editor** access. Sub-folders (`Ready/`, `Posted/`, `Failed/`) inherit access automatically.

### Step 7 — Create a Pinterest developer app

1. Go to [developers.pinterest.com/apps](https://developers.pinterest.com/apps) and click **Create app**.
2. Fill in the app name and description, then save.
3. Under **Permissions / Scopes**, enable: `boards:read`, `boards:write`, `pins:read`, `pins:write`.
4. Add a redirect URI (e.g. `https://localhost/callback`) — used only during the one-time OAuth flow below.
5. Note your **App ID** (`PINTEREST_CLIENT_ID`) and **App Secret** (`PINTEREST_CLIENT_SECRET`).

### Step 8 — Run the one-time Pinterest OAuth flow

Pinterest requires a manual browser authorisation the first time to obtain a `refresh_token`. This only needs to happen once — the script refreshes the access token automatically on every subsequent run.

**Part A — Open this URL in your browser** (fill in your own values):

```
https://www.pinterest.com/oauth/
  ?client_id=YOUR_APP_ID
  &redirect_uri=https://localhost/callback
  &response_type=code
  &scope=boards:read,boards:write,pins:read,pins:write
  &state=setup
```

Log in with A.won's Pinterest account if prompted, then click **Allow**.

**Part B — Copy the authorisation code**

Pinterest redirects to your redirect URI with a `code` parameter:

```
https://localhost/callback?code=AUTHORISATION_CODE&state=setup
```

Copy the value of `code`. (The browser will show an error page because `localhost` isn't running anything — that's fine, just copy the URL.)

**Part C — Exchange the code for tokens**

Run this command, substituting your values:

```bash
curl -X POST https://api.pinterest.com/v5/oauth/token \
  -H "Authorization: Basic $(echo -n 'CLIENT_ID:CLIENT_SECRET' | base64)" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=AUTHORISATION_CODE&redirect_uri=https://localhost/callback"
```

The JSON response contains `access_token`, `refresh_token`, and `expires_in` (seconds). Calculate the expiry time by adding `expires_in` seconds to the current UTC time and format it as ISO 8601 — for example: `2026-06-28T08:00:00Z`.

### Step 9 — Store tokens in the `_config` Sheet tab

In the Google Sheet, create a tab named exactly **`_config`** (right-click any tab → Rename). Enter the three values in column B:

| Cell | Value |
|---|---|
| `_config!B1` | The `access_token` from Part C above |
| `_config!B2` | The `refresh_token` from Part C above |
| `_config!B3` | Token expiry as ISO 8601 UTC, e.g. `2026-06-28T08:00:00Z` |

> The script updates all three cells automatically whenever it refreshes the token. Do not edit these cells while a run is in progress.

### Step 10 — Create the `.env` file for local development

```bash
cp .env.example .env
```

Open `.env` and fill in all five values:

```env
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"...full JSON contents..."}
PINTEREST_CLIENT_ID=your_app_id
PINTEREST_CLIENT_SECRET=your_app_secret
GOOGLE_SHEET_ID=the_long_id_from_the_sheets_url
GOOGLE_DRIVE_FOLDER_ID=the_folder_id_from_the_drive_url
```

For `GOOGLE_SERVICE_ACCOUNT_JSON`, paste the **entire contents** of the JSON key file as a single line with no extra newlines inside the value.

- `GOOGLE_SHEET_ID` is the string between `/d/` and `/edit` in the Sheet URL.
- `GOOGLE_DRIVE_FOLDER_ID` is the string after `/folders/` in the Drive folder URL.

> `.env` is listed in `.gitignore`. Never commit it.

### Step 11 — Add secrets to GitHub

In your repository go to **Settings → Secrets and variables → Actions** and add each secret below. Names must match exactly (they are case-sensitive):

| Secret name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the service account `.json` key file |
| `PINTEREST_CLIENT_ID` | Pinterest app ID |
| `PINTEREST_CLIENT_SECRET` | Pinterest app secret |
| `GOOGLE_SHEET_ID` | The ID portion of the Google Sheets URL |
| `GOOGLE_DRIVE_FOLDER_ID` | The ID of the root `Pinterest Queue` Drive folder |

---

## 4. Google Sheet Setup

### Queue tab

Create a tab named exactly **`Queue`**. Row 1 is the header row. Add these column headers:

| Column | Header | Set by | Description |
|---|---|---|---|
| A | `image_filename` | Operator | Exact filename in the `Ready/` folder, including extension and capitalisation |
| B | `media_type` | Operator | `image` or `video` |
| C | `title` | Operator | Pin title. Max 100 characters. |
| D | `description` | Operator | Pin description. Max 500 characters. Hashtags supported. |
| E | `board_name` | Operator | Board name exactly as it appears on Pinterest (case-sensitive) |
| F | `destination_link` | Operator | URL users land on when clicking the Pin. Must start with `https://`. |
| G | `alt_text` | Operator | Accessibility description. Max 500 characters. |
| H | `status` | Both | Operator sets `Pending`. Script writes `Posted`, `Failed`, or `Skipped`. |
| I | `pin_id` | Script | Pinterest Pin ID, written after a successful post. |
| J | `posted_at` | Script | ISO 8601 UTC timestamp, written after a successful post. |
| K | `error_message` | Script | Human-readable error message, written when status is `Failed`. |
| L | `cover_image_url` | Operator | *(Video rows only)* Public thumbnail URL. Required by Pinterest for video Pins. |

**To add a new Pin to the queue:** fill in columns A through H, set column H to `Pending`, and leave I, J, and K blank. Upload the media file to `Ready/` in Drive before running.

### `_config` tab

Create a tab named exactly **`_config`**. This tab stores Pinterest tokens — do not rename or delete it.

| Cell | Contents |
|---|---|
| `B1` | Pinterest access token (starts with `pina_`) |
| `B2` | Pinterest refresh token (starts with `pinr_`) |
| `B3` | Token expiry in ISO 8601 UTC, e.g. `2026-06-28T08:00:00Z` |

---

## 5. Google Drive Setup

Create the following folder structure. The root folder name can be anything you like; the three sub-folders must be named **exactly** as shown (including capitalisation):

```
Pinterest Queue/          ← Root folder (use its ID as GOOGLE_DRIVE_FOLDER_ID)
    Ready/                ← Place media files here before each run
    Posted/               ← Script moves files here after a successful Pin
    Failed/               ← Script moves files here after all retries fail
```

**Before each run:** upload all images and videos to `Ready/`. The filename in column A of the Sheet must match the Drive filename exactly — including extension and capitalisation (e.g. `khan-living-room.jpg` is not the same as `Khan-Living-Room.JPG`).

Files are **moved**, not copied. After a successful run, find posted files in `Posted/` and files that could not be posted in `Failed/`.

---

## 6. Running the Script Locally

```bash
# Activate your virtual environment
source venv/bin/activate

# Run
python main.py
```

Credentials are loaded from `.env` automatically. All log output goes to stdout. A structured summary block is printed at the end of every run:

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

The process exits with code `0` if every row was posted or skipped, and `1` if any rows failed.

---

## 7. Running via GitHub Actions

1. Open the repository on GitHub.
2. Click the **Actions** tab.
3. In the left sidebar, click **Pinterest Upload**.
4. Click **Run workflow → Run workflow**.

The run appears in the Actions list within a few seconds. Click it to see live log output. A red ✗ next to the step means at least one row failed — check the log for `→ Failed` lines, or open the Sheet and read column K for the exact error on each failed row.

GitHub Actions injects all secrets automatically from **Settings → Secrets**. No environment variables need to be set manually for Actions runs.

---

## 8. Troubleshooting

### Token expired (HTTP 401)

**Symptom:** The script fails during setup with a message containing `HTTP 401 Unauthorized` or `Pinterest refresh token has expired`.

**Cause:** The refresh token stored in `_config!B2` has expired. Refresh tokens expire if they are not used within 60 days, or if the Pinterest account password was changed.

**Fix:** Re-run the one-time OAuth flow from Step 8 to obtain a fresh pair of tokens. Update `_config!B1`, `_config!B2`, and `_config!B3` with the new values, then re-run the script.

---

### Board name not found

**Symptom:** Rows fail with `board_name 'XYZ' not found in board map`.

**Cause:** The value in column E does not exactly match any board name on the authenticated Pinterest account. Pinterest board names are case-sensitive and must include any punctuation or special characters exactly as they appear.

**Fix:** Log into Pinterest, open A.won's profile, and copy the board name character-for-character into column E of the Sheet. The script logs all resolved board names at startup (`Found N board(s): ...`) — use that line to verify what names are available during a run.

---

### File not found in `Ready/`

**Symptom:** Rows fail with `File 'xyz.jpg' was not found in the Ready/ folder`.

**Cause:** The filename in column A does not match any file currently in the `Ready/` Drive folder. Common causes: the file was never uploaded, it was already processed in a previous run (and moved to `Posted/` or `Failed/`), or there is a capitalisation mismatch.

**Fix:**
- Confirm the file exists in `Ready/` and not in `Posted/` or `Failed/`.
- Compare the filename in the Sheet to the exact filename in Drive — they must match character-for-character including extension.
- If a previous run moved the file to `Failed/`, move it back to `Ready/`, reset column H to `Pending`, clear columns I–K, and re-run.

---

### Cover image URL missing for video Pins

**Symptom:** Video rows fail with `HTTP 400 Bad Request`. The log contains a warning about no cover image URL being provided.

**Cause:** Column L (`cover_image_url`) is empty for a video row. Pinterest requires a publicly accessible thumbnail image URL for all video Pins — an empty value causes the API to return HTTP 400.

**Fix:** Add a valid public image URL to column L for each video row. The URL must be directly reachable by Pinterest's servers. Google Drive sharing links do not work — host the thumbnail on a CDN, an image hosting service, or any public web server, and paste the direct image URL into column L.

---

### Service account not shared with the Drive folder or Sheet

**Symptom:** The script fails during setup with a Google API error such as `The caller does not have permission`, `File not found`, or a 403 response from the Sheets or Drive API.

**Cause:** The service account has not been granted Editor access to the Google Sheet and/or the root `Pinterest Queue` Drive folder.

**Fix:**
1. Find the service account's email in Google Cloud Console → **IAM & Admin → Service Accounts** (format: `name@project-id.iam.gserviceaccount.com`).
2. Open the Google Sheet → **Share** → add the service account email → **Editor**.
3. Open the `Pinterest Queue` folder in Drive → right-click → **Share** → add the service account email → **Editor**.
4. Re-run the script. Drive sub-folder access is inherited from the parent — you do not need to share `Ready/`, `Posted/`, or `Failed/` individually.
