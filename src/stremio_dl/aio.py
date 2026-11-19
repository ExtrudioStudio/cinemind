"""Small asyncio helpers."""

from __future__ import annotations


def reraise_cancelled[T](result: T | BaseException) -> T | Exception:
    """Return one ``asyncio.gather(..., return_exceptions=True)`` result, re-raising cancellation.

    With ``return_exceptions=True``, gather also hands back ``CancelledError`` and
    ``KeyboardInterrupt`` from the inner calls as values. Those must stop the caller instead of
    being logged as one failed request, so only ordinary exceptions are returned.
    """
    if isinstance(result, BaseException) and not isinstance(result, Exception):
        raise result
    return result
