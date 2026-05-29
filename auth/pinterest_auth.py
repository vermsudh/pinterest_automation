"""
Manages Pinterest OAuth 2.0 token lifecycle.

On startup, reads access_token, refresh_token, and token_expiry from
cells B1–B3 of the _config tab in the Google Sheet. If the access token
is within 24 hours of expiry, calls the Pinterest refresh endpoint
(POST /v5/oauth/token), writes the new access_token, refresh_token, and
token_expiry back to the Sheet, and returns a valid Bearer token.

If the refresh call returns HTTP 401, halts immediately and prints a
clear message instructing the operator to re-run the one-time OAuth setup.

Token strings are masked in all log output (first 8 characters only).
"""
