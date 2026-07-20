"""
Applies the A.won wordmark watermark to image Pins before upload.

The pipeline never writes images to disk — Drive download, watermarking, and
the Pinterest base64 upload all operate on in-memory bytes end-to-end.
``apply_watermark()`` is the single public entry point:

1. If ``WATERMARK_ENABLED`` is False, the source bytes are returned unchanged
   with no Pillow work performed.
2. The static watermark PNG at ``WATERMARK_PATH`` is loaded once and cached
   at module scope, since it is a fixed asset reused on every call.
3. The watermark is resized to ``WATERMARK_WIDTH_PCT`` of the source image's
   width (aspect ratio preserved), its alpha channel is scaled by
   ``WATERMARK_OPACITY``, and it is composited onto the bottom-right corner
   of the source image, inset by ``WATERMARK_PADDING_PCT`` of the source
   width on both edges.
4. The result is re-encoded to the original ``content_type`` and returned as
   bytes via a BytesIO buffer.

Any failure during this process is logged and the original, unwatermarked
image bytes are returned instead of raising — a watermarking bug must never
cause a Pin upload to fail.
"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image

from config.settings import (
    WATERMARK_ENABLED,
    WATERMARK_OPACITY,
    WATERMARK_PADDING_PCT,
    WATERMARK_PATH,
    WATERMARK_WIDTH_PCT,
)

_log = logging.getLogger(__name__)

# Maps a Pinterest API content_type back to the Pillow format name required
# by Image.save(). Keep in sync with uploaders/image_uploader.py's
# _CONTENT_TYPE_MAP (the reverse mapping).
_CONTENT_TYPE_TO_PIL_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}

# Module-level cache for the watermark asset — loaded lazily on first use and
# reused for every subsequent call rather than re-reading it from disk.
_watermark_cache: Image.Image | None = None


def _load_watermark() -> Image.Image:
    """Load and cache the static watermark PNG from ``WATERMARK_PATH``.

    Returns:
        The watermark image in RGBA mode.
    """
    global _watermark_cache
    if _watermark_cache is None:
        _watermark_cache = Image.open(WATERMARK_PATH).convert("RGBA")
    return _watermark_cache


def apply_watermark(image_bytes: bytes, content_type: str) -> bytes:
    """Composite the A.won wordmark onto the bottom-right corner of an image.

    Args:
        image_bytes: Raw source image bytes, as returned by
            ``download_file_to_memory()``.
        content_type: Pinterest API content_type of the source image (e.g.
            ``"image/jpeg"``), used to preserve the original format on
            re-encode.

    Returns:
        The watermarked image as bytes in the same format as the input. If
        ``WATERMARK_ENABLED`` is False, or if any error occurs while loading
        or compositing the watermark, the original ``image_bytes`` are
        returned unchanged.
    """
    if not WATERMARK_ENABLED:
        return image_bytes

    try:
        source = Image.open(BytesIO(image_bytes))
        pil_format = _CONTENT_TYPE_TO_PIL_FORMAT.get(content_type, source.format or "JPEG")
        source = source.convert("RGBA")

        watermark = _load_watermark()

        # Resize the watermark so its width matches WATERMARK_WIDTH_PCT of the
        # source width, preserving the watermark's own aspect ratio.
        target_width = max(1, round(source.width * WATERMARK_WIDTH_PCT))
        scale = target_width / watermark.width
        target_height = max(1, round(watermark.height * scale))
        resized_watermark = watermark.resize((target_width, target_height), Image.LANCZOS)

        # Scale the existing per-pixel alpha by the opacity factor rather than
        # flattening it, so the PNG's soft anti-aliased edges are preserved.
        r, g, b, a = resized_watermark.split()
        a = a.point(lambda px: round(px * WATERMARK_OPACITY))
        resized_watermark = Image.merge("RGBA", (r, g, b, a))

        # Position bottom-right, inset from both edges by WATERMARK_PADDING_PCT
        # of the source image's width.
        padding = round(source.width * WATERMARK_PADDING_PCT)
        x = source.width - target_width - padding
        y = source.height - target_height - padding

        composited = source.copy()
        composited.alpha_composite(resized_watermark, dest=(x, y))

        # Re-encode to the original format. JPEG has no alpha channel, so it
        # must be flattened onto an opaque background before saving.
        buffer = BytesIO()
        if pil_format == "JPEG":
            flattened = Image.new("RGB", composited.size, (255, 255, 255))
            flattened.paste(composited, mask=composited.split()[3])
            flattened.save(buffer, format="JPEG", quality=95)
        else:
            composited.save(buffer, format=pil_format, quality=95)

        return buffer.getvalue()

    except Exception as exc:  # noqa: BLE001
        _log.warning("Watermarking failed, posting original image. Error: %s", exc)
        return image_bytes
