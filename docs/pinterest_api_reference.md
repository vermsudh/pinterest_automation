# Pinterest API v5 — Curated Reference for A.won Automation
**Source:** https://developers.pinterest.com/docs/api/v5/  
**OpenAPI Spec:** https://github.com/pinterest/api-description  
**Base URL (Production):** `https://api.pinterest.com/v5`  
**Base URL (Sandbox):** `https://api-sandbox.pinterest.com/v5`  
**Spec Version:** 5.23.0  
**Last reviewed:** May 2026

> This file is intentionally scoped to the endpoints used by the A.won Pinterest automation script.
> It covers: Authentication, Boards, Pins (image + video), and Media Upload.
> For future features (analytics, ads, catalogs) refer to the full OpenAPI spec linked above.

---

## Table of Contents

1. [Authentication & Token Management](#1-authentication--token-management)
   - 1.1 [Generate Access Token (Authorization Code)](#11-generate-access-token--authorization-code-grant)
   - 1.2 [Refresh Access Token](#12-refresh-access-token)
   - 1.3 [Token Behaviour & Expiry Rules](#13-token-behaviour--expiry-rules)
2. [User Account](#2-user-account)
   - 2.1 [Get User Account](#21-get-user-account)
3. [Boards](#3-boards)
   - 3.1 [List Boards](#31-list-boards)
   - 3.2 [Get Board](#32-get-board)
   - 3.3 [Create Board](#33-create-board)
4. [Pins — Image](#4-pins--image)
   - 4.1 [Create Image Pin (via URL)](#41-create-image-pin-via-url)
   - 4.2 [Create Image Pin (via Base64)](#42-create-image-pin-via-base64)
   - 4.3 [Get Pin](#43-get-pin)
   - 4.4 [Delete Pin](#44-delete-pin)
5. [Pins — Video (3-Step Flow)](#5-pins--video-3-step-flow)
   - 5.1 [Step 1 — Register Video Upload Intent](#51-step-1--register-video-upload-intent)
   - 5.2 [Step 2 — Upload Video to AWS S3](#52-step-2--upload-video-to-aws-s3)
   - 5.3 [Step 3 — Poll Upload Status](#53-step-3--poll-upload-status)
   - 5.4 [Step 4 — Create Video Pin](#54-step-4--create-video-pin)
6. [Rate Limits](#6-rate-limits)
7. [Error Codes & Retry Strategy](#7-error-codes--retry-strategy)
8. [Required OAuth Scopes Summary](#8-required-oauth-scopes-summary)
9. [Field Constraints](#9-field-constraints)

---

## 1. Authentication & Token Management

Pinterest uses **OAuth 2.0**. All API requests must include a valid Bearer token in the Authorization header.

```
Authorization: Bearer {access_token}
```

### 1.1 Generate Access Token — Authorization Code Grant

This is a **one-time manual step** performed during initial setup to obtain the first refresh token.
After this, all subsequent token refreshes are automated by the script.

**Step A — Redirect user to Pinterest OAuth page (browser)**

```
GET https://www.pinterest.com/oauth/
  ?client_id={YOUR_APP_ID}
  &redirect_uri={YOUR_REDIRECT_URI}
  &response_type=code
  &scope=boards:read,boards:write,pins:read,pins:write
  &state={RANDOM_STRING_FOR_CSRF}
```

Pinterest redirects back to your `redirect_uri` with:
```
https://your-redirect-uri/?code={AUTHORIZATION_CODE}&state={YOUR_STATE}
```

**Step B — Exchange code for tokens**

```
POST https://api.pinterest.com/v5/oauth/token
```

**Headers:**
```
Authorization: Basic {base64(client_id:client_secret)}
Content-Type: application/x-www-form-urlencoded
```

**Body (form-encoded):**
```
grant_type=authorization_code
code={AUTHORIZATION_CODE_FROM_STEP_A}
redirect_uri={YOUR_REDIRECT_URI}
```

**Success Response — HTTP 200:**
```json
{
  "access_token": "pina_...",
  "refresh_token": "pinr_...",
  "response_type": "authorization_code",
  "token_type": "bearer",
  "expires_in": 2592000,
  "refresh_token_expires_in": 5184000,
  "scope": "boards:read boards:write pins:read pins:write"
}
```

| Field | Description |
|---|---|
| `access_token` | Bearer token for API calls. Prefix: `pina_`. Expires in 30 days. |
| `refresh_token` | Used to get a new access token. Prefix: `pinr_`. Expires in 60 days if unused. |
| `expires_in` | Access token lifetime in seconds (2592000 = 30 days). |
| `refresh_token_expires_in` | Refresh token lifetime in seconds (5184000 = 60 days). |

---

### 1.2 Refresh Access Token

The script calls this **automatically** at the start of every run if the access token is expired or within 24 hours of expiry. The new refresh token returned **must be saved** — it replaces the old one.

```
POST https://api.pinterest.com/v5/oauth/token
```

**Headers:**
```
Authorization: Basic {base64(client_id:client_secret)}
Content-Type: application/x-www-form-urlencoded
```

**Body (form-encoded):**
```
grant_type=refresh_token
refresh_token={STORED_REFRESH_TOKEN}
```

**Success Response — HTTP 200:**
```json
{
  "access_token": "pina_...",
  "refresh_token": "pinr_...",
  "response_type": "refresh_token",
  "token_type": "bearer",
  "expires_in": 2592000,
  "refresh_token_expires_in": 5184000,
  "refresh_token_expires_at": 1730227664
}
```

> ⚠️ **Critical:** Pinterest issues a **new** refresh token on every refresh call. Always overwrite the stored refresh token with the one returned. The old one is immediately invalidated.

---

### 1.3 Token Behaviour & Expiry Rules

| Token Type | Prefix | Lifetime | Notes |
|---|---|---|---|
| Access Token | `pina_` | 30 days | Short-lived. Used in `Authorization: Bearer` header. |
| Refresh Token | `pinr_` | 60 days from last use | Refreshable indefinitely as long as refreshed within 60 days. |
| Client Credentials Token | `pinc_` | 30 days | Not used in this script. |

**Token invalidation causes (requires full re-auth):**
- User changes their Pinterest password
- Token leaked and detected by GitHub Secret Scanner (auto-revoked by Pinterest)
- Refresh token not used within 60 days

**Script behaviour:**
- On startup, check if `token_expiry` (stored in Google Sheet config tab) is within 24 hours
- If yes, call refresh endpoint, store new `access_token`, `refresh_token`, `token_expiry`
- If refresh fails with 401, halt and alert operator to run the one-time OAuth flow again

---

## 2. User Account

### 2.1 Get User Account

Used at startup to verify the token is valid and confirm which account the script is authenticated as.

```
GET https://api.pinterest.com/v5/user_account
```

**Headers:**
```
Authorization: Bearer {access_token}
```

**Required Scopes:** `user_accounts:read`

**Success Response — HTTP 200:**
```json
{
  "account_type": "BUSINESS",
  "id": "794567890123456789",
  "profile_image": "https://i.pinimg.com/...",
  "website_url": "https://a-won.com",
  "username": "awon_architecture"
}
```

---

## 3. Boards

### 3.1 List Boards

Fetches all boards for the authenticated account. The script uses this to resolve board names (e.g. "Interiors") to their `board_id` at startup. Results are paginated — the script must follow `bookmark` tokens to fetch all boards.

```
GET https://api.pinterest.com/v5/boards
```

**Required Scopes:** `boards:read`

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `page_size` | integer | No | 25 | Results per page. Max: 250. |
| `bookmark` | string | No | — | Pagination cursor from previous response. |
| `privacy` | string | No | — | Filter by `PUBLIC`, `PROTECTED`, `SECRET`. |

**Success Response — HTTP 200:**
```json
{
  "items": [
    {
      "id": "549755885175",
      "name": "Interiors",
      "description": "Interior architecture and design",
      "privacy": "PUBLIC",
      "owner": { "username": "awon_architecture" },
      "media": {
        "pin_thumbnail_urls": [],
        "image_cover_url": null
      }
    }
  ],
  "bookmark": "Y2JiNGU..."
}
```

> **Implementation note:** Call repeatedly with the `bookmark` value until `bookmark` is `null` or absent. Cache the `name → id` mapping in memory for the duration of the run.

---

### 3.2 Get Board

```
GET https://api.pinterest.com/v5/boards/{board_id}
```

**Required Scopes:** `boards:read`

**Success Response — HTTP 200:** Same schema as a single item in the List Boards response.

---

### 3.3 Create Board

```
POST https://api.pinterest.com/v5/boards
```

**Required Scopes:** `boards:write`

**Request Body:**
```json
{
  "name": "Khan Residence",
  "description": "Residential project — Lahore, 2024",
  "privacy": "PUBLIC"
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `name` | string | Yes | Max 180 characters. |
| `description` | string | No | Max 500 characters. |
| `privacy` | string | No | `PUBLIC` (default), `PROTECTED`, `SECRET`. |

**Success Response — HTTP 201:**
```json
{
  "id": "549755885999",
  "name": "Khan Residence",
  "description": "Residential project",
  "privacy": "PUBLIC",
  "owner": { "username": "awon_architecture" }
}
```

---

## 4. Pins — Image

### 4.1 Create Image Pin (via URL)

> ⚠️ Google Drive direct-download links do NOT work reliably as Pinterest image URLs. Use a CDN-style URL if using `image_url`.

```
POST https://api.pinterest.com/v5/pins
```

**Required Scopes:** `pins:write`, `boards:read`

**Request Body:**
```json
{
  "board_id": "549755885175",
  "title": "Khan Residence — Living Room",
  "description": "Warm travertine walls meet minimal steel detailing. Lahore, 2024. #Architecture #Interiors #awon",
  "link": "https://a-won.com/projects/khan-residence",
  "alt_text": "Minimalist living room with travertine walls and steel detailing",
  "media_source": {
    "source_type": "image_url",
    "url": "https://your-cdn.com/images/khan-living-room.jpg",
    "is_standard": true
  }
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `board_id` | string | Yes | Must be a board the authenticated user owns. |
| `title` | string | No | Max 100 characters. |
| `description` | string | No | Max 500 characters. Supports hashtags. |
| `link` | string | No | Destination URL. Must be a valid URL. |
| `alt_text` | string | No | Max 500 characters. |
| `media_source.source_type` | string | Yes | `image_url` for this method. |
| `media_source.url` | string | Yes | Public URL of the image. |
| `media_source.is_standard` | boolean | No | Set `true` for standard image. |

**Success Response — HTTP 201:**
```json
{
  "id": "654321654321654321",
  "title": "Khan Residence — Living Room",
  "board_id": "549755885175",
  "creative_type": "REGULAR",
  "is_owner": true,
  "created_at": "2026-05-29T08:00:00"
}
```

---

### 4.2 Create Image Pin (via Base64)

**This is the primary method for the A.won script** since images originate in Google Drive.

```
POST https://api.pinterest.com/v5/pins
```

**Request Body:**
```json
{
  "board_id": "549755885175",
  "title": "Khan Residence — Living Room",
  "description": "Warm travertine walls. Lahore, 2024. #Architecture #Interiors",
  "link": "https://a-won.com/projects/khan-residence",
  "alt_text": "Minimalist living room with travertine walls",
  "media_source": {
    "source_type": "image_base64",
    "content_type": "image/jpeg",
    "data": "{BASE64_ENCODED_IMAGE_STRING}"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `media_source.source_type` | string | Yes | `image_base64` |
| `media_source.content_type` | string | Yes | `image/jpeg`, `image/png`, `image/webp` |
| `media_source.data` | string | Yes | Raw base64 string (no `data:image/...;base64,` prefix) |

**Image constraints:**
- Supported formats: JPEG, PNG, WEBP, GIF (static)
- Recommended aspect ratio: 2:3 (e.g. 1000×1500px)
- Maximum file size: 32 MB
- Minimum dimensions: 200×300px

---

### 4.3 Get Pin

```
GET https://api.pinterest.com/v5/pins/{pin_id}
```

**Required Scopes:** `pins:read`

**Success Response — HTTP 200:** Same schema as Create Pin response.

---

### 4.4 Delete Pin

```
DELETE https://api.pinterest.com/v5/pins/{pin_id}
```

**Required Scopes:** `pins:write`

**Success Response — HTTP 204 No Content**

---

## 5. Pins — Video (3-Step Flow)

Video Pins require a 3-step upload process. Plan for 10–60 seconds of processing time after creation.

---

### 5.1 Step 1 — Register Video Upload Intent

```
POST https://api.pinterest.com/v5/media
```

**Required Scopes:** `pins:write`

**Request Body:**
```json
{
  "media_type": "video"
}
```

**Success Response — HTTP 201:**
```json
{
  "media_id": "7538982849684754701",
  "media_type": "video",
  "upload_url": "https://pinterest-media-upload.s3-accelerate.amazonaws.com/",
  "upload_parameters": {
    "x-amz-date": "20260529T080000Z",
    "x-amz-signature": "{signature}",
    "x-amz-security-token": "{token}",
    "x-amz-algorithm": "AWS4-HMAC-SHA256",
    "key": "uploads/17/4d/be/2:video:704109860400394553:5258848560742447767",
    "policy": "{base64_policy}",
    "x-amz-credential": "{credential}",
    "Content-Type": "multipart/form-data"
  }
}
```

> **Save** `media_id` and all `upload_parameters` — you need them in Steps 2 and 4.

---

### 5.2 Step 2 — Upload Video to AWS S3

> ⚠️ **No Pinterest `Authorization` header here.** This is a direct AWS request.

```
POST {upload_url from Step 1}
```

**Content-Type:** `multipart/form-data`

**Body:** All key-value pairs from `upload_parameters` + the video file as `file` field.

**Success Response — HTTP 204 No Content**

**Supported video formats:** `.mp4`, `.mov`, `.m4v`

**Video constraints:**
- Maximum file size: 2 GB
- Maximum duration: 15 minutes
- Minimum duration: 4 seconds
- Minimum dimensions: 240p
- Recommended aspect ratio: 9:16 (vertical) or 1:1 (square)

---

### 5.3 Step 3 — Poll Upload Status

```
GET https://api.pinterest.com/v5/media/{media_id}
```

**Required Scopes:** `pins:read`

**Success Response — HTTP 200:**
```json
{
  "media_id": "7538982849684754701",
  "media_type": "video",
  "status": "succeeded"
}
```

**Possible `status` values:**

| Status | Meaning | Script Action |
|---|---|---|
| `registered` | Upload registered but not yet uploaded | Wait, re-poll |
| `processing` | S3 upload received, Pinterest processing | Wait, re-poll |
| `succeeded` | Ready to use in Pin creation | Proceed to Step 4 |
| `failed` | Processing failed | Log error, skip this video |

**Polling strategy:** Poll every 5 seconds, up to 60 attempts (5 minutes timeout).

---

### 5.4 Step 4 — Create Video Pin

```
POST https://api.pinterest.com/v5/pins
```

**Request Body:**
```json
{
  "board_id": "549755885175",
  "title": "Khan Residence — Walkthrough Render",
  "description": "Full walkthrough of the Khan Residence main floor. Lahore, 2024. #Architecture #Render #awon",
  "link": "https://a-won.com/projects/khan-residence",
  "alt_text": "Architectural walkthrough render of Khan Residence main floor",
  "media_source": {
    "source_type": "video_id",
    "media_id": "7538982849684754701",
    "cover_image_url": "https://your-cdn.com/khan-walkthrough-cover.jpg"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `media_source.source_type` | string | Yes | Must be `video_id` |
| `media_source.media_id` | string | Yes | The `media_id` from Step 1 |
| `media_source.cover_image_url` | string | Yes | Public URL of the thumbnail. 400 error if missing. |

**Success Response — HTTP 201:** Same schema as Image Pin creation.

---

## 6. Rate Limits

| Category | Limit |
|---|---|
| Read endpoints (GET) | 1,000 requests / minute |
| Write endpoints (POST, PATCH, DELETE) | 100 requests / minute |

**Headers returned on every response:**

| Header | Description |
|---|---|
| `X-RateLimit-Limit` | Max requests allowed in the window |
| `X-RateLimit-Remaining` | Requests remaining in current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |

**Script implementation:** After each POST, read `X-RateLimit-Remaining`. If `≤ 5`, sleep until `X-RateLimit-Reset`.

---

## 7. Error Codes & Retry Strategy

| Code | Meaning | Script Action |
|---|---|---|
| `201` | Created successfully | Log success, mark Sheet row as `Posted` |
| `400` | Bad request | Log error details, mark as `Failed`, skip — do NOT retry |
| `401` | Unauthorized | Attempt one token refresh, retry. If still 401, halt. |
| `403` | Forbidden | Log and halt — configuration issue |
| `404` | Not found | Log error, mark as `Failed`, skip |
| `429` | Rate limit hit | Sleep until `X-RateLimit-Reset`, retry |
| `500`, `503` | Server error | Retry with exponential backoff |

### Retry Policy (Exponential Backoff)

Apply to: `429`, `500`, `503` only.

```
Attempt 1: immediate
Attempt 2: wait 2 seconds
Attempt 3: wait 4 seconds
Attempt 4: wait 8 seconds
Attempt 5: wait 16 seconds
After 5 attempts: mark row as Failed, log last error, move to next row
```

### Error Response Schema

```json
{
  "code": 4,
  "message": "The board ID provided does not exist.",
  "status": "failure",
  "endpoint_name": "pins/create",
  "message_detail": {}
}
```

---

## 8. Required OAuth Scopes Summary

| Scope | Why it's needed |
|---|---|
| `boards:read` | List boards to resolve board names to IDs |
| `boards:write` | Create new boards (future feature) |
| `pins:read` | Verify a Pin after creation; poll media status |
| `pins:write` | Create image and video Pins |

---

## 9. Field Constraints

| Field | Max Length | Notes |
|---|---|---|
| Pin `title` | 100 characters | Shown below Pin preview in search and feeds |
| Pin `description` | 500 characters | Supports hashtags (`#Architecture`) |
| Pin `alt_text` | 500 characters | Accessibility text, not publicly visible |
| Pin `link` | No stated limit | Must be a valid URL with `https://` |
| Board `name` | 180 characters | |
| Board `description` | 500 characters | |
| Image file size | 32 MB | For base64 upload |
| Video file size | 2 GB | |
| Video duration | 4 sec – 15 min | |

**Recommended image specs:**
- Aspect ratio: **2:3** (e.g. 1000×1500px or 1080×1620px)
- Format: JPEG or PNG
- Resolution: minimum 600px wide

**Recommended video specs:**
- Aspect ratio: **9:16** (vertical) or **1:1** (square)
- Format: MP4 (H.264)
- Resolution: 1080×1920 (vertical) recommended

---

*End of Pinterest API v5 Reference — A.won Automation Script*  
*For the full OpenAPI spec: https://github.com/pinterest/api-description/blob/main/v5/openapi.yaml*
