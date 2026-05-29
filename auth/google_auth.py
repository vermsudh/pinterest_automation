"""
Handles Google Service Account authentication.

Builds and returns credentialed service clients for the Google Sheets API
and Google Drive API using the service account JSON stored in the
GOOGLE_SERVICE_ACCOUNT_JSON environment variable. No credentials are ever
written to disk or printed to logs.
"""
