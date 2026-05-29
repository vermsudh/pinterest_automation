"""
Manages Pinterest OAuth 2.0 token lifecycle.

On every run, reads access_token, refresh_token, and token_expiry from
cells B1–B3 of the _config tab in the Google Sheet. If the access token
is within TOKEN_REFRESH_THRESHOLD_HOURS (24 hours) of expiry, calls the
Pinterest refresh endpoint, writes the new token triple back to the Sheet,
and returns the fresh access token.

Critical invariant from the Pinterest API spec (Section 1.2):
Every successful refresh call returns a NEW refresh_token that immediately
invalidates the previous one. Both access_token and refresh_token must be
written back to the Sheet after every refresh — not just the access_token.

Token values are never logged in full. Only the first 12 characters
followed by '...' appear in any log output.
"""

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config.settings import (
    PINTEREST_TOKEN_URL,
    SHEET_CONFIG_TAB,
    TOKEN_REFRESH_THRESHOLD_HOURS,
)

logger = logging.getLogger(__name__)

_TOKEN_MASK_LENGTH: int = 12


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TokenData:
    """Pinterest OAuth token triple stored in the _config Sheet tab.

    All three fields are kept in sync on every refresh. Writing only the
    access_token back would leave the old (now-invalidated) refresh_token
    in the Sheet, causing a 401 failure on the next run.

    Attributes:
        access_token: Bearer token used in Authorization headers for all
            Pinterest API requests. Prefix: 'pina_'. Lifetime: 30 days.
        refresh_token: Used to obtain a new access_token without user
            interaction. Prefix: 'pinr_'. Expires if unused for 60 days.
            Immediately invalidated on each refresh — always save the new one.
        token_expiry: UTC-aware datetime at which the access_token expires.
            Stored in the Sheet as an ISO 8601 UTC string
            (e.g. '2026-06-28T08:00:00Z').
    """

    access_token: str
    refresh_token: str
    token_expiry: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _mask_token(token: str) -> str:
    """Return a safe-to-log representation of a token string.

    Shows only the first 12 characters followed by '...' so the token
    can be identified in logs without being exposed.

    Args:
        token: The full token string (access_token or refresh_token).

    Returns:
        A masked string such as 'pina_Ab12Cd...' or '***' if the token
        is too short to safely show any prefix.
    """
    if len(token) <= _TOKEN_MASK_LENGTH:
        return "***"
    return token[:_TOKEN_MASK_LENGTH] + "..."


def _parse_token_expiry(raw: str) -> datetime:
    """Parse an ISO 8601 UTC string into a timezone-aware UTC datetime.

    Accepts both the 'Z' suffix (e.g. '2026-06-28T08:00:00Z') and the
    '+00:00' offset form. Python 3.11's datetime.fromisoformat() handles
    both. If the parsed datetime carries no timezone info, UTC is assumed
    (defensive guard for values written by an older version of this script).

    Args:
        raw: ISO 8601 datetime string as read from _config!B3.

    Returns:
        A timezone-aware datetime in UTC.

    Raises:
        ValueError: If the string cannot be parsed as an ISO 8601 datetime,
            with a message that names the exact cell so the operator knows
            where to look.
    """
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"token_expiry value '{raw}' in {SHEET_CONFIG_TAB}!B3 is not a valid "
            "ISO 8601 datetime. Expected format: '2026-06-28T08:00:00Z'."
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_cell(rows: list[list[str]], row_index: int, cell_name: str) -> str:
    """Extract and validate a single cell value from a Sheets API response.

    The Sheets API omits trailing empty rows from its response, so a
    missing row and an empty cell must both be treated as absent.

    Args:
        rows: The 'values' list returned by the Sheets API get() call.
        row_index: Zero-based row index within the returned values.
        cell_name: Human-readable field name used in the error message.

    Returns:
        The non-empty, stripped cell value string.

    Raises:
        RuntimeError: If the cell is absent or empty, with an operator-
            actionable message naming the exact Sheet cell.
    """
    cell_ref = f"{SHEET_CONFIG_TAB}!B{row_index + 1}"
    if row_index >= len(rows) or not rows[row_index]:
        raise RuntimeError(
            f"Cell {cell_ref} ({cell_name}) is empty. "
            "The one-time Pinterest OAuth setup has not been completed. "
            "Run the setup script to generate an initial access_token and "
            f"refresh_token, then store them in the {SHEET_CONFIG_TAB} tab."
        )
    value = rows[row_index][0].strip()
    if not value:
        raise RuntimeError(
            f"Cell {cell_ref} ({cell_name}) is empty. "
            "The one-time Pinterest OAuth setup has not been completed. "
            "Run the setup script to generate an initial access_token and "
            f"refresh_token, then store them in the {SHEET_CONFIG_TAB} tab."
        )
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_tokens_from_sheet(sheets_client: Any, sheet_id: str) -> TokenData:
    """Read the Pinterest token triple from the _config tab of the Google Sheet.

    Reads cells B1 (access_token), B2 (refresh_token), and B3 (token_expiry)
    as a single range request. Raises a descriptive error if any cell is
    missing or empty, which indicates the one-time OAuth setup has not been
    run for this account.

    Args:
        sheets_client: Authenticated Google Sheets API client built by
            auth.google_auth.build_sheets_client().
        sheet_id: Google Sheets document ID (the GOOGLE_SHEET_ID env var).

    Returns:
        A TokenData instance populated with the three stored token values.

    Raises:
        RuntimeError: If any of the three token cells are empty, with a
            message that names the specific cell and instructs the operator
            to complete the one-time OAuth setup.
        ValueError: If the token_expiry string in B3 cannot be parsed as
            an ISO 8601 datetime.
    """
    result = (
        sheets_client.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{SHEET_CONFIG_TAB}!B1:B3")
        .execute()
    )
    rows: list[list[str]] = result.get("values", [])

    access_token = _read_cell(rows, 0, "access_token")
    refresh_token = _read_cell(rows, 1, "refresh_token")
    expiry_raw = _read_cell(rows, 2, "token_expiry")
    token_expiry = _parse_token_expiry(expiry_raw)

    return TokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
    )


def save_tokens_to_sheet(
    sheets_client: Any,
    sheet_id: str,
    token_data: TokenData,
) -> None:
    """Write the Pinterest token triple back to the _config tab.

    Overwrites B1 (access_token), B2 (refresh_token), and B3 (token_expiry)
    in a single batch update. Called after every successful token refresh to
    ensure the Sheet always holds a valid, non-invalidated token set.

    token_expiry is serialised as ISO 8601 UTC with a 'Z' suffix
    (e.g. '2026-06-28T08:00:00Z') so it round-trips cleanly through
    load_tokens_from_sheet().

    Args:
        sheets_client: Authenticated Google Sheets API client built by
            auth.google_auth.build_sheets_client().
        sheet_id: Google Sheets document ID (the GOOGLE_SHEET_ID env var).
        token_data: The refreshed token values to persist. All three fields
            must be populated with the values returned by refresh_access_token().
    """
    expiry_str = token_data.token_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "values": [
            [token_data.access_token],
            [token_data.refresh_token],
            [expiry_str],
        ]
    }
    (
        sheets_client.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_CONFIG_TAB}!B1:B3",
            valueInputOption="RAW",
            body=body,
        )
        .execute()
    )
    logger.info("Token data written back to %s!B1:B3.", SHEET_CONFIG_TAB)


def is_token_expiring_soon(token_data: TokenData) -> bool:
    """Return True if the access token expires within the refresh threshold.

    Compares token_expiry against the current UTC time. Returns True when
    the remaining lifetime is less than or equal to TOKEN_REFRESH_THRESHOLD_HOURS
    (24 hours), triggering a proactive refresh before the token can expire
    mid-run.

    Args:
        token_data: Token triple loaded from the _config Sheet tab.

    Returns:
        True if the access token expires within 24 hours of now (UTC).
        False if the token has more than 24 hours of remaining lifetime.
    """
    now = datetime.now(timezone.utc)
    return (token_data.token_expiry - now) <= timedelta(hours=TOKEN_REFRESH_THRESHOLD_HOURS)


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> TokenData:
    """Call the Pinterest token refresh endpoint and return new token data.

    Sends a POST to PINTEREST_TOKEN_URL with HTTP Basic Auth credentials
    (base64-encoded 'client_id:client_secret') and a form-encoded body
    containing grant_type=refresh_token.

    Per the API spec (Section 1.2), Pinterest returns a brand-new
    refresh_token on every successful call. The caller must pass this new
    refresh_token to save_tokens_to_sheet() — the old one is immediately
    invalidated regardless of whether it gets saved.

    token_expiry is calculated as:
        datetime.now(UTC) + timedelta(seconds=response['expires_in'])

    Args:
        client_id: Pinterest OAuth app Client ID (PINTEREST_CLIENT_ID).
        client_secret: Pinterest OAuth app Client Secret (PINTEREST_CLIENT_SECRET).
        refresh_token: The current refresh_token stored in the Sheet (B2).

    Returns:
        A new TokenData with updated access_token, refresh_token, and
        token_expiry. All three values must be written back to the Sheet
        via save_tokens_to_sheet().

    Raises:
        RuntimeError: If the response is HTTP 401 — the refresh token has
            expired and the operator must re-run the one-time OAuth setup.
        RuntimeError: If the response is any other non-200 status — includes
            the HTTP status code and full response body for diagnosis.
    """
    raw_credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(raw_credentials.encode()).decode()

    response = requests.post(
        PINTEREST_TOKEN_URL,
        headers={
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Pinterest refresh token has expired. Re-run the one-time OAuth "
            "setup script to generate a new refresh token and store it in "
            "the _config sheet tab."
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Pinterest token refresh failed with HTTP {response.status_code}. "
            f"Response body: {response.text}"
        )

    payload: dict[str, Any] = response.json()
    expires_in: int = payload["expires_in"]
    token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    return TokenData(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        token_expiry=token_expiry,
    )


def ensure_valid_token(
    sheets_client: Any,
    sheet_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Guarantee a valid Pinterest access token before the upload run begins.

    Orchestrates the full token lifecycle check:
      1. Loads the stored token triple from the _config Sheet tab.
      2. Checks whether the access token expires within 24 hours.
      3. If expiring: refreshes via the Pinterest API, then writes the
         complete new token triple (including the new refresh_token) back
         to the Sheet before proceeding.
      4. Logs which path was taken and the masked token value either way.

    This function is the sole entry point for token management in main.py.
    No other module touches Pinterest tokens.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID (the GOOGLE_SHEET_ID env var).
        client_id: Pinterest OAuth app Client ID (PINTEREST_CLIENT_ID).
        client_secret: Pinterest OAuth app Client Secret (PINTEREST_CLIENT_SECRET).

    Returns:
        A valid access_token string ready to use as 'Authorization: Bearer {token}'
        for all Pinterest API calls during this run.

    Raises:
        RuntimeError: If any token cell in the Sheet is empty (setup not done),
            if the refresh token has expired (401 from Pinterest), or if the
            Pinterest token endpoint returns an unexpected error.
        ValueError: If the stored token_expiry string cannot be parsed.
    """
    token_data = load_tokens_from_sheet(sheets_client, sheet_id)

    if is_token_expiring_soon(token_data):
        expiry_str = token_data.token_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "Access token expires at %s (within %d-hour threshold). Refreshing now.",
            expiry_str,
            TOKEN_REFRESH_THRESHOLD_HOURS,
        )
        token_data = refresh_access_token(
            client_id, client_secret, token_data.refresh_token
        )
        save_tokens_to_sheet(sheets_client, sheet_id, token_data)
        new_expiry_str = token_data.token_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "Token refreshed. New access token: %s | New expiry: %s",
            _mask_token(token_data.access_token),
            new_expiry_str,
        )
    else:
        expiry_str = token_data.token_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "Access token valid until %s. Using stored token: %s",
            expiry_str,
            _mask_token(token_data.access_token),
        )

    return token_data.access_token
