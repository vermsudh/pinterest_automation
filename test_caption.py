"""
Standalone test for A.won Gemini caption generation.

Sends a local image file to Gemini and prints the generated title,
description, and alt_text alongside character-limit checks. No Google
Sheets or Pinterest credentials are required.

Usage:
    python3 test_caption.py path/to/image.jpg
"""

import os
import sys
from pathlib import Path

# load_dotenv() must run before any project import so that _require_env()
# calls inside config/settings.py succeed when that module is first loaded.
from dotenv import load_dotenv
load_dotenv()

from services.caption_generator import generate_caption  # noqa: E402

_DIVIDER: str = "-" * 40
_LIMIT_TITLE: int = 100
_LIMIT_DESCRIPTION: int = 500
_LIMIT_ALT_TEXT: int = 500


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

    Reads the image path from ``sys.argv[1]``, loads its bytes from disk,
    calls :func:`~services.caption_generator.generate_caption`, and prints
    the three caption fields in a labelled block followed by character-limit
    checks for each field.

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

    print(f"Image : {image_path} ({image_path.stat().st_size:,} bytes)")
    image_bytes: bytes = image_path.read_bytes()

    print(f"Sending to Gemini...")
    caption: dict[str, str] = generate_caption(image_bytes, image_path.name)

    title: str = caption["title"]
    description: str = caption["description"]
    alt_text: str = caption["alt_text"]

    print()
    print(_DIVIDER)
    print(f"TITLE ({len(title)} chars):")
    print(title)
    print()
    print(f"DESCRIPTION ({len(description)} chars):")
    print(description)
    print()
    print(f"ALT TEXT ({len(alt_text)} chars):")
    print(alt_text)
    print(_DIVIDER)
    print()
    print("Character limit checks:")
    print(_limit_check("title      ", title, _LIMIT_TITLE))
    print(_limit_check("description", description, _LIMIT_DESCRIPTION))
    print(_limit_check("alt_text   ", alt_text, _LIMIT_ALT_TEXT))


if __name__ == "__main__":
    main()
