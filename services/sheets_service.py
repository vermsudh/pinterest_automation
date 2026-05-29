"""
Reads and writes the A.won Pinterest upload queue in Google Sheets.

Reads all rows from the Queue tab where column H (status) equals 'Pending'
and returns them as structured records with all eleven fields (image_filename,
media_type, title, description, board_name, destination_link, alt_text,
status, pin_id, posted_at, error_message).

After each upload attempt, writes the outcome back to the correct row:
  - Success: sets status='Posted', pin_id, and posted_at (ISO 8601 UTC)
  - Failure: sets status='Failed' and error_message

Also reads and writes the _config tab (cells B1–B3) for Pinterest token
persistence on behalf of pinterest_auth.py.
"""
