"""
Handles the full image Pin creation flow for a single queue row.

1. Validates all Pin fields via validators.py before touching any API.
2. Resolves the Drive file ID from the pre-fetched ready-files mapping.
3. Downloads the image file into memory (bytes) — nothing is written to disk.
4. Detects the MIME type from the filename extension.
5. Calls pinterest_client to POST /v5/pins with source_type=image_base64,
   wrapping the call with with_retry so HTTP 429/500/503 are retried.
6. On success: writes Posted status back to the Sheet and moves the Drive
   file to Posted/.
7. On failure: writes Failed status + error message to the Sheet and moves
   the Drive file to Failed/.

A single row failing never stops the batch — upload_image_pin() always
returns True/False and never re-raises.

Supported formats: JPEG, PNG, WEBP (up to 32 MB, per API reference §9).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services.drive_service import (
    download_file_to_memory,
    move_to_failed,
    move_to_posted,
)
from services.pinterest_client import PinterestClient, RetryableError
from services.sheets_service import PinRow, mark_failed, mark_posted
from utils.retry import with_retry
from utils.validators import ValidationError, validate_row

_log = logging.getLogger(__name__)

# Mapping of lowercase file extension → Pinterest API content_type value.
# Keys include the leading dot as returned by pathlib.Path.suffix.
# Source: API reference Section 4.2 — supported image formats.
_CONTENT_TYPE_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_DEFAULT_CONTENT_TYPE: str = "image/jpeg"


def _detect_content_type(filename: str, row_number: int) -> str:
    """Derive the Pinterest API ``content_type`` value from a filename's extension.

    Uses ``pathlib.Path.suffix`` so the extension is extracted without any
    hardcoded path-separator assumptions.  The lookup is case-insensitive —
    ``.JPG`` is treated identically to ``.jpg``.

    Args:
        filename: The bare filename as stored in the Queue tab column A,
            e.g. ``"khan-living-room.JPG"`` or ``"facade-render.png"``.
        row_number: 1-based Sheet row number used only in the warning log
            message so the operator can locate the row immediately.

    Returns:
        A MIME type string accepted by ``media_source.content_type`` in the
        Pinterest image_base64 API call.  Returns ``"image/jpeg"`` as a safe
        default for any unrecognised extension.
    """
    suffix = Path(filename).suffix.lower()
    content_type = _CONTENT_TYPE_MAP.get(suffix)
    if content_type is None:
        _log.warning(
            "Row %d | Unrecognised file extension '%s' for '%s'. "
            "Defaulting content_type to '%s'.",
            row_number,
            suffix or "(none)",
            filename,
            _DEFAULT_CONTENT_TYPE,
        )
        return _DEFAULT_CONTENT_TYPE
    return content_type


def upload_image_pin(
    row: PinRow,
    board_map: dict[str, str],
    pinterest_client: PinterestClient,
    sheets_client: Any,
    sheet_id: str,
    drive_client: Any,
    ready_folder_id: str,
    posted_folder_id: str,
    failed_folder_id: str,
    drive_files: dict[str, str],
) -> bool:
    """Process one image-Pin queue row end-to-end and report success or failure.

    Handles the complete lifecycle of a single ``PinRow`` whose
    ``media_type`` is ``"image"``:

    1. **Validate** — all fields are checked locally before any network call.
       Validation failures do not move the Drive file because the file may
       not exist in Drive at all (the filename could simply be wrong in the
       Sheet).
    2. **Resolve** — the Drive file ID is looked up in *drive_files* (the
       pre-fetched ``Ready/`` listing).  A missing filename is treated as a
       failure and the row is marked without touching Drive.
    3. **Download** — the file bytes are fetched into memory via
       ``download_file_to_memory()``.  Nothing is written to disk.
    4. **Content-type detection** — derived from the filename extension via
       ``_detect_content_type()``.
    5. **Upload** — ``PinterestClient.create_image_pin()`` is called with the
       ``with_retry`` decorator applied at the call site so HTTP 429/500/503
       responses trigger up to 4 additional attempts with exponential backoff.
    6. **Sheet write-back** — on success ``mark_posted()`` records the pin_id
       and timestamp; on failure ``mark_failed()`` records the error message.
    7. **Drive move** — on success the file moves to ``Posted/``; on failure
       it moves to ``Failed/``.

    This function never re-raises.  All recoverable and unrecoverable errors
    are caught, logged at WARNING level, written back to the Sheet, and
    reported as ``False`` so the caller's batch loop can continue to the next
    row uninterrupted.

    Args:
        row: The ``PinRow`` to process.  ``row.media_type`` should be
            ``"image"``; the caller is responsible for routing correctly.
        board_map: Name→ID mapping built at startup from ``GET /v5/boards``,
            e.g. ``{"Interiors": "549755885175"}``.
        pinterest_client: Authenticated ``PinterestClient`` instance shared
            across all rows in the current run.
        sheets_client: Authenticated Google Sheets API client used for
            ``mark_posted`` / ``mark_failed`` write-backs.
        sheet_id: Google Sheets document ID (the ``GOOGLE_SHEET_ID`` env var).
        drive_client: Authenticated Google Drive API client used for
            downloading and moving files.
        ready_folder_id: Drive folder ID of ``Ready/`` — the source of the
            file before it is processed.
        posted_folder_id: Drive folder ID of ``Posted/`` — destination on
            success.
        failed_folder_id: Drive folder ID of ``Failed/`` — destination on
            failure after all retries are exhausted.
        drive_files: Pre-fetched mapping of filename → Drive file ID for
            every file currently in ``Ready/``, as returned by
            ``list_ready_files()``.  Passed in rather than fetched per-row
            to avoid a Drive API call for every row.

    Returns:
        ``True`` if the Pin was created successfully and the Sheet row was
        marked ``Posted``.
        ``False`` for any failure: validation error, missing Drive file,
        download error, API error (including exhausted retries), or any
        unexpected exception.
    """
    _log.info(
        "Row %d | %s | Starting image upload.",
        row.row_number,
        row.image_filename,
    )

    # ------------------------------------------------------------------
    # Step 1 — Validate the row fields before any API or Drive call.
    # Do NOT move the Drive file on a validation failure: the filename in
    # the Sheet may not correspond to any real file in Drive.
    # ------------------------------------------------------------------
    try:
        validate_row(row, board_map)
    except ValidationError as exc:
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            exc,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, str(exc))
        return False

    # ------------------------------------------------------------------
    # Step 2 — Resolve Drive file ID from the pre-fetched listing.
    # No Drive move on a missing-file failure either — there is no file
    # to move.
    # ------------------------------------------------------------------
    file_id = drive_files.get(row.image_filename)
    if file_id is None:
        error_msg = (
            f"File '{row.image_filename}' was not found in the Ready/ folder. "
            f"Ensure the filename in column A exactly matches the Drive filename "
            f"(including extension and capitalisation)."
        )
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            error_msg,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
        return False

    # ------------------------------------------------------------------
    # Steps 3–6 — Download, detect MIME type, resolve board, upload.
    # Any exception here (Drive error, API error, exhausted retries)
    # results in the row being marked Failed and the file moved to Failed/.
    # ------------------------------------------------------------------
    try:
        # Step 3: Download the image into memory.
        image_bytes = download_file_to_memory(drive_client, file_id)

        # Step 4: Derive MIME type from filename extension.
        content_type = _detect_content_type(row.image_filename, row.row_number)

        # Step 5: Resolve board_id — board_map key membership was already
        # confirmed by validate_row(), so this lookup cannot KeyError.
        board_id = board_map[row.board_name]

        # Step 6: Upload with retry applied at the call site on the bound method.
        create_with_retry = with_retry(max_attempts=5)(
            pinterest_client.create_image_pin
        )
        pin_id: str = create_with_retry(
            board_id=board_id,
            title=row.title,
            description=row.description,
            link=row.destination_link,
            alt_text=row.alt_text,
            image_data=image_bytes,
            content_type=content_type,
        )

    except RetryableError as exc:
        # Step 8: All retry attempts exhausted.
        error_msg = str(exc)
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            error_msg,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
        move_to_failed(drive_client, file_id, ready_folder_id, failed_folder_id)
        return False

    except Exception as exc:  # noqa: BLE001
        # Step 9: Any other unexpected error (RuntimeError from non-retryable
        # HTTP status, Drive download failure, etc.).  Never re-raised so
        # a single row cannot abort the batch.
        error_msg = str(exc)
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            error_msg,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
        move_to_failed(drive_client, file_id, ready_folder_id, failed_folder_id)
        return False

    # ------------------------------------------------------------------
    # Step 7 — Success path. Only reached when no exception was raised.
    # ------------------------------------------------------------------
    mark_posted(sheets_client, sheet_id, row.row_number, pin_id)
    move_to_posted(drive_client, file_id, ready_folder_id, posted_folder_id)
    _log.info(
        "Row %d | %s → Posted | Pin ID: %s",
        row.row_number,
        row.image_filename,
        pin_id,
    )
    return True
