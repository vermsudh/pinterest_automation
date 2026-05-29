"""
All Pinterest API v5 HTTP calls for the A.won automation script.

Covers:
  - GET  /v5/user_account          — startup health check
  - GET  /v5/boards (paginated)    — build board name→ID map
  - POST /v5/pins                  — create image Pin (image_base64)
  - POST /v5/media                 — register video upload intent (Step 1)
  - POST {s3_upload_url}           — upload video to AWS S3 (Step 2, no Pinterest auth)
  - GET  /v5/media/{media_id}      — poll video processing status (Step 3)
  - POST /v5/pins                  — create video Pin (video_id) (Step 4)

After every write request, reads X-RateLimit-Remaining from response headers.
If the value is <= 5, sleeps until the Unix timestamp in X-RateLimit-Reset.

All endpoint schemas come from docs/pinterest_api_reference.md.
"""
