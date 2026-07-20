# Pinterest OAuth Token Setup Guide

This guide covers how to generate a fresh Pinterest access token and store it in the `_config` sheet. Follow these steps whenever the script throws a `401 Unauthorized` error, or when both the access token and refresh token have expired.

You should not need this often — the script auto-refreshes the access token every 30 days using the stored refresh token. The refresh token itself lasts 60 days. You only need this manual flow when:

- You are setting up the script for the first time
- The refresh token has expired (unused for 60+ days)
- The tokens in `_config` are stale placeholders (e.g. expiry shows a far-future date like `2027-01-01`)
- You rotated the app secret key on the Pinterest developer portal

---

## Prerequisites

Before starting, have the following ready:

| Item | Where to find it |
|---|---|
| **App ID** | `1576225` (fixed — this is the A.won Studio Publisher app) |
| **App secret key** | [developers.pinterest.com/apps/1576225](https://developers.pinterest.com/apps/1576225) → Configure tab → App secret key → click the eye icon |
| **Pinterest login** | Must be logged in as `@awonarchitects` in your browser |
| **`_config` sheet** | The Google Sheet linked to this project, `_config` tab, cells B1–B3 |

---

## Step 1 — Get an authorization code

Open this URL in your browser while logged in as `@awonarchitects`:

```
https://www.pinterest.com/oauth/?client_id=1576225&redirect_uri=https://localhost/&response_type=code&scope=boards:read,boards:write,pins:read,pins:write,user_accounts:read
```

Pinterest will show a permissions screen. Click **Give access**.

Your browser will redirect to `https://localhost/` and show a "connection refused" or "site can't be reached" error — **this is expected**. Nothing is running on localhost; the redirect is just the mechanism Pinterest uses to hand back the code.

Look at the address bar. It will look like:

```
https://localhost/?code=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX&state=...
```

Copy the value after `code=` and before `&state`. This is your **authorization code**. It is single-use and expires in approximately 5 minutes — proceed to Step 2 immediately.

---

## Step 2 — Exchange the code for tokens

Open **PowerShell** in the project directory and run the following command. Replace `YOUR_APP_SECRET` with the real secret key from Step 0, and `YOUR_AUTH_CODE` with the code you just copied:

```powershell
$cred = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("1576225:YOUR_APP_SECRET")); Invoke-RestMethod -Uri "https://api.pinterest.com/v5/oauth/token" -Method Post -Headers @{Authorization="Basic $cred"} -ContentType "application/x-www-form-urlencoded" -Body "grant_type=authorization_code&code=YOUR_AUTH_CODE&redirect_uri=https://localhost/" | ConvertTo-Json
```

A successful response looks like this:

```json
{
    "access_token":  "pina_XXXXXXXXXX...",
    "refresh_token":  "pinr.XXXXXXXXXX...",
    "response_type":  "authorization_code",
    "token_type":  "bearer",
    "expires_in":  2592000,
    "refresh_token_expires_in":  5184000,
    "scope":  "boards:read boards:write pins:read pins:write user_accounts:read",
    "refresh_token_expires_at":  XXXXXXXXXX
}
```

If you see an error like `"code": "INVALID_GRANT"`, the authorization code expired. Go back to Step 1 and get a fresh code, then re-run this command immediately.

---

## Step 3 — Calculate the token expiry datetime

`expires_in` is the access token lifetime in seconds (always `2592000` = 30 days). You need to calculate the actual expiry datetime in UTC ISO 8601 format to write into the sheet.

Run this in PowerShell to calculate it automatically:

```powershell
(Get-Date).ToUniversalTime().AddSeconds(2592000).ToString("yyyy-MM-ddTHH:mm:ssZ")
```

This outputs something like `2026-08-19T07:46:37Z`. Copy this value.

---

## Step 4 — Write the tokens into the `_config` sheet

Open the Google Sheet linked to this project. Go to the `_config` tab and update these three cells:

| Cell | Value |
|---|---|
| **B1** | The full `access_token` value from Step 2 (starts with `pina_`) |
| **B2** | The full `refresh_token` value from Step 2 (starts with `pinr.`) |
| **B3** | The expiry datetime from Step 3 (e.g. `2026-08-19T07:46:37Z`) |

**Important rules for B3:**
- Must be in exactly this format: `2026-08-19T07:46:37Z` (UTC, Z suffix, no spaces)
- Do not use a far-future placeholder like `2027-01-01T00:00:00Z` — this disables the auto-refresh logic silently

---

## Step 5 — Verify

Run the script:

```powershell
python main.py
```

You should see:

```
[INFO] Google authentication successful.
[INFO] Access token valid until 2026-08-19T07:46:37Z. Using stored token: pina_XXXXXX...
[INFO] Pinterest authentication successful.
[INFO] Authenticated as Pinterest user: awonarchitects
```

If you still see a `401` error, double-check that B1 in the sheet has no leading/trailing spaces, and that the full token was pasted (they are long strings).

---

## Token lifecycle reference

| Token | Lifetime | What happens at expiry |
|---|---|---|
| `access_token` | 30 days | Script auto-refreshes using `refresh_token` if expiry is within 24 hours |
| `refresh_token` | 60 days | Auto-refresh fails with 401 — must redo this full guide |

The script checks token expiry on every run. As long as `main.py` runs at least once every 60 days, tokens will rotate automatically and you will never need to repeat these steps.

---

## Security notes

- Never commit tokens to the GitHub repository
- Never share tokens in chat, email, or screenshots
- If a token is accidentally exposed, go to [developers.pinterest.com/apps/1576225](https://developers.pinterest.com/apps/1576225) → **Reset app secret**, then redo this entire guide with the new secret
- The app secret key lives in `.env` locally and in GitHub Actions secrets — if you reset the secret, update it in both places

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `401 Unauthorized` at startup | Stored access token is revoked or stale | Redo this guide |
| `401` during token refresh | Refresh token expired (60+ days unused) | Redo this guide |
| `403 Forbidden` when posting pins | Token has read-only scopes (e.g. generated via the "Generate token" button on the developer portal) | Redo this guide — the "Generate token" button does not issue write scopes; only the full OAuth flow does |
| `INVALID_GRANT` in Step 2 | Authorization code expired (>5 min) | Go back to Step 1 and get a fresh code |
| `400 — redirect URI does not match` | Wrong redirect URI in the Step 1 URL | Use exactly `https://localhost/` including the trailing slash |
| `B3` parse error in logs | Wrong datetime format in the sheet | Use `2026-08-19T07:46:37Z` — no `+00:00`, no spaces |
