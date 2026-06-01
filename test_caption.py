"""
Standalone test for A.won Gemini caption generation.

Sends a local image file to Gemini and prints the generated title,
description, and alt_text alongside character-limit checks, image resize
stats, and Gemini token usage with estimated cost in USD and INR.

Usage:
    python3 test_caption.py path/to/image.jpg
"""

import logging
import os
import sys
from pathlib import Path

# load_dotenv() must run before any project import so that _require_env()
# calls inside config/settings.py succeed when that module is first loaded.
from dotenv import load_dotenv
load_dotenv()

from config.settings import GEMINI_MODEL  # noqa: E402
from services.caption_generator import _resize_for_gemini, generate_caption  # noqa: E402

_DIVIDER: str = "-" * 40
_LIMIT_TITLE: int = 100
_LIMIT_DESCRIPTION: int = 500
_LIMIT_ALT_TEXT: int = 500


class _UsageCapture(logging.Handler):
    """Captures the Gemini usage LogRecord emitted by caption_generator."""

    def __init__(self) -> None:
        super().__init__()
        self.usage_record: logging.LogRecord | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record if it contains the Gemini usage summary."""
        if "Gemini usage" in record.getMessage():
            self.usage_record = record


def _limit_check(field: str, value: str, limit: int) -> str:
    """Format a character-limit check result for one caption field.

    Args:
        field: Human-readable field name shown in the output line.
        value: The generated caption value to measure.
        limit: Maximum allowed character count for this field.

    Returns:
        A one-line string with the count, limit, and a pass/fail note.
    """
    count = len(value)
    if count <= limit:
        verdict = "OK"
    else:
        verdict = f"OVER LIMIT by {count - limit} char(s)"
    return f"  {field}: {count}/{limit} — {verdict}"


def main() -> None:
    """Generate and print captions for a local image file using Gemini.

    Reads the image path from ``sys.argv[1]``, displays original and resized
    file sizes, calls :func:`~services.caption_generator.generate_caption`,
    and prints the three caption fields with character-limit checks and a
    Gemini token usage summary including estimated cost in USD and INR.

    Exits with code 1 and a clear message if the image path is missing,
    the file does not exist, or ``GEMINI_API_KEY`` is not set.
    """
    if len(sys.argv) < 2:
        print("Error: no image path provided.")
        print(f"Usage: python3 {Path(sys.argv[0]).name} path/to/image.jpg")
        sys.exit(1)

    image_path = Path(sys.argv[1])
    if not image_path.exists():
        print(f"Error: file not found — {image_path}")
        sys.exit(1)

    if not os.environ.get("GEMINI_API_KEY", "").strip():
        print("Error: GEMINI_API_KEY is not set in .env or the environment.")
        sys.exit(1)

    image_bytes: bytes = image_path.read_bytes()
    orig_size: int = len(image_bytes)
    orig_mb: float = orig_size / 1_048_576

    # Pre-compute resized size for display; generate_caption resizes again internally.
    _, resized_size = _resize_for_gemini(image_bytes)
    resized_kb: float = resized_size / 1024

    # Install a log capture handler to intercept the Gemini usage record.
    usage_handler = _UsageCapture()
    usage_handler.setLevel(logging.INFO)
    cap_logger = logging.getLogger("services.caption_generator")
    prev_level = cap_logger.level
    cap_logger.setLevel(logging.INFO)
    cap_logger.addHandler(usage_handler)

    print(_DIVIDER)
    print(f"Image         : {image_path.name} ({orig_size:,} bytes — {orig_mb:.1f} MB)")
    print(f"Resized to    : {resized_size:,} bytes — {resized_kb:.0f} KB (sent to Gemini)")
    print(f"Model         : {GEMINI_MODEL}")
    print(_DIVIDER)

    caption: dict[str, str] = generate_caption(image_bytes, image_path.name)

    cap_logger.removeHandler(usage_handler)
    cap_logger.setLevel(prev_level)

    title: str = caption["title"]
    description: str = caption["description"]
    alt_text: str = caption["alt_text"]

    print(f"TITLE ({len(title)} chars):")
    print(title)
    print()
    print(f"DESCRIPTION ({len(description)} chars):")
    print(description)
    print()
    print(f"ALT TEXT ({len(alt_text)} chars):")
    print(alt_text)
    print(_DIVIDER)
    print("Character limit checks:")
    print(_limit_check("title      ", title, _LIMIT_TITLE))
    print(_limit_check("description", description, _LIMIT_DESCRIPTION))
    print(_limit_check("alt_text   ", alt_text, _LIMIT_ALT_TEXT))
    print(_DIVIDER)
    print("Token usage:")

    rec = usage_handler.usage_record
    if rec is not None and isinstance(rec.args, tuple) and len(rec.args) >= 6:
        input_tokens: int = rec.args[1]
        output_tokens: int = rec.args[2]
        total_tokens: int = rec.args[3]
        cost_usd: float = rec.args[4]
        cost_inr: float = rec.args[5]
        print(f"  Input tokens : {input_tokens:,}")
        print(f"  Output tokens: {output_tokens:,}")
        print(f"  Total tokens : {total_tokens:,}")
        print(f"  Est. cost    : ${cost_usd:.6f} (₹{cost_inr:.3f})")
    else:
        print("  (usage data unavailable)")
    print(_DIVIDER)


if __name__ == "__main__":
    main()
