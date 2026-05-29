"""
Exponential backoff retry decorator for Pinterest API calls.

Wraps a function and automatically retries it when the called code raises
an exception carrying an HTTP status code of 429, 500, or 503.

Backoff sequence (5 attempts total):
  Attempt 1: immediate
  Attempt 2: wait 2 seconds
  Attempt 3: wait 4 seconds
  Attempt 4: wait 8 seconds
  Attempt 5: wait 16 seconds

After 5 exhausted attempts the original exception is re-raised so the
caller can mark the sheet row as Failed and continue to the next row.

HTTP 400, 403, and 404 are NOT retried — they indicate configuration
problems where retrying will not help.
"""
