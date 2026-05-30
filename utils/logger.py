"""
Configures structured logging for the A.won Pinterest automation script.

Sets up the root logger with a formatter that produces lines in the format:
  [TIMESTAMP] [LEVEL   ] [MODULE] message

where TIMESTAMP is ISO 8601 UTC (e.g. 2026-05-29T08:00:01Z) and LEVEL is
left-justified within an 8-character field so all module names start at the
same column regardless of level name length.

GitHub Actions captures stdout/stderr automatically, so no file handler
is needed. Call setup_logger(__name__) in each module to obtain a named
logger that inherits the configured formatter via the root logger.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


# Log format string.  %(levelname)-8s left-justifies the level name and pads
# it with spaces to 8 characters so the [MODULE] field always starts at the
# same column — e.g. "[INFO    ]" and "[WARNING ]" align identically.
_FORMAT: str = "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s"


class _UTCFormatter(logging.Formatter):
    """Logging formatter that emits UTC timestamps in ISO 8601 format.

    Overrides ``formatTime`` to produce timestamps like
    ``2026-05-29T08:00:01Z`` (UTC, Z suffix) rather than the local-time
    string that the default ``Formatter`` would produce.
    """

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        """Return the log record's creation time as a UTC ISO 8601 string.

        Args:
            record: The log record whose ``created`` epoch timestamp is
                converted to a human-readable UTC string.
            datefmt: Ignored — this formatter always uses ISO 8601 UTC
                regardless of any datefmt passed in.

        Returns:
            A string of the form ``'YYYY-MM-DDTHH:MM:SSZ'``, e.g.
            ``'2026-05-29T08:00:01Z'``.
        """
        return (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )


def setup_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring the root logger on the first call.

    On the first call in a process, attaches a ``StreamHandler`` (stdout)
    with the ``_UTCFormatter`` to the root logger and sets its level to
    ``INFO``. All subsequent calls are idempotent — they skip handler
    configuration and simply return a new named child logger that propagates
    to the already-configured root.

    Using the root logger as the single handler attachment point means that
    every named logger across every module shares the same formatter without
    each module needing to register its own handler.

    Args:
        name: The logger name — pass ``__name__`` from the calling module
            so log lines show the short module name (e.g. ``[main]``,
            ``[image_uploader]``) rather than a generic label.

    Returns:
        A ``logging.Logger`` instance named *name*.

    Example::

        logger = setup_logger(__name__)
        logger.info("Starting A.won Pinterest upload run")
        # → [2026-05-29T08:00:01Z] [INFO    ] [main] Starting A.won ...
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_UTCFormatter(fmt=_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    return logging.getLogger(name)
