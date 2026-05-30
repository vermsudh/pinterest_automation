"""
Exponential backoff retry decorator for Pinterest API calls.

Wraps a function and automatically retries it when the called code raises
a RetryableError (HTTP 429, 500, or 503 from the Pinterest API).

Backoff sequence (5 attempts total):
  Attempt 1: immediate
  Attempt 2: wait 2 seconds
  Attempt 3: wait 4 seconds
  Attempt 4: wait 8 seconds
  Attempt 5: wait 16 seconds

After 5 exhausted attempts the original exception is re-raised so the
caller can mark the sheet row as Failed and continue to the next row.

HTTP 400, 403, and 404 raise plain RuntimeError (not RetryableError) in
pinterest_client.py, so they pass straight through this decorator without
triggering any retry.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from services.pinterest_client import RetryableError

_log = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# Pre-computed wait times (seconds) indexed by attempt number (0-based).
# Attempt 0 is the first try — no wait before it.  After attempt 0 fails
# the decorator waits _BACKOFF_WAITS[0] = 2 s before attempt 1, and so on.
_BACKOFF_WAITS: tuple[int, ...] = (2, 4, 8, 16)


def with_retry(max_attempts: int = 5) -> Callable[[_F], _F]:
    """Decorator factory that retries a function on ``RetryableError``.

    Only catches ``RetryableError`` — all other exceptions propagate
    immediately without any retry. This keeps the decorator from
    accidentally swallowing unrelated errors (validation failures,
    configuration problems, etc.).

    The decorator works correctly on both stand-alone functions and class
    methods because ``functools.wraps`` preserves the wrapped callable's
    signature and ``*args / **kwargs`` forwarding passes ``self`` through
    unchanged.

    Args:
        max_attempts: Total number of attempts before giving up and
            re-raising the last ``RetryableError``. Defaults to 5.
            Must be at least 1.

    Returns:
        A decorator that, when applied to a callable, wraps it with the
        retry-and-backoff behaviour described above.

    Example::

        @with_retry(max_attempts=5)
        def create_pin(self, ...):
            ...

        # Or on a standalone function:
        @with_retry()
        def upload_something():
            ...
    """

    def decorator(func: _F) -> _F:
        """Apply retry-and-backoff behaviour to *func*.

        Args:
            func: The callable to wrap.  May be a regular function or a
                bound/unbound method.

        Returns:
            A new callable with the same signature as *func* that retries
            on ``RetryableError`` up to *max_attempts* times.
        """

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Invoke *func*, retrying with exponential backoff on ``RetryableError``.

            Attempt 1 is made immediately.  If it raises ``RetryableError``,
            the attempt number and wait time are logged before sleeping.
            After *max_attempts* consecutive ``RetryableError`` exceptions the
            last one is re-raised so the caller can decide what to do (typically:
            mark the Sheet row as Failed and move on to the next row).

            Args:
                *args: Positional arguments forwarded verbatim to *func*.
                **kwargs: Keyword arguments forwarded verbatim to *func*.

            Returns:
                Whatever *func* returns on a successful call.

            Raises:
                RetryableError: If all *max_attempts* attempts raise
                    ``RetryableError``.  The exception is the one from the
                    final attempt.
                Any other exception: Propagated immediately without retrying.
            """
            last_exc: RetryableError | None = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except RetryableError as exc:
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        # All attempts exhausted — let the caller handle it.
                        raise

                    wait = _BACKOFF_WAITS[attempt]
                    _log.warning(
                        "Attempt %d failed. Retrying in %ds. Error: %s",
                        attempt + 1,
                        wait,
                        exc,
                    )
                    time.sleep(wait)

            # Unreachable — the loop always either returns or raises.
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
