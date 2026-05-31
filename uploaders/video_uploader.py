"""
Handles the 4-step video Pin creation flow for a single queue row.

Step 1 — Register intent:
    POST /v5/media with media_type='video'. Saves media_id and
    upload_url/upload_parameters from the response.

Step 2 — Upload to S3:
    Download the video file from Google Drive into memory and POST it
    to the AWS upload_url using multipart/form-data.  No Pinterest
    Authorization header is sent for this request.  S3 pre-signed URLs
    are single-use, so this step is never retried.

Step 3 — Poll processing status:
    GET /v5/media/{media_id} every VIDEO_POLL_INTERVAL_SECONDS seconds,
    up to VIDEO_POLL_MAX_ATTEMPTS attempts (5 minutes at default settings).
    Proceeds only when status == 'succeeded'.  Marks row Failed on timeout
    or status == 'failed'.

Step 4 — Create the video Pin:
    POST /v5/pins with source_type='video_id' and the media_id.
    cover_image_url is passed as-is — an empty string causes HTTP 400
    from Pinterest (operator must supply a valid URL in the Sheet).

A single row failing never stops the batch — upload_video_pin() always
returns True/False and never re-raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config.settings import VIDEO_POLL_INTERVAL_SECONDS, VIDEO_POLL_MAX_ATTEMPTS
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


def upload_video_pin(
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
    cover_image_url: str,
) -> bool:
    """Process one video-Pin queue row through the 4-step Pinterest flow.

    Handles the complete lifecycle of a single ``PinRow`` whose
    ``media_type`` is ``"video"``:

    1. **Validate** — all fields checked locally before any network call.
       Validation failures do not move the Drive file because the filename
       in the Sheet may not correspond to a real file in Drive.
    2. **Resolve** — the Drive file ID is looked up in *drive_files*.
       A missing filename fails the row without touching Drive.
    3. **Download** — the full video bytes are loaded into memory via
       ``download_file_to_memory()``.  Nothing is written to disk.
    4. **Register** — ``POST /v5/media`` obtains the S3 pre-signed URL and
       ``media_id`` (wrapped with retry for transient Pinterest errors).
    5. **S3 upload** — the video bytes are POSTed directly to AWS S3
       without a Pinterest ``Authorization`` header.  Not retried because
       S3 pre-signed URLs are single-use.
    6. **Poll** — ``GET /v5/media/{media_id}`` is called repeatedly until
       Pinterest reports ``status == "succeeded"``.  Times out after
       ``VIDEO_POLL_MAX_ATTEMPTS`` × ``VIDEO_POLL_INTERVAL_SECONDS`` seconds.
    7. **Create Pin** — ``POST /v5/pins`` with ``source_type: "video_id"``
       (wrapped with retry).  *cover_image_url* is passed as-is; an empty
       string will cause Pinterest to return HTTP 400.
    8. **Write-back** — on success ``mark_posted()`` records the pin_id;
       on failure ``mark_failed()`` records the error.
    9. **Drive move** — on success the file moves to ``Posted/``; on failure
       it moves to ``Failed/`` (when a file_id was successfully resolved).

    This function never re-raises.  Every exception is caught, logged at
    WARNING level, written back to the Sheet, and returned as ``False`` so
    the caller's batch loop continues to the next row uninterrupted.

    Args:
        row: The ``PinRow`` to process.  ``row.media_type`` should be
            ``"video"``; the caller is responsible for routing correctly.
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
        cover_image_url: Public URL of the thumbnail image shown on the
            Pin before playback.  Required by the Pinterest API — an empty
            string will cause a HTTP 400 error at step 7.  The operator
            must supply a valid URL in the Sheet; this function logs a
            WARNING if the value is empty but does not block the attempt.

    Returns:
        ``True`` if the video Pin was created successfully and the Sheet
        row was marked ``Posted``.
        ``False`` for any failure: validation error, missing Drive file,
        download error, S3 upload failure, processing timeout, API error
        (including exhausted retries), or any unexpected exception.
    """
    _log.info(
        "Row %d | %s | Starting video upload.",
        row.row_number,
        row.image_filename,
    )

    # ------------------------------------------------------------------
    # Step 1 — Validate all row fields before any network call.
    # Do NOT move the Drive file on a validation failure: the filename in
    # the Sheet may not correspond to any file in Drive.
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
    # If the filename is absent from drive_files the row is failed without
    # touching Drive (there is no file to move).
    # ------------------------------------------------------------------
    file_id: str | None = drive_files.get(row.image_filename)
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
    # Steps 3–8 — Download, register, S3 upload, poll, create Pin.
    # file_id is known from this point.  Any unhandled exception is caught
    # by the broad handlers at the bottom and results in mark_failed +
    # move_to_failed + return False.
    # ------------------------------------------------------------------
    try:
        # Step 3: Download the full video into memory.
        # NOTE: This loads the entire file into RAM. For v1 this is
        # acceptable (max 2 GB, runs on GitHub Actions ubuntu-latest with
        # 7 GB RAM). Revisit for v2 — consider chunked streaming to S3.
        video_bytes: bytes = download_file_to_memory(drive_client, file_id)

        # Resolve board_id here — validate_row() already confirmed the key
        # exists in board_map, so this lookup cannot raise KeyError.
        board_id: str = board_map[row.board_name]

        # Step 4: Register video upload intent with Pinterest (retryable).
        # Obtains the S3 pre-signed URL and policy parameters, plus the
        # media_id that links this upload to the eventual Pin.
        register_with_retry = with_retry(max_attempts=5)(
            pinterest_client.register_video_upload
        )
        media_id: str
        upload_url: str
        upload_parameters: dict[str, str]
        media_id, upload_url, upload_parameters = register_with_retry()

        # Step 5: Upload the video bytes directly to AWS S3.
        # NOT retried — S3 pre-signed URLs are single-use.  A failure here
        # raises RuntimeError, which propagates to the except Exception
        # handler below, marking the row Failed and moving the file.
        pinterest_client.upload_video_to_s3(
            upload_url, upload_parameters, video_bytes
        )

        # Step 6: Poll until Pinterest finishes processing the video.
        _log.info(
            "Row %d | %s | Polling for video processing status "
            "(max %d attempts, %ds interval).",
            row.row_number,
            row.image_filename,
            VIDEO_POLL_MAX_ATTEMPTS,
            VIDEO_POLL_INTERVAL_SECONDS,
        )
        for attempt in range(1, VIDEO_POLL_MAX_ATTEMPTS + 1):
            # Sleep before every attempt except the first so the initial
            # check is immediate and subsequent checks respect the interval.
            if attempt > 1:
                time.sleep(VIDEO_POLL_INTERVAL_SECONDS)

            # Periodic log every 10 attempts to confirm the run is alive.
            if attempt % 10 == 0:
                _log.info(
                    "Row %d | %s | Waiting for video processing... "
                    "attempt %d/%d",
                    row.row_number,
                    row.image_filename,
                    attempt,
                    VIDEO_POLL_MAX_ATTEMPTS,
                )

            # poll_video_status returns True (succeeded), False (still
            # processing), or raises RuntimeError (status == 'failed' or
            # unrecognised).  RuntimeError propagates to except Exception.
            if pinterest_client.poll_video_status(media_id):
                _log.info(
                    "Row %d | %s | Video processing succeeded "
                    "(attempt %d/%d).",
                    row.row_number,
                    row.image_filename,
                    attempt,
                    VIDEO_POLL_MAX_ATTEMPTS,
                )
                break

        else:
            # for…else: the loop ran to completion without a break, meaning
            # all VIDEO_POLL_MAX_ATTEMPTS polls returned False (still
            # processing).  Treat this as a terminal failure.
            timeout_seconds = VIDEO_POLL_MAX_ATTEMPTS * VIDEO_POLL_INTERVAL_SECONDS
            error_msg = (
                f"Video processing timed out after {VIDEO_POLL_MAX_ATTEMPTS} "
                f"attempts ({timeout_seconds}s). "
                f"media_id='{media_id}'. "
                f"The file may be too large or Pinterest processing is degraded."
            )
            _log.warning(
                "Row %d | %s → Failed | Error: %s",
                row.row_number,
                row.image_filename,
                error_msg,
            )
            mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
            move_to_failed(drive_client, file_id, ready_folder_id, failed_folder_id)
            return False

        # Step 7: Validate cover_image_url and warn if absent.
        # The Pinterest API requires a non-empty cover_image_url for video
        # Pins — an empty string causes HTTP 400.  We warn here (not fail)
        # so the operator sees the cause when the subsequent API call fails.
        if not cover_image_url:
            _log.warning(
                "Row %d | %s | No cover image URL provided. "
                "The Pinterest API requires cover_image_url for video Pins — "
                "this row will likely fail with HTTP 400. "
                "Add a thumbnail URL to the Sheet and re-run.",
                row.row_number,
                row.image_filename,
            )

        # Step 8: Create the video Pin (retryable).
        create_with_retry = with_retry(max_attempts=5)(
            pinterest_client.create_video_pin
        )
        pin_id: str = create_with_retry(
            board_id=board_id,
            title=row.title,
            description=row.description,
            link=row.destination_link,
            alt_text=row.alt_text,
            media_id=media_id,
            cover_image_url=cover_image_url,
        )

    except RetryableError as exc:
        # Step 10: RetryableError re-raised after all backoff attempts are
        # exhausted (from register_video_upload, poll_video_status GET, or
        # create_video_pin).
        error_msg = str(exc)
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            error_msg,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
        if file_id is not None:
            move_to_failed(drive_client, file_id, ready_folder_id, failed_folder_id)
        return False

    except Exception as exc:  # noqa: BLE001
        # Step 11: Any other exception — RuntimeError from non-retryable HTTP
        # status codes (400/403/404), S3 upload failures, Drive download
        # errors, video processing failures, or truly unexpected errors.
        # Never re-raised so a single row cannot abort the batch.
        error_msg = str(exc)
        _log.warning(
            "Row %d | %s → Failed | Error: %s",
            row.row_number,
            row.image_filename,
            error_msg,
        )
        mark_failed(sheets_client, sheet_id, row.row_number, error_msg)
        if file_id is not None:
            move_to_failed(drive_client, file_id, ready_folder_id, failed_folder_id)
        return False

    # ------------------------------------------------------------------
    # Step 9 — Success path.
    # Only reached when every step above completed without exception and
    # without an early return.
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
