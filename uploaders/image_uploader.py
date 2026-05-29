"""
Handles the full image Pin creation flow for a single queue row.

1. Calls drive_service to download the image file into memory (bytes).
2. Base64-encodes the bytes (no data URI prefix — raw base64 string only).
3. Validates all Pin fields via validators.py before touching the API.
4. Calls pinterest_client to POST /v5/pins with source_type=image_base64.
5. Returns the pin_id string on success, or raises on failure.

Does not write any temporary files to disk.
Supported formats: JPEG, PNG, WEBP (up to 32 MB).
"""
