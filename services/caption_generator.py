"""
Generates Pinterest captions for A.won architectural content using Google Gemini.

Sends image bytes to the Gemini multimodal API and parses the response into
three caption fields — title, description, and alt_text. Integrates with the
Sheet queue via fill_missing_captions, which fills only the empty caption
columns for each pending row and writes the generated values back to the Sheet
in a single batchUpdate call per row.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

from google import genai
from PIL import Image

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
    "You are the copywriter for A.won, a concept architecture studio in New Delhi "
    "founded by Abhishek RK Khanna. A.won designs residences, farmhouses, villas, "
    "retreats, hotels, and resorts for HNI clients across India and the GCC. The "
    "work is restrained, material-honest, and attentive to natural light. Recurring "
    "materials: travertine, stone, timber, alabaster, lime plaster, bouclé, brass — "
    "composed around courtyards, thresholds, and well-proportioned rooms.\n\n"
    "OBJECTIVE: These captions run on A.won's Pinterest. The business goal is "
    "DISCOVERY and LEADS — high-net-worth homeowners in Delhi, India, and the GCC "
    "planning a home should search a term, find this pin, save it, and click "
    "through to the studio. Pinterest is a visual SEARCH engine: ranking is driven "
    "by keyword relevance in the title, description, and alt text — NOT by hashtags. "
    "Write the way an HNI actually searches, not the way an architect talks.\n\n"
    "SEARCH-LED WRITING: Open every title and the first sentence of the description "
    "with the highest-intent search term for what is visible — the TYPOLOGY and "
    "STYLE first (e.g. 'Modern farmhouse design', 'Luxury villa elevation', "
    "'Double-height living room', 'Courtyard house design', 'Modern house front "
    "elevation', 'Master bedroom design'), THEN the distinguishing material or "
    "detail second. Material-first phrasing like 'travertine and timber accents' is "
    "connoisseur language and ranks poorly — lead with the searchable typology "
    "instead and let material follow.\n\n"
    "VOICE: Restrained, premium, specific, confident — never promotional. British/"
    "Indian English (visualisation, colour, storey, metre). NEVER use 'special "
    "offer', 'package', 'deal', 'limited time', 'affordable', 'world-class', "
    "'turnkey', 'stunning', 'breathtaking', or any hype/discount language. Do NOT "
    "invent project names, locations, areas, prices, clients, awards, or studio "
    "statistics — describe only what is actually visible.\n\n"
    "Return ONLY a JSON object with exactly three keys:\n"
    '- "title": Search-led and keyword-first. Lead with typology + style, material '
    "second. May use a clean pipe structure (e.g. 'Modern Farmhouse Design | "
    "Travertine Facade, Luxury Villa Concept'). Maximum 100 characters. No "
    "hashtags. No emoji.\n"
    '- "description": Two to three restrained sentences. The FIRST sentence must be '
    "keyword-dense and search-led, repeating the core typology/style terms an HNI "
    "would type and naturally weaving in relevant terms (villa, residence, "
    "farmhouse, interior, courtyard, facade, elevation, Delhi, India where "
    "genuinely true). Include ONE quiet signal that this is commissioned concept "
    "work a client could engage — e.g. 'A concept designed for a private residence' "
    "or close with 'Concept by A.won, New Delhi.' — without ever sounding "
    "promotional. End with a MAXIMUM of 3–4 precise hashtags chosen from what is "
    "visible: #conceptarchitecture #luxuryvilla #modernindianhome #villadesign "
    "#farmhousedesign #interiordesign #facadedesign #delhiarchitecture. The ENTIRE "
    "description including hashtags must be 500 characters or fewer.\n"
    '- "alt_text": A factual, accessible description of exactly what is visible, '
    "written so it is useful to a screen-reader AND naturally contains the same "
    "core search keywords (typology, style, key materials, setting). No hype, no "
    "marketing phrasing, no studio name. Maximum 500 characters.\n\n"
    "Respond with valid JSON only — no markdown fences, no explanation, no text "
    "outside the JSON object."
)

MAX_GEMINI_IMAGE_WIDTH: int = 800
# Pricing for gemini-2.5-flash-lite — update if GEMINI_MODEL is changed.
GEMINI_INPUT_COST_PER_TOKEN: float = 0.10 / 1_000_000
# Pricing for gemini-2.5-flash-lite — update if GEMINI_MODEL is changed.
GEMINI_OUTPUT_COST_PER_TOKEN: float = 0.40 / 1_000_000
USD_TO_INR: float = 85.0

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


def _resize_for_gemini(
    image_bytes: bytes,
    max_width: int = MAX_GEMINI_IMAGE_WIDTH,
) -> tuple[bytes, int]:
    """Resize and JPEG-encode an image before sending to Gemini.

    Opens *image_bytes* with Pillow and downscales proportionally when its
    pixel width exceeds *max_width*, using LANCZOS resampling.  The result is
    always written to a JPEG buffer (quality 85) regardless of the original
    format, so the caller must always use ``"image/jpeg"`` as the MIME type.

    Args:
        image_bytes: Raw bytes of the original image file.
        max_width: Maximum pixel width after resizing.  Images narrower than
            this value are not scaled but are still re-encoded as JPEG.

    Returns:
        A tuple of ``(jpeg_bytes, size)`` where *jpeg_bytes* is the JPEG-
        encoded buffer and *size* is its length in bytes.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.LANCZOS)

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    jpeg_bytes = buf.getvalue()
    new_size = len(jpeg_bytes)

    logger.info(
        "Resized image from %.1f MB to %.0f KB before sending to Gemini.",
        len(image_bytes) / 1_048_576,
        new_size / 1024,
    )
    return jpeg_bytes, new_size


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

    resized_bytes, _ = _resize_for_gemini(image_bytes)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            genai.types.Part.from_bytes(data=resized_bytes, mime_type="image/jpeg"),
            _AWON_PROMPT,
        ],
    )

    usage = response.usage_metadata
    input_tokens: int = (usage.prompt_token_count or 0) if usage else 0
    output_tokens: int = (usage.candidates_token_count or 0) if usage else 0
    total_tokens: int = input_tokens + output_tokens
    cost_usd: float = (
        input_tokens * GEMINI_INPUT_COST_PER_TOKEN
        + output_tokens * GEMINI_OUTPUT_COST_PER_TOKEN
    )
    cost_inr: float = cost_usd * USD_TO_INR
    logger.info(
        "Gemini usage — model: %s | input: %d tokens | output: %d tokens"
        " | total: %d tokens | estimated cost: $%.6f (₹%.3f)",
        GEMINI_MODEL,
        input_tokens,
        output_tokens,
        total_tokens,
        cost_usd,
        cost_inr,
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
