"""
Syncs new Drive Ready/ files into the Google Sheets Queue tab.

Appends a Pending row (filename, inferred media_type, default destination_link)
for each Drive file absent from the Sheet. Calls fill_missing_captions() when
GEMINI_API_KEY is set so captions are ready before the operator fills board_name.

Run standalone:  python sync_queue.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from auth.google_auth import (
    build_drive_client,
    build_google_credentials,
    build_sheets_client,
)
from config.settings import (
    DRIVE_READY_FOLDER_NAME,
    GEMINI_API_KEY,
    PINTEREST_DEFAULT_BOARD,
    PINTEREST_DESTINATION_URL,
    SHEET_QUEUE_TAB,
    load_awon_account,
)
from services.caption_generator import fill_missing_captions
from services.drive_service import get_subfolder_id, list_ready_files
from services.sheets_service import fetch_pending_rows
from utils.logger import setup_logger

_log = setup_logger(__name__)

_SUMMARY_LABEL_WIDTH: int = 26
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
_VIDEO_EXTENSIONS: frozenset[str] = frozenset({"mp4", "mov", "avi", "mkv"})


def _fetch_sheet_filenames(sheets_client: Any, sheet_id: str) -> set[str]:
    """Return all filenames in column A of the Queue tab, all statuses.

    Reads every row so Posted/Failed/Skipped files are never re-added.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID.

    Returns:
        Set of stripped filename strings from column A, rows 2 onwards.
    """
    result: dict = (
        sheets_client.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{SHEET_QUEUE_TAB}!A2:A")
        .execute()
    )
    return {
        row[0].strip()
        for row in result.get("values", [])
        if row and row[0].strip()
    }


def _infer_media_type(filename: str) -> str | None:
    """Return 'image' or 'video' from the file extension, or None if unknown."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    return None


def _append_queue_row(
    sheets_client: Any,
    sheet_id: str,
    filename: str,
    media_type: str,
    default_board: str,
) -> None:
    """Append one Pending row to the Queue tab for a new Drive file.

    C/D/G (title, description, alt_text) are left blank; E (board_name)
    is set to ``default_board``; F defaults to PINTEREST_DESTINATION_URL;
    H is set to 'Pending'.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID.
        filename: Exact Drive filename for column A.
        media_type: 'image' or 'video' for column B.
        default_board: Board name written into column E.  Must match an
            existing Pinterest board name exactly (case-sensitive).
    """
    (
        sheets_client.spreadsheets()
        .values()
        .append(
            spreadsheetId=sheet_id,
            range=f"{SHEET_QUEUE_TAB}!A:H",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[filename, media_type, "", "", default_board, PINTEREST_DESTINATION_URL, "", "Pending"]]},
        )
        .execute()
    )


def _print_summary(
    new_files: int,
    already_in_sheet: int,
    captions_generated: int,
    rows_needing_board_name: int,
) -> None:
    """Print the structured sync summary block to stdout."""
    w = _SUMMARY_LABEL_WIDTH
    print("============================")
    print("A.won Queue Sync — Summary")
    print("============================")
    print(f"{'New files found in Drive':<{w}} : {new_files}")
    print(f"{'Already in Sheet (skipped)':<{w}} : {already_in_sheet}")
    print(f"{'Captions generated':<{w}} : {captions_generated}")
    print(f"{'Rows needing board_name':<{w}} : {rows_needing_board_name}")
    print("============================")


def sync_queue_with_clients(
    sheets_client: Any,
    drive_client: Any,
    sheet_id: str,
    ready_folder_id: str,
    drive_files: dict[str, str],
    default_board: str = "",
) -> int:
    """Sync new Drive Ready/ files into the Google Sheets Queue tab.

    Compares ``drive_files`` against every filename already present in the
    Queue tab (all statuses) and appends a Pending row for each file not yet
    tracked.  Rows with unrecognised file extensions are skipped with a
    warning.  Per-file append failures are logged and skipped without aborting.

    This is a pure library function — it never calls sys.exit() and never
    performs authentication.  Call it after clients and folder IDs have already
    been resolved.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        drive_client: Authenticated Google Drive API client (reserved for
            future use; not called directly in this function).
        sheet_id: Google Sheets document ID.
        ready_folder_id: Drive folder ID for the Ready/ subfolder (reserved
            for future use; not called directly in this function).
        drive_files: Mapping of filename → Drive file ID for every file
            currently in the Ready/ folder.
        default_board: Board name written into column E of each new row.
            Must match an existing Pinterest board name exactly
            (case-sensitive).  Defaults to ``""`` so callers that do not
            supply a board leave the cell blank rather than raising.

    Returns:
        The number of rows successfully appended to the Queue tab.
    """
    if not drive_files:
        _log.info("Ready/ folder is empty — nothing to sync.")
        return 0

    sheet_filenames: set[str] = _fetch_sheet_filenames(sheets_client, sheet_id)
    new_filenames: list[str] = [
        name for name in drive_files if name not in sheet_filenames
    ]

    if not new_filenames:
        _log.info("All %d Drive file(s) already present in Sheet.", len(drive_files))
        return 0

    appended: int = 0
    for filename in new_filenames:
        media_type = _infer_media_type(filename)
        if media_type is None:
            _log.warning("Skipping '%s' — unrecognised file extension.", filename)
            continue
        try:
            _append_queue_row(sheets_client, sheet_id, filename, media_type, default_board)
            appended += 1
            _log.info("Appended row for '%s' (media_type=%s).", filename, media_type)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to append row for '%s': %s", filename, exc)

    return appended


def sync_queue() -> None:
    """Sync new Drive Ready/ files into the Google Sheets Queue tab.

    Appends one Pending row per file absent from the Sheet. Row failures log a
    warning and continue; setup failures abort with sys.exit(1).
    """
    _log.info("Starting A.won Queue Sync")

    try:
        account = load_awon_account()
        credentials = build_google_credentials()
        sheets_client: Any = build_sheets_client(credentials)
        drive_client: Any = build_drive_client(credentials)
        _log.info("Google authentication successful.")
        ready_folder_id: str = get_subfolder_id(
            drive_client, account.google_drive_folder_id, DRIVE_READY_FOLDER_NAME
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("Setup failed — aborting sync: %s", exc)
        sys.exit(1)

    drive_files: dict[str, str] = list_ready_files(drive_client, ready_folder_id)
    if not drive_files:
        _log.info("Ready/ folder is empty — nothing to sync.")
        _print_summary(0, 0, 0, 0)
        return

    sheet_filenames: set[str] = _fetch_sheet_filenames(
        sheets_client, account.google_sheet_id
    )
    already_in_sheet: int = sum(1 for name in drive_files if name in sheet_filenames)

    appended_count: int = sync_queue_with_clients(
        sheets_client=sheets_client,
        drive_client=drive_client,
        sheet_id=account.google_sheet_id,
        ready_folder_id=ready_folder_id,
        drive_files=drive_files,
        default_board=PINTEREST_DEFAULT_BOARD,
    )

    captions_generated: int = 0
    if appended_count and GEMINI_API_KEY:
        _log.info("Gemini API key found — generating captions for new rows.")
        try:
            pending_rows = fetch_pending_rows(sheets_client, account.google_sheet_id)
            fill_missing_captions(
                sheets_client=sheets_client,
                sheet_id=account.google_sheet_id,
                drive_client=drive_client,
                ready_folder_id=ready_folder_id,
                pending_rows=pending_rows,
                drive_files=drive_files,
            )
            captions_generated = sum(
                1 for name in drive_files
                if name not in sheet_filenames and _infer_media_type(name) == "image"
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Caption generation failed: %s", exc)
    elif not GEMINI_API_KEY:
        _log.warning(
            "GEMINI_API_KEY not set — skipping caption generation. "
            "Fill title, description, and alt_text manually in the Sheet."
        )

    _print_summary(appended_count, already_in_sheet, captions_generated, appended_count)


if __name__ == "__main__":
    sync_queue()
