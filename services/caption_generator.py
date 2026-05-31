"""
Generates Pinterest captions for A.won architectural content using Google Gemini.

Sends image bytes to the Gemini multimodal API and parses the response into
three caption fields — title, description, and alt_text. Integrates with the
Sheet queue via fill_missing_captions, which fills only the empty caption
columns for each pending row and writes the generated values back to the Sheet
in a single batchUpdate call per row.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google import genai

from config.settings import GEMINI_API_KEY, GEMINI_MODEL, SHEET_QUEUE_TAB
from services.drive_service import download_file_to_memory
from services.sheets_service import PinRow

logger = logging.getLogger(__name__)

# Column letters for the three caption fields in the Queue tab.
_COL_TITLE: str = "C"
_COL_DESCRIPTION: str = "D"
_COL_ALT_TEXT: str = "G"

# Prompt sent to Gemini alongside each architectural image.
_AWON_PROMPT: str = (
    "You are the copywriter for A.won, a concept architecture studio based in "
    "New Delhi, founded by Abhishek RK Khanna. A.won designs residences, "
    "retreats, farmhouses, villas, hotels, and resorts for discerning clients "
    "across Delhi and India. The work is distinguished by restraint, material "
    "honesty, and a precise attention to how natural light moves through a "
    "space across the day. The studio works in concept only: vision, plans, "
    "sections, three-dimensional form, and cinematic visualisation. Its "
    "sensibility sits alongside studios like SAOTA and Studio MK27 — premium, "
    "considered, emotionally specific. Recurring materials include travertine, "
    "stone, timber, alabaster, lime plaster, bouclé, and brass, composed around "
    "courtyards, thresholds, and quiet, well-proportioned rooms.\n\n"
    "AUDIENCE & PLATFORM: These captions are for A.won's Pinterest, where "
    "high-net-worth homeowners in Delhi and South Delhi — and across India — "
    "plan their residences, villas, and farmhouses. Pinterest is a visual "
    "SEARCH engine: discovery comes mainly from clear, keyword-rich, natural "
    "language in the title and description, not from hashtags. Write so an "
    "Indian HNI planning a home would search, find, and save the pin.\n\n"
    "VOICE: Restrained, premium, specific, confident. Never promotional. Use "
    "precise material and spatial language. Use British/Indian English spelling "
    "(visualisation, colour, storey, metre). NEVER use the words 'special "
    "offer', 'package', 'deal', 'limited time', 'affordable', 'world-class', "
    "'turnkey', 'stunning', 'breathtaking', or any discount or hype language. "
    "Do NOT invent project names, locations, areas, prices, client details, "
    "awards, years of experience, or studio statistics — describe only what is "
    "actually visible in the image.\n\n"
    "Analyse the provided architectural image and return ONLY a JSON object "
    "with exactly three keys:\n"
    '- "title": Compelling, descriptive, keyword-led for Pinterest search. '
    "Open with the strongest searchable terms an Indian HNI would type — the "
    "space, the dominant material, the style (e.g. 'Travertine living room in a "
    "modern Indian villa', 'Sculptural stone staircase', 'Courtyard farmhouse "
    "facade'). Maximum 100 characters. No hashtags. No emoji.\n"
    '- "description": One to three restrained, premium sentences describing the '
    "space — its materials, light, and spatial idea — written naturally but "
    "rich in the keywords an Indian HNI would search (villa, residence, "
    "farmhouse, interior, courtyard, facade, travertine, stone, Delhi, India "
    "where genuinely relevant). It may close with a quiet studio signature such "
    "as 'Concept by A.won, New Delhi.' Then end with 5–8 relevant, premium "
    "hashtags chosen from what is visible, adapted from this family: "
    "#conceptarchitecture #indianarchitecture #luxuryvilla #modernindianhome "
    "#villadesign #interiordesign #travertine #stoneinteriors #delhiarchitecture "
    "#architecturedesign. NEVER use #archilovers, #archi, #instagood, "
    "#follow4follow, or generic engagement tags. The ENTIRE description "
    "including hashtags must be 500 characters or fewer — count carefully and "
    "trim the prose first so no hashtag is ever cut off.\n"
    '- "alt_text": Plain, factual description of exactly what is visible in the '
    "image, for accessibility. Maximum 500 characters. No hashtags. No brand "
    "language, no marketing phrasing, no studio name.\n\n"
    "Respond with valid JSON only — no markdown fences, no explanation, and no "
    "text outside the JSON object."
)

# Maps lowercase filename extensions to MIME types for the Gemini API request.
_MIME_TYPE_MAP: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_mime_type(filename: str) -> str:
    """Return the MIME type for an image file based on its extension.

    Args:
        filename: The file's display name, e.g. ``"khan-living-room.jpg"``.

    Returns:
        A MIME type string such as ``"image/jpeg"``. Defaults to
        ``"image/jpeg"`` for unrecognised or missing extensions.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MIME_TYPE_MAP.get(ext, "image/jpeg")


def _parse_gemini_response(text: str) -> dict[str, str]:
    """Extract caption fields from a Gemini JSON response string.

    Strips markdown code fences that the model may add despite instructions,
    parses the JSON, and truncates each field to its maximum allowed length.

    Args:
        text: The raw ``.text`` content returned by the Gemini API.

    Returns:
        A dict with ``"title"`` (≤100 chars), ``"description"`` (≤500 chars),
        and ``"alt_text"`` (≤500 chars) string values.

    Raises:
        ValueError: If the text cannot be parsed as JSON, or the parsed value
            is not a JSON object.
    """
    cleaned = text.strip()
    # Strip markdown code fences that the model may add despite instructions.
    cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Gemini response parsed but is not a JSON object.")

    return {
        "title": str(data.get("title", "")).strip()[:100],
        "description": str(data.get("description", "")).strip()[:500],
        "alt_text": str(data.get("alt_text", "")).strip()[:500],
    }


def _queue_cell(col: str, row_number: int) -> str:
    """Build a Sheets API range string for a single cell in the Queue tab.

    Args:
        col: Column letter, e.g. ``"C"``.
        row_number: 1-based Sheet row number.

    Returns:
        A range string such as ``"Queue!C5"``.
    """
    return f"{SHEET_QUEUE_TAB}!{col}{row_number}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_caption(image_bytes: bytes, filename: str) -> dict[str, str]:
    """Generate Pinterest caption fields for one architectural image via Gemini.

    Configures the Gemini client from settings at call time so the module
    can be imported safely even when GEMINI_API_KEY is absent — the key is
    only required when this function is actually invoked.

    Args:
        image_bytes: Raw bytes of the image file downloaded from Google Drive.
        filename: The original filename (e.g. ``"render-kitchen.jpg"``), used
            to detect the correct MIME type for the Gemini request.

    Returns:
        A dict with keys ``"title"``, ``"description"``, and ``"alt_text"``,
        each a non-empty string within its character-limit constraint.

    Raises:
        ValueError: If the Gemini response cannot be parsed into the expected
            JSON structure.
        Exception: Any exception raised by the Gemini SDK is propagated to
            the caller so fill_missing_captions can log and skip the row.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    mime_type = _detect_mime_type(filename)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            _AWON_PROMPT,
        ],
    )

    caption = _parse_gemini_response(response.text)
    logger.info(
        "Gemini caption fields — title: '%s', description: %d chars, alt_text: %d chars.",
        caption["title"],
        len(caption["description"]),
        len(caption["alt_text"]),
    )
    return caption


def fill_missing_captions(
    sheets_client: Any,
    sheet_id: str,
    drive_client: Any,
    ready_folder_id: str,
    pending_rows: list[PinRow],
    drive_files: dict[str, str],
) -> None:
    """Fill empty caption fields for pending rows using Gemini.

    Iterates over *pending_rows* and, for each row where title, description,
    or alt_text is empty:

    1. Resolves the Drive file ID from *drive_files* using the row's filename.
    2. Downloads the image into memory via :func:`download_file_to_memory`.
    3. Calls :func:`generate_caption` to produce the missing fields only.
    4. Writes the newly-generated cells back to the Sheet in a single
       batchUpdate call — already-filled fields are never touched.
    5. Updates the PinRow object in memory so the rest of the script sees
       the filled values without re-fetching from the Sheet.

    Rows where all three caption fields are already filled are skipped without
    making any Drive or Gemini API call. If any step fails for a row, a
    warning is logged and the loop moves on to the next row without aborting.

    Args:
        sheets_client: Authenticated Google Sheets API client.
        sheet_id: Google Sheets document ID (``GOOGLE_SHEET_ID`` env var).
        drive_client: Authenticated Google Drive API client.
        ready_folder_id: Drive ID of the ``Ready/`` sub-folder. Reserved for
            future use; file IDs are resolved from *drive_files* directly.
        pending_rows: List of :class:`~services.sheets_service.PinRow` objects
            returned by :func:`~services.sheets_service.fetch_pending_rows`.
        drive_files: Mapping of filename → Drive file ID for the ``Ready/``
            folder, as returned by
            :func:`~services.drive_service.list_ready_files`.
    """
    for row in pending_rows:
        needs_title = not row.title
        needs_description = not row.description
        needs_alt_text = not row.alt_text

        if not (needs_title or needs_description or needs_alt_text):
            logger.info(
                "Row %d | %s | All caption fields already filled — skipping.",
                row.row_number,
                row.image_filename,
            )
            continue

        file_id = drive_files.get(row.image_filename)
        if not file_id:
            logger.warning(
                "Row %d | %s | File not found in Ready/ drive listing — "
                "cannot generate captions.",
                row.row_number,
                row.image_filename,
            )
            continue

        try:
            image_bytes = download_file_to_memory(drive_client, file_id)
            caption = generate_caption(image_bytes, row.image_filename)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Row %d | %s | Caption generation failed: %s",
                row.row_number,
                row.image_filename,
                exc,
            )
            continue

        # Build the batchUpdate payload — only include cells that were empty.
        update_data: list[dict[str, Any]] = []
        if needs_title:
            update_data.append(
                {
                    "range": _queue_cell(_COL_TITLE, row.row_number),
                    "values": [[caption["title"]]],
                }
            )
        if needs_description:
            update_data.append(
                {
                    "range": _queue_cell(_COL_DESCRIPTION, row.row_number),
                    "values": [[caption["description"]]],
                }
            )
        if needs_alt_text:
            update_data.append(
                {
                    "range": _queue_cell(_COL_ALT_TEXT, row.row_number),
                    "values": [[caption["alt_text"]]],
                }
            )

        try:
            (
                sheets_client.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": update_data},
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Row %d | %s | Failed to write captions back to Sheet: %s",
                row.row_number,
                row.image_filename,
                exc,
            )
            continue

        # Reflect generated values into the in-memory PinRow so the upload
        # loop sees them without a second Sheet read.
        if needs_title:
            row.title = caption["title"]
        if needs_description:
            row.description = caption["description"]
        if needs_alt_text:
            row.alt_text = caption["alt_text"]

        logger.info(
            "Row %d | %s | Caption generated and written to Sheet.",
            row.row_number,
            row.image_filename,
        )
