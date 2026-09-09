"""Fail-fast guards for expensive in-process request workloads."""
import os
import threading
from functools import wraps

from flask import jsonify, render_template, request


_DEFAULT_CONCURRENCY = 1
_EXPENSIVE_PAGE_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("EXPENSIVE_PAGE_CONCURRENCY", _DEFAULT_CONCURRENCY))
)

_BUSY_MESSAGE = "This page is temporarily busy. Please try again."


def _wants_html() -> bool:
    """True when the client is a normal browser navigation.

    Fetch/XHR clients (our own JS, monitoring, curl) keep the JSON contract;
    top-level document navigations get a self-reloading HTML page so the user
    is not stranded on raw JSON after clicking Watch/Stream.
    """
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


def _busy_response():
    """Build the 429 response for an at-capacity expensive page request.

    Concurrency limiting is identical in both branches — only the payload
    differs. Browsers receive a page that reloads itself once (bounded, using
    the same Retry-After window), so a transient busy state self-heals instead
    of requiring a manual hard refresh. JSON clients keep Retry-After too.
    """
    if _wants_html():
        from flask import make_response
        response = make_response(render_template("errors/busy.html"))
        response.status_code = 429
        response.headers["Retry-After"] = "5"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response
    response = jsonify({"error": _BUSY_MESSAGE})
    response.status_code = 429
    response.headers["Retry-After"] = "5"
    return response


def expensive_page_limit(view):
    """Reject expensive page requests when this worker is at capacity."""
    @wraps(view)
    def guarded_view(*args, **kwargs):
        if not _EXPENSIVE_PAGE_SLOTS.acquire(blocking=False):
            return _busy_response()
        try:
            return view(*args, **kwargs)
        finally:
            _EXPENSIVE_PAGE_SLOTS.release()

    return guarded_view
