"""
Downloads media files from Google Drive and manages folder moves.

Searches the Ready/ folder (GOOGLE_DRIVE_FOLDER_ID points to the root
Pinterest Queue folder, not directly to Ready/) for files by exact
filename. Downloads image files entirely into memory (as bytes) so they
can be base64-encoded without writing to disk. Streams video files from
Drive to avoid loading large files (up to 2 GB) into memory at once.

After each upload attempt, moves the file to the correct destination:
  - Success: moves to Posted/ folder
  - Failure: moves to Failed/ folder

All moves use the Drive API files.update method to change the parent
folder ID — files are never copied and deleted.
"""

import io
import logging
from typing import Any

from googleapiclient.http import MediaIoBaseDownload

_log = logging.getLogger(__name__)

# Drive MIME type that identifies folders — used to resolve sub-folder IDs
# and to exclude folder entries when listing files in Ready/.
_FOLDER_MIME_TYPE: str = "application/vnd.google-apps.folder"


def get_subfolder_id(
    drive_client: Any,
    parent_folder_id: str,
    folder_name: str,
) -> str:
    """Return the Drive ID of a direct child folder with the given name.

    Queries the Drive API for a folder whose name exactly matches
    *folder_name* and whose immediate parent is *parent_folder_id*.
    Only non-trashed items are considered.

    Args:
        drive_client: An authenticated Google Drive API resource object
            (``googleapiclient.discovery.Resource``).
        parent_folder_id: The Drive ID of the parent folder to search
            inside. In this project this is always the root
            ``GOOGLE_DRIVE_FOLDER_ID`` value.
        folder_name: The exact display name of the sub-folder to find
            (e.g. ``"Ready"``, ``"Posted"``, ``"Failed"``). Case-sensitive.

    Returns:
        The Drive file ID (string) of the matching sub-folder.

    Raises:
        RuntimeError: If no sub-folder with *folder_name* exists inside
            *parent_folder_id*. The error message includes both values so
            the operator can identify the missing folder in Drive.
    """
    query = (
        f"mimeType='{_FOLDER_MIME_TYPE}'"
        f" and name='{folder_name}'"
        f" and '{parent_folder_id}' in parents"
        f" and trashed=false"
    )
    response: dict = (
        drive_client.files()
        .list(q=query, fields="files(id, name)", pageSize=10)
        .execute()
    )
    items: list[dict] = response.get("files", [])
    if not items:
        raise RuntimeError(
            f"Sub-folder '{folder_name}' was not found inside Drive folder "
            f"'{parent_folder_id}'. Create the folder in Google Drive and "
            f"re-run the script."
        )
    return items[0]["id"]


def list_ready_files(
    drive_client: Any,
    ready_folder_id: str,
) -> dict[str, str]:
    """List all non-folder files in the Ready/ Drive folder.

    Paginates through the Drive API until all results have been fetched.
    Folder entries (``mimeType == application/vnd.google-apps.folder``)
    are excluded from the returned mapping so callers only see media files.

    Args:
        drive_client: An authenticated Google Drive API resource object.
        ready_folder_id: The Drive ID of the ``Ready/`` sub-folder.

    Returns:
        A ``dict`` mapping each file's display name to its Drive file ID,
        e.g. ``{"khan-living-room.jpg": "1aBcDeFgHiJk..."}``.
        Returns an empty dict if the folder contains no files.
    """
    files: dict[str, str] = {}
    page_token: str | None = None
    query = f"'{ready_folder_id}' in parents and trashed=false"

    while True:
        kwargs: dict[str, Any] = {
            "q": query,
            "fields": "nextPageToken, files(id, name, mimeType)",
            "pageSize": 1000,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response: dict = drive_client.files().list(**kwargs).execute()

        for item in response.get("files", []):
            if item.get("mimeType") == _FOLDER_MIME_TYPE:
                continue
            files[item["name"]] = item["id"]

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    _log.info("Ready/ folder contains %d file(s) available for processing.", len(files))
    return files


def download_file_to_memory(
    drive_client: Any,
    file_id: str,
) -> bytes:
    """Download a Drive file into memory and return its raw bytes.

    Uses ``MediaIoBaseDownload`` to stream the file content in chunks,
    which keeps memory usage predictable even for large video files.
    Nothing is written to disk at any point.

    Args:
        drive_client: An authenticated Google Drive API resource object.
        file_id: The Drive file ID of the file to download.

    Returns:
        The complete file contents as a ``bytes`` object.
    """
    _log.info("Downloading file '%s' from Drive into memory.", file_id)

    request = drive_client.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buffer.getvalue()


def move_file(
    drive_client: Any,
    file_id: str,
    destination_folder_id: str,
    current_folder_id: str,
) -> None:
    """Move a Drive file from one folder to another.

    Issues a single ``files.update`` call that atomically adds the
    *destination_folder_id* parent and removes the *current_folder_id*
    parent. The file is not copied — it is moved, so it no longer
    appears in the source folder after the call completes.

    Args:
        drive_client: An authenticated Google Drive API resource object.
        file_id: The Drive file ID of the file to move.
        destination_folder_id: The Drive folder ID of the target folder.
        current_folder_id: The Drive folder ID of the folder the file
            currently lives in. This parent is removed during the move.
    """
    _log.info(
        "Moving file '%s' → folder '%s'.",
        file_id,
        destination_folder_id,
    )
    drive_client.files().update(
        fileId=file_id,
        addParents=destination_folder_id,
        removeParents=current_folder_id,
        fields="id, parents",
    ).execute()


def move_to_posted(
    drive_client: Any,
    file_id: str,
    ready_folder_id: str,
    posted_folder_id: str,
) -> None:
    """Move a file from Ready/ to Posted/ after a successful Pin upload.

    Convenience wrapper around :func:`move_file` that names the destination
    explicitly so log output and call-sites remain self-documenting.

    Args:
        drive_client: An authenticated Google Drive API resource object.
        file_id: The Drive file ID of the file to move.
        ready_folder_id: The Drive ID of the ``Ready/`` folder (source).
        posted_folder_id: The Drive ID of the ``Posted/`` folder (destination).
    """
    _log.info("File '%s': moving from Ready/ to Posted/.", file_id)
    move_file(drive_client, file_id, posted_folder_id, ready_folder_id)


def move_to_failed(
    drive_client: Any,
    file_id: str,
    ready_folder_id: str,
    failed_folder_id: str,
) -> None:
    """Move a file from Ready/ to Failed/ after all retry attempts are exhausted.

    Convenience wrapper around :func:`move_file` that names the destination
    explicitly so log output and call-sites remain self-documenting.

    Args:
        drive_client: An authenticated Google Drive API resource object.
        file_id: The Drive file ID of the file to move.
        ready_folder_id: The Drive ID of the ``Ready/`` folder (source).
        failed_folder_id: The Drive ID of the ``Failed/`` folder (destination).
    """
    _log.info("File '%s': moving from Ready/ to Failed/.", file_id)
    move_file(drive_client, file_id, failed_folder_id, ready_folder_id)
