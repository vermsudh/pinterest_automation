"""
Loads all environment variables and defines project-wide constants.

All string literals that appear more than once — API base URLs, sheet tab
names, Drive folder names, retry counts, poll intervals, rate-limit thresholds
— are defined here as module-level constants. No other module should contain
hardcoded strings; import from this module instead.

For local development, values are read from a .env file via python-dotenv.
In GitHub Actions, the same variables are injected as process environment
variables — load_dotenv() is a silent no-op in that case, so both paths
work without any conditional logic.

The PinterestAccount dataclass groups all account-specific config so that
a second brand account can be added in a future version by creating a new
loader function and passing a different PinterestAccount to main() — no
changes required in any other module (see Section 11 of SYSTEM_INSTRUCTION.md).
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Loads .env into os.environ for local development.
# No-op when variables are already present (e.g. GitHub Actions).
load_dotenv()

# ---------------------------------------------------------------------------
# Gemini API (caption generation)
# ---------------------------------------------------------------------------

# Optional — if absent, caption generation is skipped and the run continues.
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()

# Available Gemini models for caption generation.
GEMINI_MODELS: dict[str, str] = {
    "flash-lite": "gemini-2.5-flash-lite",
    "flash": "gemini-2.5-flash",
    "flash-3": "gemini-3-flash-preview",
    "flash-lite-3": "gemini-3.1-flash-lite",
    "pro": "gemini-2.5-pro",
}

# Default model — most cost-efficient for caption generation
GEMINI_MODEL: str = GEMINI_MODELS["flash-lite"]

# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Return the value of a required environment variable.

    Args:
        name: The exact environment variable name to look up.

    Returns:
        The non-empty, stripped string value of the variable.

    Raises:
        EnvironmentError: If the variable is absent or set to an empty/
            whitespace-only string. The error message names the missing
            variable explicitly so the operator knows exactly what to fix.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is missing or empty.\n"
            f"  → For local development: add it to your .env file.\n"
            f"  → For GitHub Actions: add it to the repository Secrets."
        )
    return value


# ---------------------------------------------------------------------------
# Pinterest API endpoints
# ---------------------------------------------------------------------------

PINTEREST_API_BASE_URL: str = "https://api.pinterest.com/v5"
PINTEREST_TOKEN_URL: str = "https://api.pinterest.com/v5/oauth/token"

# ---------------------------------------------------------------------------
# Google Sheets structure
# ---------------------------------------------------------------------------

SHEET_QUEUE_TAB: str = "Queue"
SHEET_CONFIG_TAB: str = "_config"

# ---------------------------------------------------------------------------
# Google Drive folder names
# (sub-folders that must exist inside the root GOOGLE_DRIVE_FOLDER_ID folder)
# ---------------------------------------------------------------------------

DRIVE_READY_FOLDER_NAME: str = "Ready"
DRIVE_POSTED_FOLDER_NAME: str = "Posted"
DRIVE_FAILED_FOLDER_NAME: str = "Failed"

# ---------------------------------------------------------------------------
# Operational tuning constants
# ---------------------------------------------------------------------------

# Hours before access token expiry at which the script proactively refreshes.
TOKEN_REFRESH_THRESHOLD_HOURS: int = 24

# Seconds between each poll of GET /v5/media/{media_id} during video processing.
VIDEO_POLL_INTERVAL_SECONDS: int = 5

# Maximum number of poll attempts before declaring a video upload timed out.
# 60 attempts × 5 seconds = 5-minute timeout.
VIDEO_POLL_MAX_ATTEMPTS: int = 60

# If X-RateLimit-Remaining falls to this value or below after a write request,
# the script sleeps until X-RateLimit-Reset before sending the next request.
RATE_LIMIT_BUFFER: int = 5

# Total number of attempts (including the first) before a row is marked Failed.
# Backoff sequence: immediate, 2s, 4s, 8s, 16s.
MAX_RETRY_ATTEMPTS: int = 5

# ---------------------------------------------------------------------------
# Google credentials
#
# Read at import time so any misconfiguration is caught immediately on
# startup — before any Google API client is constructed or any Sheet row
# is read. The raw JSON string is passed to google_auth.py for parsing.
# ---------------------------------------------------------------------------

GOOGLE_SERVICE_ACCOUNT_JSON: str = _require_env("GOOGLE_SERVICE_ACCOUNT_JSON")


# ---------------------------------------------------------------------------
# Gemini API
#
# GEMINI_API_KEY is intentionally optional — caption generation is skipped
# gracefully when the key is absent rather than aborting the run. Using
# os.environ.get() (not _require_env) achieves this without raising on import.
# ---------------------------------------------------------------------------

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Account dataclass
# ---------------------------------------------------------------------------

@dataclass
class PinterestAccount:
    """All account-specific config for one Pinterest / Google Sheet pair.

    Keeping these fields together means adding a second brand account requires
    only a new load_<brand>_account() function that reads a different set of
    environment variables — nothing else changes.

    Attributes:
        pinterest_client_id: OAuth Client ID from the Pinterest developer app.
        pinterest_client_secret: OAuth Client Secret from the Pinterest
            developer app.
        google_sheet_id: The ID portion of the Google Sheets URL
            (the long alphanumeric string between /d/ and /edit).
        google_drive_folder_id: Drive folder ID of the root Pinterest Queue
            folder that contains the Ready/, Posted/, and Failed/ sub-folders.
        account_name: Short lowercase identifier used in log lines and
            error messages (e.g. "awon"). Not used for any API call.
    """

    pinterest_client_id: str
    pinterest_client_secret: str
    google_sheet_id: str
    google_drive_folder_id: str
    account_name: str


# ---------------------------------------------------------------------------
# Account loader
# ---------------------------------------------------------------------------

def load_awon_account() -> PinterestAccount:
    """Build and return the PinterestAccount for the A.won brand.

    Reads PINTEREST_CLIENT_ID, PINTEREST_CLIENT_SECRET, GOOGLE_SHEET_ID,
    and GOOGLE_DRIVE_FOLDER_ID from the environment. Raises immediately if
    any variable is absent so the operator sees a clear error before the
    script attempts any network call.

    Returns:
        A fully-populated PinterestAccount for the A.won brand.

    Raises:
        EnvironmentError: If any required environment variable is absent or
            empty. The error message includes the exact variable name.
    """
    return PinterestAccount(
        pinterest_client_id=_require_env("PINTEREST_CLIENT_ID"),
        pinterest_client_secret=_require_env("PINTEREST_CLIENT_SECRET"),
        google_sheet_id=_require_env("GOOGLE_SHEET_ID"),
        google_drive_folder_id=_require_env("GOOGLE_DRIVE_FOLDER_ID"),
        account_name="awon",
    )
