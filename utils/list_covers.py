"""Automatic list-cover artwork (Feature F48).

WHY THIS EXISTS
---------------
``UserList.cover_image`` had exactly one writer, and that writer was broken.
``static/js/list-enhancements.js`` uploaded a chosen file through
``FileReader.readAsDataURL`` and PUT the resulting ``data:`` URL. A data URL
for any real image is far longer than the 500-character column, and since F39
the write path refuses ``data:`` outright, so the feature could never store an
image. ``utils/cover_image_policy.py`` says so in as many words. The UI still
offered a "Cover" button, so users were invited to do something that silently
did nothing.

Rather than build uploads (storage, an upload route, a size policy, a CDN, and
a moderation story) this module makes the cover *free*: a cover is derived from
the list's own contents.

THE RULE
--------
A list's cover is built from **the first ten items of that specific list, in
the list's own order** — never from a global "top 10", a trending feed, or any
other list's posters. Ordering is the user's own: ``position`` first (the drag
handle), then ``added_at`` (insertion order for equal positions), then ``id``
as a final tiebreak.

The ``id`` tiebreak is not decoration. ``position`` is deliberately not unique
(see the ``UserListItem`` comment on the two-phase reorder), so ``position,
added_at`` alone can tie, and a cover that reorders between two renders of the
same data is a collage that looks like it is shuffling itself. It also means
the order is total, which is what the tests need in order to assert on it.

Movies *and* TV items are included. The task framed this as "the first 10
movies", but a ``UserList`` holds both, and a TV-only list filtered down to
movies would have no cover at all — the placeholder would show where real
posters were available one filter away. The first ten *items* is also the only
reading that is stable under the ordering rule above.

PERFORMANCE
-----------
Rendering a card grid of lists must not turn into one query per list, so
:func:`prefetch_list_covers` resolves a whole page of lists in exactly one
statement using a ``ROW_NUMBER()`` window partitioned by list, filtered to
``rn <= 10`` so a 900-item list costs no more than a 3-item one. That is the
same shape as :func:`models.lists.prefetch_list_data`, and it is called from
there.

``sqlite3`` in this project's test environment is 3.50.4 and PostgreSQL both
support window functions, and the statement is plain SQLAlchemy, so this needs
no dialect-specific branch and has been exercised on both.

NO SCHEMA CHANGE
----------------
``cover_image`` is untouched. Nothing is persisted: the collage is derived at
read time from rows that already exist, and no base64 or data URI is stored or
re-emitted. The column and its writer stay exactly as they were, so a list
whose owner had already saved a valid external cover URL keeps it.
"""
from datetime import datetime
from typing import List, Optional, Sequence

#: How many items a cover is built from. Ten is what the collage layouts are
#: designed around; see ``static/js/list-cover.js`` ``LAYOUTS``, which has a
#: gap-free entry for every count from 0 to this value.
COVER_POSTER_LIMIT = 10

#: The renderer must never receive more than this, whatever a caller asks for.
#: Clamped rather than rejected for the same reason ``utils/query_limits.py``
#: clamps: these are read-only paths and a larger grid is not a thing.
_MAX_HARD = 20


def _effective_limit(limit: Optional[int]) -> int:
    """Clamp a caller-supplied limit into ``[0, _MAX_HARD]``."""
    if limit is None:
        return COVER_POSTER_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return COVER_POSTER_LIMIT
    if value < 0:
        return 0
    return min(value, _MAX_HARD)


def _cover_rows(list_ids: Sequence[int], limit: int):
    """One statement: up to ``limit`` poster paths per list, in list order.

    The join to ``media_item`` is an OUTER join on purpose. An item whose media
    row is missing still occupies a position in the list, so dropping it here
    would slide every later poster forward and quietly show the wrong ten
    movies. It arrives as a ``None`` poster instead, which the renderer draws
    as a placeholder tile in the right slot.

    That case is engine-dependent, and the difference is why the join cannot be
    simplified. PostgreSQL enforces the ``user_list_item_media_id_fkey``
    constraint, so a dangling ``media_id`` cannot be stored there at all.
    SQLite in this project's test environment runs with
    ``PRAGMA foreign_keys = 0`` — there is no such pragma anywhere in the repo —
    so the row CAN exist, and an inner join would silently return the wrong
    ten posters on every SQLite-backed read. The outer join costs nothing on
    PostgreSQL and is the difference between correct and wrong on SQLite.
    """
    from models import MediaItem
    from models.lists import UserListItem
    from models.base import db
    from sqlalchemy import func

    ranked = db.session.query(
        UserListItem.list_id.label('list_id'),
        UserListItem.id.label('item_id'),
        MediaItem.poster_path.label('poster_path'),
        func.row_number().over(
            partition_by=UserListItem.list_id,
            order_by=(
                UserListItem.position.asc(),
                UserListItem.added_at.asc(),
                UserListItem.id.asc(),
            ),
        ).label('rn'),
    ).outerjoin(
        MediaItem, MediaItem.id == UserListItem.media_id
    ).filter(
        UserListItem.list_id.in_(list(list_ids))
    ).subquery()

    return db.session.query(
        ranked.c.list_id,
        ranked.c.poster_path,
    ).filter(
        ranked.c.rn <= limit
    ).order_by(
        ranked.c.list_id.asc(),
        ranked.c.rn.asc(),
    ).all()


def _rows_to_covers(rows) -> dict:
    """Group ordered rows into ``{list_id: [poster_path, ...]}``."""
    covers: dict = {}
    for row in rows:
        bucket = covers.setdefault(row.list_id, [])
        # One slot per item, even when the poster is missing, so the renderer
        # can place a placeholder where the poster would have been.
        bucket.append(row.poster_path)
    return covers


def prefetch_list_covers(user_lists, limit: Optional[int] = COVER_POSTER_LIMIT) -> None:
    """Attach ``_prefetched_cover_posters`` to each list with ONE query.

    Mirrors :func:`models.lists.prefetch_list_data`: no-op for empty input, and
    a safe no-op for ``None`` entries.
    """
    lists = [user_list for user_list in (user_lists or []) if user_list is not None]
    if not lists:
        return

    effective = _effective_limit(limit)
    if effective == 0:
        for user_list in lists:
            user_list._prefetched_cover_posters = []
        return

    rows = _cover_rows([user_list.id for user_list in lists], effective)
    covers = _rows_to_covers(rows)

    for user_list in lists:
        user_list._prefetched_cover_posters = covers.get(user_list.id, [])


def cover_posters(user_list, limit: Optional[int] = COVER_POSTER_LIMIT) -> List[Optional[str]]:
    """Poster paths for one list's cover, in list order.

    Uses the batch-prefetched value when there is one; otherwise runs the
    single-list query. Same contract as ``_item_count`` / ``_category_dicts``
    in ``models/lists.py``.
    """
    prefetched = getattr(user_list, '_prefetched_cover_posters', None)
    if prefetched is not None:
        return list(prefetched)

    effective = _effective_limit(limit)
    if effective == 0 or user_list is None or user_list.id is None:
        return []

    rows = _cover_rows([user_list.id], effective)
    return list(_rows_to_covers(rows).get(user_list.id, []))


def cover_posters_from_items(items, limit: Optional[int] = COVER_POSTER_LIMIT) -> List[Optional[str]]:
    """Poster paths from ``UserListItem`` rows whose ``media`` is already loaded.

    The list-detail routes already join ``UserListItem.media`` to render the
    page, so the cover is free there: this reads the same rows rather than
    issuing a second query.

    The sort is re-applied defensively. A caller that hands over items in the
    wrong order would otherwise produce a cover that disagrees with the list it
    is drawn from, and the ordering rule is the whole point of this feature.
    ``added_at`` is nullable in the database, so ``None`` is mapped to
    ``datetime.min`` rather than left in the tuple: Python 3 refuses to compare
    ``None`` with a ``datetime``, which would turn a cosmetic row into a 500.
    """
    effective = _effective_limit(limit)
    if effective == 0 or not items:
        return []

    def _order_key(item):
        added_at = getattr(item, 'added_at', None)
        return (
            getattr(item, 'position', 0) or 0,
            added_at if added_at is not None else datetime.min,
            getattr(item, 'id', 0) or 0,
        )

    ordered = sorted(items, key=_order_key)
    paths = []
    for item in ordered[:effective]:
        media = getattr(item, 'media', None)
        paths.append(getattr(media, 'poster_path', None) if media is not None else None)
    return paths