"""
Handles the 4-step video Pin creation flow for a single queue row.

Step 1 — Register intent:
    POST /v5/media with media_type='video'. Saves media_id and
    upload_url/upload_parameters from the response.

Step 2 — Upload to S3:
    Stream the video file from Google Drive and POST it to the AWS
    upload_url using multipart/form-data. No Pinterest Authorization
    header is sent for this request.

Step 3 — Poll processing status:
    GET /v5/media/{media_id} every 5 seconds, up to 60 attempts (5 min).
    Proceeds only when status == 'succeeded'. Marks row Failed on timeout
    or status == 'failed'.

Step 4 — Create the video Pin:
    POST /v5/pins with source_type='video_id' and the media_id.
    cover_image_url is required — uses the Drive direct-download URL of a
    companion thumbnail file if present, otherwise logs a warning.

Returns the pin_id string on success.
"""
