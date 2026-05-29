"""
Handles Google Service Account authentication.

Parses the service account JSON from config.settings and produces scoped
credentials plus API client objects for Google Sheets and Google Drive.

All three public functions are called once in main.py. The resulting clients
are passed as arguments into the service modules — no service module builds
its own client. This keeps authentication in one place and makes the
dependency chain explicit.
"""

import json
from typing import Any

from google.oauth2 import service_account
from googleapiclient import discovery

from config.settings import GOOGLE_SERVICE_ACCOUNT_JSON

# OAuth 2.0 scopes required by this script.
# Spreadsheets scope covers reading the queue and writing status back.
# Drive scope covers downloading media files and moving them between folders.
_GOOGLE_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def build_google_credentials() -> service_account.Credentials:
    """Parse the service account JSON and return scoped Google credentials.

    Reads the raw JSON string from config.settings.GOOGLE_SERVICE_ACCOUNT_JSON
    (which was validated at import time by settings.py). Parses it into a dict,
    then constructs a google.oauth2.service_account.Credentials instance scoped
    for both the Sheets API and the Drive API so a single Credentials object
    can be reused for both clients.

    Returns:
        A service_account.Credentials instance authorised for Google Sheets
        and Google Drive.

    Raises:
        ValueError: If GOOGLE_SERVICE_ACCOUNT_JSON contains text that is not
            valid JSON. The message tells the operator what the correct format
            is so they know how to fix it.
        google.auth.exceptions.MalformedError: If the JSON is valid but is
            missing required service account fields (e.g. 'client_email',
            'private_key'). Raised by the Google Auth library directly.
    """
    try:
        service_account_info: dict[str, Any] = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON could not be parsed as JSON.\n"
            "The secret must contain the raw contents of the service account key file\n"
            "(.json file downloaded from Google Cloud Console → IAM → Service Accounts).\n"
            f"JSON error: {exc}"
        ) from exc

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=_GOOGLE_SCOPES,
    )


def build_sheets_client(credentials: service_account.Credentials) -> Any:
    """Build and return an authenticated Google Sheets API v4 client.

    The returned object is the entry point for all Sheets calls made by
    services/sheets_service.py. It is constructed once in main.py and
    passed down — never instantiated inside a service module.

    Args:
        credentials: Scoped service account credentials returned by
            build_google_credentials().

    Returns:
        A googleapiclient.discovery.Resource configured for the Sheets v4 API.
        Typed as Any because the google-api-python-client library generates
        resource classes dynamically and does not expose a concrete public type.
    """
    return discovery.build("sheets", "v4", credentials=credentials)


def build_drive_client(credentials: service_account.Credentials) -> Any:
    """Build and return an authenticated Google Drive API v3 client.

    The returned object is the entry point for all Drive calls made by
    services/drive_service.py. It is constructed once in main.py and
    passed down — never instantiated inside a service module.

    Args:
        credentials: Scoped service account credentials returned by
            build_google_credentials().

    Returns:
        A googleapiclient.discovery.Resource configured for the Drive v3 API.
        Typed as Any because the google-api-python-client library generates
        resource classes dynamically and does not expose a concrete public type.
    """
    return discovery.build("drive", "v3", credentials=credentials)
