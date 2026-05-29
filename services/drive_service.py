"""
Downloads media files from Google Drive and manages folder moves.

Searches the Ready/ folder (GOOGLE_DRIVE_FOLDER_ID) for files by exact
filename. Downloads image files entirely into memory (as bytes) so they
can be base64-encoded without writing to disk. Streams video files from
Drive to avoid loading large files (up to 2 GB) into memory at once.

After each upload attempt, moves the file to the correct destination:
  - Success: moves to Posted/ folder
  - Failure: moves to Failed/ folder

All moves use the Drive API files.update method to change the parent
folder ID — files are never copied and deleted.
"""
