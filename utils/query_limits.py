"""One place that decides how large a client may ask a query to be.

F43.

WHY THIS EXISTS
---------------
Most routes already clamp a client-supplied page size, but the convention was
inline and inconsistent, and the sites that were missed are the dangerous ones:
a `limit` that reaches the database un-clamped lets one request ask for the
whole table, and a *negative* limit is worse than a huge one — SQLite treats
`LIMIT -1` as "no limit at all", so the value a human reads as "impossible" is
the value that removes the bound.

The failure is silent either way: the response is a normal 200, just enormous,
so nothing in the logs looks wrong.

THE RULE
--------
Every size a client can influence goes through :func:`size_arg`, which returns
a value inside ``[minimum, maximum]`` and never returns ``None``. Out-of-range
values are **clamped**, not rejected, because these endpoints are read-only
lists that clients page through with optimistic sizes; clamping preserves the
contract while removing the risk. Where a route must reject rather than clamp
it says so explicitly (see ``routes/for_you.py``, whose ``limit`` is validated
because it feeds a paid upstream API, not a database).

THE VALUES
----------
Chosen from the conventions already in this repository rather than invented:

* :data:`LIST_LIMIT_MAX` (50) — for "top N" style limits. Matches
  ``get_trending_media``'s existing cap, ``routes/tags.py``'s popular-tags cap
  and ``routes/tv_tracking.py``'s cap.
* :data:`PAGE_SIZE_MAX` (100) — for paginated page sizes. Matches the
  ``min(request.args.get('per_page', 20, type=int), 100)`` line used in
  thirteen places.
"""
from typing import Mapping, Optional, Union

#: Maximum for "top N" result limits.
LIST_LIMIT_MAX = 50

#: Maximum for paginated page sizes.
PAGE_SIZE_MAX = 100


def bounded_int(
    value: object,
    default: int,
    maximum: int,
    minimum: int = 1,
) -> int:
    """Coerce ``value`` to an int inside ``[minimum, maximum]``.

    * absent, empty, or not an int-able value -> ``default`` (then clamped)
    * below ``minimum`` -> ``minimum``  (a negative SQL LIMIT removes the bound)
    * above ``maximum`` -> ``maximum``
    * a float string such as ``"5.0"`` is rejected rather than truncated,
      because ``int("5.0")`` raises and silently truncating would hide a
      client bug.
    """
    if isinstance(value, bool):
        # bool is an int subclass; a client sending true must not mean 1 page.
        return default
    if isinstance(value, int):
        parsed: Optional[int] = value
    elif isinstance(value, str):
        text = value.strip()
        parsed = None
        if text:
            try:
                parsed = int(text)
            except ValueError:
                parsed = None
    elif value is None:
        parsed = None
    else:
        parsed = None

    if parsed is None:
        parsed = default
    return max(minimum, min(parsed, maximum))


def size_arg(
    args: Mapping[str, Union[str, None]],
    name: str,
    default: int,
    maximum: int,
    minimum: int = 1,
) -> int:
    """Read a client-supplied size out of a query-string mapping, bounded.

    ``args`` is anything with ``get(name, None)`` — a Flask ``MultiDict`` in
    production, a plain dict in tests.
    """
    return bounded_int(args.get(name), default, maximum, minimum)


def page_args(
    args: Mapping[str, Union[str, None]],
    per_page_default: int = 20,
    per_page_max: int = PAGE_SIZE_MAX,
    minimum: int = 1,
):
    """Return ``(page, per_page)`` for a paginated endpoint.

    ``page`` is 1-based and clamped to at least 1 so that ``?page=0`` or
    ``?page=-5`` addresses the first page rather than producing a negative
    OFFSET (which SQLite also treats as unbounded-from-the-end).
    """
    page = bounded_int(args.get('page'), 1, 1 << 31, minimum=1)
    per_page = size_arg(args, 'per_page', per_page_default, per_page_max,
                        minimum)
    return page, per_page
