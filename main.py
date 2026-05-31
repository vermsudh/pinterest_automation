"""
Entrypoint for the A.won Pinterest automation script.

Orchestrates the full upload run: authenticates with Google and Pinterest,
reads the pending queue from Google Sheets, resolves board names to IDs,
and processes each row by downloading media from Drive and creating Pins.
Writes Posted/Failed status back to the Sheet and moves files in Drive
accordingly. Prints a structured summary block on completion.

This module is intentionally thin — it reads top-to-bottom like a recipe.
All implementation lives in auth/, services/, uploaders/, and utils/.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from auth.google_auth import (
    build_drive_client,
    build_google_credentials,
    build_sheets_client,
)
from auth.pinterest_auth import ensure_valid_token
from config.settings import (
    DRIVE_FAILED_FOLDER_NAME,
    DRIVE_POSTED_FOLDER_NAME,
    DRIVE_READY_FOLDER_NAME,
    GEMINI_API_KEY,
    SHEET_QUEUE_TAB,
    load_awon_account,
)
from services.caption_generator import fill_missing_captions
from services.drive_service import get_subfolder_id, list_ready_files
from services.pinterest_client import PinterestClient
from services.sheets_service import fetch_pending_rows, mark_skipped
from uploaders.image_uploader import upload_image_pin
from uploaders.video_uploader import upload_video_pin
from utils.logger import setup_logger

# setup_logger configures the root handler on first call so every
# subsequent logging.getLogger() call in other modules inherits the
# formatter.  Must be called before any other module logs a line.
_log = setup_logger(__name__)

# Width of the left-hand label column in the run summary block.
# All labels are padded to this width so the colon aligns on every row.
_SUMMARY_LABEL_WIDTH: int = 20


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _fetch_cover_image_urls(
    sheets_client: Any,
    sheet_id: str,
) -> dict[int, str]:
    """Batch-fetch cover image URLs from column L of the Queue tab.

    Column L is an optional field outside the core schema (columns A–K).
    Reads the entire column in one API call and returns a mapping of
    1-based Sheet row number → stripped URL string, omitting rows where
    the cell is absent or empty.

    If the Sheets API call fails for any reason the function returns an
    empty dict so the caller falls back to empty strings gracefully rather
    than aborting a run that may otherwise be healthy.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID (the ``GOOGLE_SHEET_ID`` env var).

    Returns:
        A ``dict`` mapping row number (int) to cover image URL (str) for
        every row in the Queue tab that has a non-empty value in column L.
        Returns ``{}`` if the column is absent or the API call fails.
    """
    try:
        result: dict = (
            sheets_client.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{SHEET_QUEUE_TAB}!L2:L")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Could not read cover image URLs from column L — "
            "video rows will use empty cover_image_url. Error: %s",
            exc,
        )
        return {}

    urls: dict[int, str] = {}
    # Row 1 is the header; data rows start at Sheet row 2.
    for data_index, row in enumerate(result.get("values", [])):
        if row and row[0].strip():
            sheet_row_number = data_index + 2
            urls[sheet_row_number] = row[0].strip()
    return urls


def _format_duration(elapsed_seconds: int) -> str:
    """Format an elapsed second count as ``HH:MM:SS``.

    Args:
        elapsed_seconds: Total run duration in whole seconds.

    Returns:
        A zero-padded string such as ``"00:01:42"`` or ``"01:05:03"``.
    """
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _print_summary(
    total: int,
    posted: int,
    failed: int,
    skipped: int,
    duration_str: str,
) -> None:
    """Print the structured run summary block to stdout.

    Produces the exact format required by Section 8 of the system
    instruction so GitHub Actions captures it in the step output.

    Args:
        total: Total number of rows processed in this run.
        posted: Rows successfully uploaded as Pinterest Pins.
        failed: Rows that could not be posted after all retries.
        skipped: Rows with an unrecognised ``media_type`` bypassed.
        duration_str: Elapsed time string in ``HH:MM:SS`` format.
    """
    w = _SUMMARY_LABEL_WIDTH
    print("============================")
    print("A.won Pinterest Upload — Run Summary")
    print("============================")
    print(f"{'Total rows processed':<{w}} : {total}")
    print(f"{'Successfully posted':<{w}} : {posted}")
    print(f"{'Failed':<{w}} : {failed}")
    print(f"{'Skipped':<{w}} : {skipped}")
    print(f"{'Run duration':<{w}} : {duration_str}")
    print("============================")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full A.won Pinterest upload batch.

    Orchestrates every phase of the upload run in sequential order:

    1. **Setup** — initialise logging, record start time, load account config.
    2. **Google auth** — build Sheets and Drive API clients from the service
       account JSON stored in the ``GOOGLE_SERVICE_ACCOUNT_JSON`` secret.
    3. **Pinterest token** — load stored tokens from the ``_config`` Sheet
       tab and refresh proactively if expiry is within 24 hours.
    4. **Health check** — call ``GET /v5/user_account`` to confirm the token
       is valid and log the authenticated username.
    5. **Board map** — fetch all Pinterest boards and build a name→ID dict
       used for every row in the batch.
    6. **Drive folders** — resolve the Drive folder IDs for ``Ready/``,
       ``Posted/``, and ``Failed/`` by name under the root folder.
    7. **Queue fetch** — read all ``Pending`` rows from the Queue tab and
       the current file listing from the ``Ready/`` Drive folder.
    8. **Row loop** — route each row to the image or video uploader, track
       posted/failed/skipped counters.
    9. **Summary** — print the structured summary block to stdout.
    10. **Exit code** — ``sys.exit(1)`` if any rows failed so GitHub Actions
        marks the workflow run as failed; ``sys.exit(0)`` otherwise.

    Setup errors (steps 1–7) abort the run immediately with ``sys.exit(1)``
    and a clear log message so the operator can diagnose the problem before
    any rows are processed.  Row-level failures do not stop the loop — they
    are handled inside the uploaders and reflected in the failed counter.

    Raises:
        SystemExit: Always — exits with code 0 on full success or clean
            early termination, and with code 1 on setup failure or any
            row-level failures.
    """
    # ------------------------------------------------------------------
    # STEP 1 — Setup
    # ------------------------------------------------------------------
    _log.info("Starting A.won Pinterest upload run")
    run_start: float = time.monotonic()

    # Wrap the entire setup phase so a single clear error message is
    # logged and the process exits before any rows are touched.
    try:
        account = load_awon_account()

        # ------------------------------------------------------------------
        # STEP 2 — Google Authentication
        # ------------------------------------------------------------------
        credentials = build_google_credentials()
        sheets_client: Any = build_sheets_client(credentials)
        drive_client: Any = build_drive_client(credentials)
        _log.info("Google authentication successful.")

        # ------------------------------------------------------------------
        # STEP 3 — Pinterest Token
        # ------------------------------------------------------------------
        access_token: str = ensure_valid_token(
            sheets_client,
            account.google_sheet_id,
            account.pinterest_client_id,
            account.pinterest_client_secret,
        )
        _log.info("Pinterest authentication successful.")

        # ------------------------------------------------------------------
        # STEP 4 — Pinterest API health check
        # ------------------------------------------------------------------
        pinterest_client = PinterestClient(access_token)
        user_info: dict[str, Any] = pinterest_client.get_user_account()
        _log.info(
            "Authenticated as Pinterest user: %s",
            user_info.get("username", "(unknown)"),
        )

        # ------------------------------------------------------------------
        # STEP 5 — Load board map
        # ------------------------------------------------------------------
        board_map: dict[str, str] = pinterest_client.get_boards()
        if not board_map:
            _log.error(
                "No boards found for this Pinterest account. "
                "Cannot resolve board names — aborting run."
            )
            sys.exit(1)
        _log.info(
            "Found %d board(s): %s",
            len(board_map),
            ", ".join(sorted(board_map.keys())),
        )

        # ------------------------------------------------------------------
        # STEP 6 — Resolve Drive folder IDs
        # ------------------------------------------------------------------
        root_folder_id: str = account.google_drive_folder_id
        ready_folder_id: str = get_subfolder_id(
            drive_client, root_folder_id, DRIVE_READY_FOLDER_NAME
        )
        _log.info(
            "'%s/' folder ID resolved: %s",
            DRIVE_READY_FOLDER_NAME,
            ready_folder_id,
        )
        posted_folder_id: str = get_subfolder_id(
            drive_client, root_folder_id, DRIVE_POSTED_FOLDER_NAME
        )
        _log.info(
            "'%s/' folder ID resolved: %s",
            DRIVE_POSTED_FOLDER_NAME,
            posted_folder_id,
        )
        failed_folder_id: str = get_subfolder_id(
            drive_client, root_folder_id, DRIVE_FAILED_FOLDER_NAME
        )
        _log.info(
            "'%s/' folder ID resolved: %s",
            DRIVE_FAILED_FOLDER_NAME,
            failed_folder_id,
        )

        # ------------------------------------------------------------------
        # STEP 7 — Fetch queue and Drive file listing
        # ------------------------------------------------------------------
        pending_rows = fetch_pending_rows(sheets_client, account.google_sheet_id)
        drive_files: dict[str, str] = list_ready_files(drive_client, ready_folder_id)
        cover_image_urls: dict[int, str] = _fetch_cover_image_urls(
            sheets_client, account.google_sheet_id
        )
        _log.info("Found %d pending row(s) in Queue tab.", len(pending_rows))

        if not pending_rows:
            _log.info("No pending rows found. Exiting.")
            sys.exit(0)

    except Exception as exc:  # noqa: BLE001
        # SystemExit is a BaseException subclass and is NOT caught here.
        # Any other exception during setup (missing env vars, auth failure,
        # missing Drive folders, etc.) lands here.
        _log.error("Setup failed — aborting run: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # STEP 7b — Fill missing captions via Gemini
    # Runs after queue fetch so pending_rows and drive_files are available.
    # Skipped entirely when GEMINI_API_KEY is absent — does not abort the run.
    # ------------------------------------------------------------------
    if not GEMINI_API_KEY:
        _log.warning(
            "GEMINI_API_KEY is not set — skipping automatic caption generation. "
            "Ensure all caption fields are filled manually before running."
        )
    else:
        fill_missing_captions(
            sheets_client=sheets_client,
            sheet_id=account.google_sheet_id,
            drive_client=drive_client,
            ready_folder_id=ready_folder_id,
            pending_rows=pending_rows,
            drive_files=drive_files,
        )

    # ------------------------------------------------------------------
    # STEP 8 — Process each row
    # All row-level failures are handled inside the uploaders.
    # main() tracks counters only; it never catches per-row exceptions.
    # ------------------------------------------------------------------
    posted: int = 0
    failed: int = 0
    skipped: int = 0

    for row in pending_rows:
        if row.media_type == "image":
            success = upload_image_pin(
                row=row,
                board_map=board_map,
                pinterest_client=pinterest_client,
                sheets_client=sheets_client,
                sheet_id=account.google_sheet_id,
                drive_client=drive_client,
                ready_folder_id=ready_folder_id,
                posted_folder_id=posted_folder_id,
                failed_folder_id=failed_folder_id,
                drive_files=drive_files,
            )
            if success:
                posted += 1
            else:
                failed += 1

        elif row.media_type == "video":
            cover_image_url: str = cover_image_urls.get(row.row_number, "")
            success = upload_video_pin(
                row=row,
                board_map=board_map,
                pinterest_client=pinterest_client,
                sheets_client=sheets_client,
                sheet_id=account.google_sheet_id,
                drive_client=drive_client,
                ready_folder_id=ready_folder_id,
                posted_folder_id=posted_folder_id,
                failed_folder_id=failed_folder_id,
                drive_files=drive_files,
                cover_image_url=cover_image_url,
            )
            if success:
                posted += 1
            else:
                failed += 1

        else:
            _log.warning(
                "Row %d | %s | Unrecognised media_type '%s' — marking as Skipped.",
                row.row_number,
                row.image_filename,
                row.media_type,
            )
            mark_skipped(sheets_client, account.google_sheet_id, row.row_number)
            skipped += 1

    # ------------------------------------------------------------------
    # STEP 9 — Print run summary
    # ------------------------------------------------------------------
    total: int = posted + failed + skipped
    elapsed_seconds: int = int(time.monotonic() - run_start)
    duration_str: str = _format_duration(elapsed_seconds)

    _log.info(
        "Run complete. Posted: %d | Failed: %d | Skipped: %d",
        posted,
        failed,
        skipped,
    )
    _print_summary(total, posted, failed, skipped, duration_str)

    # ------------------------------------------------------------------
    # STEP 10 — Exit
    # sys.exit(1) flags the GitHub Actions step as failed when any row
    # could not be posted, giving the operator a visible signal to
    # investigate without scanning the full log.
    # ------------------------------------------------------------------
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
