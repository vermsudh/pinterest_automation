"""
Reads and writes the A.won Pinterest upload queue in Google Sheets.

Responsible for two concerns:
  1. Reading — fetches rows from the Queue tab where status == 'Pending'
     and returns them as structured PinRow dataclasses.
  2. Writing — updates individual cells (status, pin_id, posted_at,
     error_message) after each upload attempt, using batch API calls
     so each status change is a single HTTP request.

All column indices and letter references are defined as private module
constants so that a Sheet schema change requires editing only this file.
The tab name is imported from config.settings so it matches the rest of
the project without duplication.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config.settings import SHEET_QUEUE_TAB

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column layout — Queue tab
# ---------------------------------------------------------------------------
# 0-based indices used when reading rows from the Sheets API response.
# The Sheets API omits trailing empty cells, so every read goes through
# _get_cell() which returns '' for any out-of-bounds index.

_IDX_IMAGE_FILENAME: int = 0   # Column A
_IDX_MEDIA_TYPE: int = 1        # Column B
_IDX_TITLE: int = 2             # Column C
_IDX_DESCRIPTION: int = 3       # Column D
_IDX_BOARD_NAME: int = 4        # Column E
_IDX_DESTINATION_LINK: int = 5  # Column F
_IDX_ALT_TEXT: int = 6          # Column G
_IDX_STATUS: int = 7            # Column H
_IDX_PIN_ID: int = 8            # Column I
_IDX_POSTED_AT: int = 9         # Column J
_IDX_ERROR_MESSAGE: int = 10    # Column K

# Column letters used when constructing write ranges.
_COL_STATUS: str = "H"
_COL_PIN_ID: str = "I"
_COL_POSTED_AT: str = "J"
_COL_ERROR_MESSAGE: str = "K"

# Status values read from and written to column H.
_STATUS_PENDING: str = "Pending"
_STATUS_POSTED: str = "Posted"
_STATUS_FAILED: str = "Failed"
_STATUS_SKIPPED: str = "Skipped"

# The Queue tab has a header row in row 1; data rows start at row 2.
_HEADER_ROWS: int = 1


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PinRow:
    """One content row from the Queue tab of the Google Sheet.

    Represents a single Pin to be uploaded. Fields that the script writes
    back (pin_id, posted_at, error_message) may arrive as empty strings
    when a row is first read — the dataclass does not enforce non-empty
    values for those fields.

    Attributes:
        row_number: 1-based Sheet row number (row 1 is the header, so the
            first data row is row_number=2). Used in log output and in
            range strings for write-back calls.
        image_filename: Exact filename as stored in the Drive Ready/ folder.
        media_type: 'image' or 'video'.
        title: Pin title. Validated to <= 100 characters before API call.
        description: Pin description. Validated to <= 500 characters.
        board_name: Human-readable board name resolved to a board_id at
            startup via the Pinterest boards list.
        destination_link: URL the Pin links to. Must start with 'https://'.
        alt_text: Accessibility text. Validated to <= 500 characters.
        status: Current status cell value. Always 'Pending' when returned
            by fetch_pending_rows().
        pin_id: Pinterest Pin ID, written back after a successful post.
            Empty string on first read.
        posted_at: ISO 8601 UTC timestamp, written back after a successful
            post. Empty string on first read.
        error_message: Human-readable error, written back on failure.
            Empty string on first read.
    """

    row_number: int
    image_filename: str
    media_type: str
    title: str
    description: str
    board_name: str
    destination_link: str
    alt_text: str
    status: str
    pin_id: str
    posted_at: str
    error_message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_cell(row: list[str], index: int) -> str:
    """Safely extract a cell value from a Sheets API row.

    The Sheets API omits trailing empty cells from each row, so any column
    beyond the last non-empty cell is simply absent from the list. This
    helper returns an empty string for those absent columns rather than
    raising an IndexError.

    Args:
        row: One row from the 'values' list returned by the Sheets API.
        index: 0-based column index to read.

    Returns:
        The stripped cell value, or '' if the column is absent.
    """
    if index >= len(row):
        return ""
    return row[index].strip()


def _queue_range(col: str, row_number: int) -> str:
    """Build a Sheets API range string for a single cell in the Queue tab.

    Args:
        col: Column letter (e.g. 'H').
        row_number: 1-based Sheet row number.

    Returns:
        A range string such as 'Queue!H5'.
    """
    return f"{SHEET_QUEUE_TAB}!{col}{row_number}"


def _queue_span(col_start: str, col_end: str, row_number: int) -> str:
    """Build a Sheets API range string for a contiguous column span.

    Args:
        col_start: First column letter (e.g. 'H').
        col_end: Last column letter (e.g. 'J').
        row_number: 1-based Sheet row number.

    Returns:
        A range string such as 'Queue!H5:J5'.
    """
    return f"{SHEET_QUEUE_TAB}!{col_start}{row_number}:{col_end}{row_number}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_pending_rows(sheets_client: Any, sheet_id: str) -> list[PinRow]:
    """Read all Pending rows from the Queue tab.

    Fetches the entire data region (Queue!A2:K) in one API call, then
    filters client-side for rows where column H == 'Pending'. Rows where
    column A (image_filename) is empty are logged as a warning and skipped
    — they cannot be processed without a filename.

    The row_number in each returned PinRow reflects the actual Google Sheet
    row (first data row = 2, not 0 or 1), so it can be used directly in
    write-back range strings.

    Args:
        sheets_client: Authenticated Google Sheets API client built by
            auth.google_auth.build_sheets_client().
        sheet_id: Google Sheets document ID (GOOGLE_SHEET_ID env var).

    Returns:
        A list of PinRow instances for every row where status == 'Pending',
        in Sheet order. Empty list if there are no pending rows.
    """
    result = (
        sheets_client.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_QUEUE_TAB}!A2:K",
        )
        .execute()
    )
    all_rows: list[list[str]] = result.get("values", [])
    pending: list[PinRow] = []

    for data_index, row in enumerate(all_rows):
        # Row 1 is the header; the first data row maps to Sheet row 2.
        sheet_row_number = data_index + _HEADER_ROWS + 1

        status = _get_cell(row, _IDX_STATUS)
        if status != _STATUS_PENDING:
            continue

        image_filename = _get_cell(row, _IDX_IMAGE_FILENAME)
        if not image_filename:
            logger.warning(
                "Row %d | Skipping Pending row: column A (image_filename) is empty.",
                sheet_row_number,
            )
            continue

        pending.append(
            PinRow(
                row_number=sheet_row_number,
                image_filename=image_filename,
                media_type=_get_cell(row, _IDX_MEDIA_TYPE),
                title=_get_cell(row, _IDX_TITLE),
                description=_get_cell(row, _IDX_DESCRIPTION),
                board_name=_get_cell(row, _IDX_BOARD_NAME),
                destination_link=_get_cell(row, _IDX_DESTINATION_LINK),
                alt_text=_get_cell(row, _IDX_ALT_TEXT),
                status=status,
                pin_id=_get_cell(row, _IDX_PIN_ID),
                posted_at=_get_cell(row, _IDX_POSTED_AT),
                error_message=_get_cell(row, _IDX_ERROR_MESSAGE),
            )
        )

    logger.info(
        "Found %d row(s) with status '%s' in %s.",
        len(pending),
        _STATUS_PENDING,
        SHEET_QUEUE_TAB,
    )
    return pending


def mark_posted(
    sheets_client: Any,
    sheet_id: str,
    row_number: int,
    pin_id: str,
) -> None:
    """Write a successful outcome back to the Sheet for one row.

    Updates three contiguous cells in a single batchUpdate API call:
      - Column H (status)    ← 'Posted'
      - Column I (pin_id)    ← the Pinterest Pin ID returned by the API
      - Column J (posted_at) ← current UTC timestamp in ISO 8601 format

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID.
        row_number: 1-based Sheet row number of the row to update.
        pin_id: The Pin ID string returned by the Pinterest create-pin API.
    """
    posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "valueInputOption": "RAW",
        "data": [
            {
                "range": _queue_span(_COL_STATUS, _COL_POSTED_AT, row_number),
                "values": [[_STATUS_POSTED, pin_id, posted_at]],
            }
        ],
    }
    (
        sheets_client.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=sheet_id, body=body)
        .execute()
    )
    logger.info(
        "Row %d | Wrote status='%s', pin_id='%s', posted_at='%s'.",
        row_number,
        _STATUS_POSTED,
        pin_id,
        posted_at,
    )


def mark_failed(
    sheets_client: Any,
    sheet_id: str,
    row_number: int,
    error_message: str,
) -> None:
    """Write a failure outcome back to the Sheet for one row.

    Updates two non-contiguous cells in a single batchUpdate API call:
      - Column H (status)        ← 'Failed'
      - Column K (error_message) ← the human-readable error description

    Columns I and J (pin_id, posted_at) are intentionally left blank so
    the row clearly shows it was never successfully posted.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID.
        row_number: 1-based Sheet row number of the row to update.
        error_message: Human-readable description of what went wrong.
            Should be specific enough for the operator to diagnose without
            reading the full log.
    """
    body: dict[str, Any] = {
        "valueInputOption": "RAW",
        "data": [
            {
                "range": _queue_range(_COL_STATUS, row_number),
                "values": [[_STATUS_FAILED]],
            },
            {
                "range": _queue_range(_COL_ERROR_MESSAGE, row_number),
                "values": [[error_message]],
            },
        ],
    }
    (
        sheets_client.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=sheet_id, body=body)
        .execute()
    )
    logger.info(
        "Row %d | Wrote status='%s', error_message='%s'.",
        row_number,
        _STATUS_FAILED,
        error_message,
    )


def mark_skipped(
    sheets_client: Any,
    sheet_id: str,
    row_number: int,
) -> None:
    """Write 'Skipped' to column H for a row that was intentionally bypassed.

    Used when a row is deliberately not processed during a run (e.g. a
    row that was already in a non-Pending state at fetch time but needs
    an explicit status for audit purposes). Does not touch any other cell.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID.
        row_number: 1-based Sheet row number of the row to update.
    """
    (
        sheets_client.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=_queue_range(_COL_STATUS, row_number),
            valueInputOption="RAW",
            body={"values": [[_STATUS_SKIPPED]]},
        )
        .execute()
    )
    logger.info(
        "Row %d | Wrote status='%s'.",
        row_number,
        _STATUS_SKIPPED,
    )
