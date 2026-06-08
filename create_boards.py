"""
One-time script to create all A.won Pinterest boards for production.

Run this once after OAuth setup to create all boards defined in BOARDS list.
Skips boards that already exist on the account.

Usage:
    python create_boards.py
"""

from __future__ import annotations

import sys
from dotenv import load_dotenv

load_dotenv()

from auth.google_auth import build_sheets_client
from auth.pinterest_auth import ensure_valid_token
from config.settings import load_awon_account
from services.pinterest_client import PinterestClient
from utils.logger import setup_logger
from auth.google_auth import build_google_credentials, build_sheets_client

_log = setup_logger(__name__)

# All production boards for A.won's Pinterest account.
# Edit this list to add or remove boards.
BOARDS_TO_CREATE = [
    # Set 1 — South Delhi & India HNI
    'Luxury Villa Design — India',
    'Modern Farmhouse Design — New Delhi',
    'Luxury Home Interiors — India',
    'Stone & Travertine Interiors',
    'Luxury Villa Elevation — New Delhi',

    # Set 2 — Dubai & GCC HNI
    'Luxury Villa Design — Dubai & UAE',
    'Modern Villa Architecture — Middle East',
    'Luxury Pool Villa Design',
    'Minimalist Luxury Interiors',
    'Contemporary Villa Facade Design',
]


def main() -> None:
    # Load account config
    account = load_awon_account()

    # Authenticate
    _log.info("Authenticating with Pinterest...")
    credentials = build_google_credentials()
    sheets_client = build_sheets_client(credentials)
    access_token = ensure_valid_token(
        sheets_client,
        account.google_sheet_id,
        account.pinterest_client_id,
        account.pinterest_client_secret,
    )
    client = PinterestClient(access_token)

    # Fetch existing boards to avoid duplicates
    _log.info("Fetching existing boards...")
    existing_boards = client.get_boards()
    existing_names = {name.lower() for name in existing_boards.keys()}
    _log.info("Found %d existing board(s).", len(existing_names))

    # Create missing boards
    created = 0
    skipped = 0

    for board_name in BOARDS_TO_CREATE:
        if board_name.lower() in existing_names:
            _log.info("Board '%s' already exists — skipping.", board_name)
            skipped += 1
            continue

        try:
            board = client.create_board(board_name, privacy="PUBLIC")
            _log.info(
                "Created board '%s' with ID %s.",
                board_name,
                board.get("id"),
            )
            created += 1
        except Exception as exc:
            _log.error("Failed to create board '%s': %s", board_name, exc)

    # Summary
    print("\n============================")
    print("A.won Board Creation — Summary")
    print("============================")
    print(f"{'Boards created':<20} : {created}")
    print(f"{'Boards skipped':<20} : {skipped}")
    print(f"{'Total in list':<20} : {len(BOARDS_TO_CREATE)}")
    print("============================\n")

    sys.exit(0)


if __name__ == "__main__":
    main()