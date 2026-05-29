"""
Validates Pin fields locally before any Pinterest API call is made.

Checks performed on each queue row:
  - title           : <= 100 characters
  - description     : <= 500 characters
  - alt_text        : <= 500 characters
  - destination_link: non-empty and starts with 'https://'
  - board_name      : present in the board name→ID map built at startup
  - media_type      : exactly 'image' or 'video'
  - image_filename  : non-empty string

Returns a list of human-readable error strings. An empty list means the
row is valid. The caller writes the joined errors to column K (error_message)
and marks the row Failed without touching the Pinterest API.
"""
