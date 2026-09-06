"""Fail-fast guards for expensive in-process request workloads."""
import os
import threading
from functools import wraps

from flask import jsonify


_DEFAULT_CONCURRENCY = 1
_EXPENSIVE_PAGE_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("EXPENSIVE_PAGE_CONCURRENCY", _DEFAULT_CONCURRENCY))
)


def expensive_page_limit(view):
    """Reject expensive page requests when this worker is at capacity."""
    @wraps(view)
    def guarded_view(*args, **kwargs):
        if not _EXPENSIVE_PAGE_SLOTS.acquire(blocking=False):
            response = jsonify({
                "error": "This page is temporarily busy. Please try again."
            })
            response.status_code = 429
            response.headers["Retry-After"] = "5"
            return response
        try:
            return view(*args, **kwargs)
        finally:
            _EXPENSIVE_PAGE_SLOTS.release()

    return guarded_view
