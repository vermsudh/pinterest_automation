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
