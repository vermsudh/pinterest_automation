"""
Validates Pin fields locally before any Pinterest API call is made.

Checks performed on each queue row:
  - image_filename  : non-empty string
  - media_type      : exactly 'image' or 'video'
  - title           : <= 100 characters
  - description     : <= 500 characters
  - alt_text        : <= 500 characters
  - destination_link: starts with 'https://'
  - board_name      : present as a key in the board name→ID map

All checks run on every row before any is reported — callers receive the
complete list of violations in one exception rather than discovering them
one at a time. The caller writes the joined error to column K
(error_message) and marks the row Failed without touching the Pinterest API.

Also provides mask_token(), the single source of truth for safely logging
token strings without exposing their full value.
"""

from __future__ import annotations

import logging

from services.sheets_service import PinRow

_log = logging.getLogger(__name__)

# Field length limits from docs/pinterest_api_reference.md Section 9.
_MAX_TITLE_LEN: int = 100
_MAX_DESCRIPTION_LEN: int = 500
_MAX_ALT_TEXT_LEN: int = 500

# Number of leading characters shown when masking a token for log output.
_MASK_PREFIX_LEN: int = 12

# Accepted values for the media_type column.
_VALID_MEDIA_TYPES: frozenset[str] = frozenset({"image", "video"})


class ValidationError(Exception):
    """Raised when a PinRow fails one or more pre-flight validation checks.

    The exception message contains every violation found on the row,
    separated by ``"; "``, so the operator can fix all issues in one pass
    rather than re-running the script after each correction.

    Example message::

        Row 4 validation failed: title exceeds 100 characters (current: 143); \
board_name 'Facades' not found in board map
    """


def validate_row(row: PinRow, board_map: dict[str, str]) -> None:
    """Validate all required fields on *row* before calling the Pinterest API.

    Runs every check unconditionally and collects all violations before
    raising so the caller learns about every problem in one shot.  Raises
    nothing if all checks pass.

    Checks (in order):
      1. ``image_filename`` is not empty.
      2. ``media_type`` is exactly ``"image"`` or ``"video"``.
      3. ``title`` is at most 100 characters.
      4. ``description`` is at most 500 characters.
      5. ``alt_text`` is at most 500 characters.
      6. ``destination_link`` starts with ``"https://"``.
      7. ``board_name`` exists as a key in *board_map*.

    Args:
        row: A ``PinRow`` instance loaded from the Queue tab. The
            ``row_number`` attribute is used only in the exception message.
        board_map: The name→ID mapping built at startup from
            ``GET /v5/boards``.  The validator checks for key membership
            only — it never resolves the board ID itself.

    Raises:
        ValidationError: If one or more checks fail.  The message lists
            every violation separated by ``"; "`` so the full description
            can be written to column K in a single call.

    Example::

        try:
            validate_row(row, board_map)
        except ValidationError as exc:
            mark_failed(sheets_client, sheet_id, row.row_number, str(exc))
    """
    violations: list[str] = []

    if not row.image_filename.strip():
        violations.append("image_filename is empty")

    if row.media_type not in _VALID_MEDIA_TYPES:
        violations.append(
            f"media_type '{row.media_type}' is invalid — must be 'image' or 'video'"
        )

    title_len = len(row.title)
    if title_len > _MAX_TITLE_LEN:
        violations.append(
            f"title exceeds {_MAX_TITLE_LEN} characters (current: {title_len})"
        )

    desc_len = len(row.description)
    if desc_len > _MAX_DESCRIPTION_LEN:
        violations.append(
            f"description exceeds {_MAX_DESCRIPTION_LEN} characters (current: {desc_len})"
        )

    alt_len = len(row.alt_text)
    if alt_len > _MAX_ALT_TEXT_LEN:
        violations.append(
            f"alt_text exceeds {_MAX_ALT_TEXT_LEN} characters (current: {alt_len})"
        )

    if not row.destination_link.startswith("https://"):
        violations.append(
            f"destination_link '{row.destination_link}' does not start with 'https://'"
        )

    if row.board_name not in board_map:
        violations.append(
            f"board_name '{row.board_name}' not found in board map"
        )

    if violations:
        detail = "; ".join(violations)
        raise ValidationError(
            f"Row {row.row_number} validation failed: {detail}"
        )


def mask_token(token: str) -> str:
    """Return a safe-to-log representation of a Pinterest token string.

    Shows only the first ``_MASK_PREFIX_LEN`` (12) characters followed by
    ``"..."`` so the token can be identified in log output (e.g. by its
    ``pina_`` prefix) without being exposed.  This is the single source
    of truth for token masking — all modules that need to log a token
    should import and call this function rather than implementing their
    own truncation logic.

    Args:
        token: The full token string, e.g. an access token (``pina_...``)
            or refresh token (``pinr_...``).

    Returns:
        - ``token[:12] + "..."`` when ``len(token) >= 12``.
        - ``"***"`` when ``len(token) < 12`` — the token is too short to
          reveal even a safe prefix without risking exposure.

    Examples::

        mask_token("pina_Ab12Cd34Ef56Gh")  →  "pina_Ab12Cd3..."
        mask_token("short")                →  "***"
    """
    if len(token) < _MASK_PREFIX_LEN:
        return "***"
    return token[:_MASK_PREFIX_LEN] + "..."
