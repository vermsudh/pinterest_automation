"""
Configures structured logging for the A.won Pinterest automation script.

Sets up the root logger with a formatter that produces lines in the format:
  [TIMESTAMP] [LEVEL] [MODULE] message

where TIMESTAMP is ISO 8601 UTC (e.g. 2026-05-29T08:00:01Z).

GitHub Actions captures stdout/stderr automatically, so no file handler
is needed. Call get_logger(__name__) in each module to obtain a named logger.
"""
