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

import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config.settings import PINTEREST_API_BASE_URL, RATE_LIMIT_BUFFER

_log = logging.getLogger(__name__)


class RetryableError(Exception):
    """Raised when the Pinterest API returns HTTP 429, 500, or 503.

    The retry decorator in ``utils/retry.py`` catches this exception class
    to apply exponential backoff before re-attempting the failed request.
    Raising this (rather than a generic exception) keeps retry logic out of
    this module while still giving the caller clear signal about recoverability.
    """


class PinterestClient:
    """Thin HTTP client for the Pinterest API v5.

    Encapsulates all network calls to Pinterest so the rest of the codebase
    never constructs raw requests directly. Contains no business logic —
    no retry loops, no Sheet writes, no Drive operations. Every method either
    returns a parsed result or raises an exception; the caller decides what
    to do next.

    Attributes:
        _session: A ``requests.Session`` with the ``Authorization`` and
            ``Content-Type`` headers pre-set so they are sent on every call
            without repetition.
    """

    def __init__(self, access_token: str) -> None:
        """Initialise the client with a valid Pinterest access token.

        Args:
            access_token: A Pinterest Bearer token (prefix ``pina_``).
                Set once on the session so every subsequent request includes it.
        """
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_rate_limit(self, response: requests.Response) -> None:
        """Sleep until the rate-limit window resets if the budget is nearly gone.

        Reads ``X-RateLimit-Remaining`` from *response*. If the value is at or
        below ``RATE_LIMIT_BUFFER`` (5), calculates the sleep duration from
        ``X-RateLimit-Reset`` (a Unix timestamp) and blocks until that moment.
        Called after every POST request per the API reference (Section 6).

        Args:
            response: The completed ``requests.Response`` whose headers are
                inspected. Does nothing if the rate-limit headers are absent.
        """
        remaining_str = response.headers.get("X-RateLimit-Remaining")
        if remaining_str is None:
            return
        try:
            remaining = int(remaining_str)
        except ValueError:
            return

        if remaining > RATE_LIMIT_BUFFER:
            return

        reset_str = response.headers.get("X-RateLimit-Reset", "")
        try:
            reset_ts = int(reset_str)
        except ValueError:
            return

        reset_time = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        _log.warning("Rate limit nearly exhausted. Sleeping until %s.", reset_time)
        sleep_seconds = max(0, reset_ts - int(time.time()))
        time.sleep(sleep_seconds)

    def _handle_response(self, response: requests.Response) -> dict[str, Any]:
        """Parse a Pinterest API response or raise an appropriate exception.

        Maps HTTP status codes to either a parsed JSON dict or a raised
        exception so every public method has a single, consistent path for
        error handling (Section 7 of the API reference).

        - 2xx  → returns the parsed JSON body (empty dict for 204)
        - 400  → ``RuntimeError`` — bad request, do not retry
        - 401  → ``RuntimeError`` — instructs operator to re-run OAuth
        - 403  → ``RuntimeError`` — permissions/config issue, do not retry
        - 404  → ``RuntimeError`` — resource not found, do not retry
        - 429/500/503 → ``RetryableError`` — caller's retry decorator handles these
        - other → ``RuntimeError`` with the raw status and body

        Args:
            response: The completed ``requests.Response`` to evaluate.

        Returns:
            Parsed JSON body as a ``dict``, or an empty ``dict`` for 204 responses.

        Raises:
            RuntimeError: For non-retryable HTTP errors (400, 401, 403, 404)
                and unexpected status codes.
            RetryableError: For transient HTTP errors (429, 500, 503) that
                the retry decorator should handle.
        """
        status = response.status_code

        if 200 <= status < 300:
            if status == 204 or not response.content:
                return {}
            return response.json()

        if status == 400:
            raise RuntimeError(
                f"HTTP 400 Bad Request from Pinterest API — do not retry. "
                f"Check field values and constraints. "
                f"Response body: {response.text}"
            )

        if status == 401:
            raise RuntimeError(
                "HTTP 401 Unauthorized from Pinterest API. "
                "The access token is invalid or has been revoked. "
                "Re-run the one-time OAuth setup to obtain a fresh access token "
                "and refresh token, then store them in the _config sheet tab."
            )

        if status == 403:
            raise RuntimeError(
                f"HTTP 403 Forbidden from Pinterest API — do not retry. "
                f"Verify the app's OAuth scopes and board ownership. "
                f"Response body: {response.text}"
            )

        if status == 404:
            raise RuntimeError(
                f"HTTP 404 Not Found from Pinterest API — do not retry. "
                f"The requested resource does not exist. "
                f"Response body: {response.text}"
            )

        if status in (429, 500, 503):
            raise RetryableError(
                f"HTTP {status} from Pinterest API — eligible for retry. "
                f"Response body: {response.text}"
            )

        raise RuntimeError(
            f"Unexpected HTTP {status} from Pinterest API. "
            f"Response body: {response.text}"
        )

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_user_account(self) -> dict[str, Any]:
        """Fetch the authenticated user's account details.

        Used at startup to verify the access token is valid and to confirm
        which Pinterest account the script is running as (Section 2.1 of
        the API reference).

        Returns:
            The full JSON response body as a ``dict``, containing fields such
            as ``username``, ``id``, ``account_type``, and ``website_url``.

        Raises:
            RuntimeError: For any non-2xx response (see ``_handle_response``).
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        response = self._session.get(f"{PINTEREST_API_BASE_URL}/user_account")
        return self._handle_response(response)

    def get_boards(self) -> dict[str, str]:
        """Fetch all boards for the authenticated account and return a name→ID map.

        Paginates through the ``/v5/boards`` endpoint using the ``bookmark``
        cursor until the response contains no further pages (Section 3.1 of
        the API reference). Results are cached by the caller for the duration
        of the run.

        Returns:
            A ``dict`` mapping each board's display name to its Pinterest ID,
            e.g. ``{"Interiors": "549755885175", "Facades": "123456789"}``.
            Returns an empty dict if the account has no boards.

        Raises:
            RuntimeError: For any non-2xx response (see ``_handle_response``).
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        boards: dict[str, str] = {}
        params: dict[str, Any] = {"page_size": 250}

        while True:
            response = self._session.get(
                f"{PINTEREST_API_BASE_URL}/boards",
                params=params,
            )
            data = self._handle_response(response)

            for board in data.get("items", []):
                boards[board["name"]] = board["id"]

            bookmark: str | None = data.get("bookmark")
            if not bookmark:
                break
            params["bookmark"] = bookmark

        _log.info("Loaded %d board(s) from the Pinterest account.", len(boards))
        return boards
    
    def create_board(self,name: str,privacy: str = "PUBLIC",) -> dict[str, Any]:
        """Create a new Pinterest board on the authenticated account.

        Calls POST /v5/boards to create a board with the given name and
        privacy setting. Requires boards:write scope.

        Args:
            name: The board name. Must be unique within the account.
            privacy: Either "PUBLIC" or "SECRET". Defaults to "PUBLIC".

        Returns:
            A dict containing the new board's "id" and "name" fields.

        Raises:
            RuntimeError: For any non-retryable HTTP error.
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        payload: dict[str, Any] = {
            "name": name,
            "privacy": privacy,
        }
        response = self._session.post(
            f"{PINTEREST_API_BASE_URL}/boards",
            json=payload,
        )
        data = self._handle_response(response)
        self._check_rate_limit(response)
        _log.info("Created board '%s' with ID %s.", name, data.get("id"))
        return data

    def create_image_pin(
        self,
        board_id: str,
        title: str,
        description: str,
        link: str,
        alt_text: str,
        image_data: bytes,
        content_type: str,
    ) -> str:
        """Create an image Pin using the base64 upload method.

        Encodes *image_data* as a raw base64 string (no ``data:image/...;base64,``
        prefix) and posts it to ``/v5/pins`` with ``source_type: "image_base64"``.
        This is the primary image Pin creation method for the A.won script
        since images originate in Google Drive (Section 4.2 of the API reference).

        Args:
            board_id: The Pinterest board ID to post this Pin to.
            title: Pin title. Max 100 characters (validated upstream).
            description: Pin description. Max 500 characters. Supports hashtags.
            link: Destination URL shown when a user clicks the Pin.
            alt_text: Accessibility description. Max 500 characters.
            image_data: Raw image file bytes downloaded from Google Drive.
            content_type: MIME type of the image, e.g. ``"image/jpeg"``,
                ``"image/png"``, or ``"image/webp"``.

        Returns:
            The Pinterest ``pin_id`` string of the newly created Pin.

        Raises:
            RuntimeError: For any non-retryable HTTP error.
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        encoded_data = base64.b64encode(image_data).decode("utf-8")
        payload: dict[str, Any] = {
            "board_id": board_id,
            "title": title,
            "description": description,
            "link": link,
            "alt_text": alt_text,
            "media_source": {
                "source_type": "image_base64",
                "content_type": content_type,
                "data": encoded_data,
            },
        }
        response = self._session.post(
            f"{PINTEREST_API_BASE_URL}/pins",
            json=payload,
        )
        data = self._handle_response(response)
        self._check_rate_limit(response)
        return data["id"]

    def register_video_upload(self) -> tuple[str, str, dict[str, str]]:
        """Register a video upload intent with Pinterest (Step 1 of 4).

        Calls ``POST /v5/media`` to obtain the AWS S3 upload URL and all
        pre-signed parameters needed for Step 2 (Section 5.1 of the API
        reference). The returned ``media_id`` must also be passed to
        ``create_video_pin`` in Step 4.

        Returns:
            A three-tuple of:
            - ``media_id`` (str): Opaque ID that links the S3 upload to the
              eventual Pin.
            - ``upload_url`` (str): The AWS S3 endpoint to POST the video to.
            - ``upload_parameters`` (dict[str, str]): Pre-signed fields that
              must be included as multipart form fields in the S3 POST request.

        Raises:
            RuntimeError: For any non-retryable HTTP error.
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        response = self._session.post(
            f"{PINTEREST_API_BASE_URL}/media",
            json={"media_type": "video"},
        )
        data = self._handle_response(response)
        self._check_rate_limit(response)
        return data["media_id"], data["upload_url"], data["upload_parameters"]

    def upload_video_to_s3(
        self,
        upload_url: str,
        upload_parameters: dict[str, str],
        video_data: bytes,
    ) -> None:
        """Upload raw video bytes directly to the AWS S3 pre-signed URL (Step 2 of 4).

        Sends a ``multipart/form-data`` POST to *upload_url* containing all
        fields from *upload_parameters* followed by the video bytes as the
        ``file`` field. Uses a separate ``requests.Session`` with **no**
        ``Authorization`` header — S3 uses the pre-signed policy for auth and
        will reject any Pinterest token (Section 5.2 of the API reference).

        Args:
            upload_url: The ``upload_url`` string from ``register_video_upload``.
            upload_parameters: The ``upload_parameters`` dict from
                ``register_video_upload``. All entries are sent as multipart
                form fields before the file field, as required by S3 policy.
            video_data: Raw video file bytes. Max 2 GB per the API reference.

        Raises:
            RuntimeError: If the S3 response is anything other than HTTP 204.
        """
        s3_session = requests.Session()
        response = s3_session.post(
            upload_url,
            data=upload_parameters,
            files={"file": video_data},
        )
        if response.status_code != 204:
            raise RuntimeError(
                f"AWS S3 video upload failed — expected HTTP 204, "
                f"got HTTP {response.status_code}. "
                f"Response body: {response.text}"
            )

    def poll_video_status(self, media_id: str) -> bool:
        """Check the processing status of an uploaded video (Step 3 of 4).

        Calls ``GET /v5/media/{media_id}`` and interprets the ``status`` field
        in the response (Section 5.3 of the API reference). Designed to be
        called repeatedly by the video uploader with a sleep between attempts.

        Args:
            media_id: The ``media_id`` returned by ``register_video_upload``.

        Returns:
            ``True`` if Pinterest has finished processing the video
            (``status == "succeeded"``).
            ``False`` if processing is still underway
            (``status == "registered"`` or ``"processing"``).

        Raises:
            RuntimeError: If ``status == "failed"`` — processing cannot be
                recovered; the caller should mark the row as Failed.
            RuntimeError: If *status* is an unrecognised value.
            RetryableError: For HTTP 429, 500, or 503 responses from the poll
                endpoint itself.
        """
        response = self._session.get(
            f"{PINTEREST_API_BASE_URL}/media/{media_id}"
        )
        data = self._handle_response(response)
        status: str = data.get("status", "")

        if status == "succeeded":
            return True

        if status in ("processing", "registered"):
            return False

        if status == "failed":
            raise RuntimeError(
                f"Pinterest video processing failed for media_id '{media_id}'. "
                f"The file may be in an unsupported format, exceed size or "
                f"duration limits, or have been corrupted during upload."
            )

        raise RuntimeError(
            f"Unrecognised video status '{status}' for media_id '{media_id}'. "
            f"Full response: {data}"
        )

    def create_video_pin(
        self,
        board_id: str,
        title: str,
        description: str,
        link: str,
        alt_text: str,
        media_id: str,
        cover_image_url: str,
    ) -> str:
        """Create a video Pin referencing a successfully processed media upload (Step 4 of 4).

        Calls ``POST /v5/pins`` with ``source_type: "video_id"`` to link the
        uploaded video (identified by *media_id*) to a board (Section 5.4 of
        the API reference). Must only be called after ``poll_video_status``
        returns ``True``.

        Args:
            board_id: The Pinterest board ID to post this Pin to.
            title: Pin title. Max 100 characters (validated upstream).
            description: Pin description. Max 500 characters. Supports hashtags.
            link: Destination URL shown when a user clicks the Pin.
            alt_text: Accessibility description. Max 500 characters.
            media_id: The ``media_id`` returned by ``register_video_upload``
                whose status has reached ``"succeeded"``.
            cover_image_url: Public URL of the thumbnail image. Required by
                the Pinterest API — a missing or empty value causes HTTP 400.

        Returns:
            The Pinterest ``pin_id`` string of the newly created Pin.

        Raises:
            RuntimeError: For any non-retryable HTTP error.
            RetryableError: For HTTP 429, 500, or 503 responses.
        """
        payload: dict[str, Any] = {
            "board_id": board_id,
            "title": title,
            "description": description,
            "link": link,
            "alt_text": alt_text,
            "media_source": {
                "source_type": "video_id",
                "media_id": media_id,
                "cover_image_url": cover_image_url,
            },
        }
        response = self._session.post(
            f"{PINTEREST_API_BASE_URL}/pins",
            json=payload,
        )
        data = self._handle_response(response)
        self._check_rate_limit(response)
        return data["id"]
