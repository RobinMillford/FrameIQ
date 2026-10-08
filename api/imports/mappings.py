"""Persistent import source mappings (Task F7).

F6 refused to guess. An unknown or ambiguous external title was reported and
the user's only recovery was "open the title in FrameIQ, then re-run". That is
honest but it means re-making the same judgement for every file, forever.

This module stores the answer, and makes it the FIRST rung of resolution.

Why the key is not the record's ``source_key``
----------------------------------------------
``record.source_key`` identifies a *row in the file*: ``('tvtime', 'episode',
show, season, episode)``. A TV Time show can contribute forty rows. If the user
maps that show once, all forty must resolve — otherwise they are asked the same
question forty times.

So the mapping key is the **resolution identity** (:func:`resolution_key`):
one film slug for a movie, one show identity for a TV show. A single mapping
then resolves every episode of that show, which is bulk resolution rather than
a separate feature.

Ordering of trust
-----------------
A saved mapping outranks everything automatic. The user compared candidates and
picked one; no title heuristic has that information, so an automatic match must
never override it.
"""
from typing import Dict, List, Optional, Tuple

import json

from models import ImportSourceMapping, MediaItem, db

from api.imports.sources import SOURCES

# Re-exported so callers do not need to import models directly.
__all__ = ['resolution_key', 'MappingSet', 'load_mapping_set', 'save_mapping',
           'delete_mapping', 'list_mappings', 'MappingError', 'encode_key',
           'decode_key']

MAX_SOURCE_KEY = 255
MAX_SOURCE_TITLE = 300
MEDIA_TYPES = ('movie', 'tv')


class MappingError(ValueError):
    """A mapping request was rejected. Safe to show the user verbatim."""


def encode_key(key) -> Optional[str]:
    """Serialise a resolution key for the wire.

    JSON rather than a hand-rolled separator: a Letterboxd slug or a TV Time id
    is simple today, but a separator-based encoding would silently mis-parse the
    first source key that contained the separator, and the result would be a
    mapping applied to the wrong film.
    """
    if not key:
        return None
    return json.dumps([key[1], key[2]])


def decode_key(text, source=None) -> Optional[Tuple[str, str, str]]:
    """Parse a wire key back to ``(source, media_type, source_key)``.

    ``source`` is taken from the URL rather than the body so a client cannot
    post a mapping into a namespace it does not own.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) != 2:
        return None
    media_type, source_key = parsed
    if media_type not in MEDIA_TYPES:
        return None
    if not isinstance(source_key, str):
        return None
    source_key = source_key.strip()
    if not source_key or len(source_key) > MAX_SOURCE_KEY:
        return None
    return (source or SOURCES[0], media_type, source_key)


def resolution_key(record) -> Optional[Tuple[str, str, str]]:
    """The stable identity a mapping should be keyed on for this record.

    Returns ``(source, media_type, source_key)`` or ``None`` when the record
    carries no usable identity (in which case nothing can be mapped and the
    record can only be resolved automatically or skipped).

    For a TV episode this deliberately returns the SHOW identity, so one
    mapping covers the whole show.
    """
    source = record.source
    if source not in SOURCES:
        return None

    key = record.source_key
    if not key:
        return None

    # Episodes: collapse (source, ..., show, season, episode) to the show.
    if getattr(record, 'season_number', None) is not None:
        show_identity = _show_identity(record)
        if not show_identity:
            return None
        return (source, 'tv', show_identity)

    # Movies: the source key's final segment is the film/show identity the
    # adapter already derived (a Letterboxd slug, or a TMDb id).
    identity = str(key[-1]).strip()
    if not identity:
        return None
    return (source, 'movie', identity[:MAX_SOURCE_KEY])


def _show_identity(record):
    """A TV show's stable identity, preferring an external id over its title."""
    external = getattr(record, 'show_external_id', None)
    if external:
        return str(external).strip()
    # Fall back to the show identity the adapter put in the key. A title or a
    # service id is a weaker key than a TMDb id but is still stable within one
    # source, and refusing to map it would leave some shows permanently
    # unfixable.
    #
    # The TV Time episode key is (source, show_key, season, episode), so the
    # show is at index 1 — NOT the second-from-last element, which is the
    # season number.
    key = record.source_key
    if len(key) >= 4:
        candidate = str(key[1]).strip()
        # Guard against a future adapter shape that reuses a generic label
        # here; a literal 'episode' is not an identity.
        if candidate and candidate.lower() not in ('episode', 'episodes'):
            return candidate[:MAX_SOURCE_KEY]
    return None


class MappingSet:
    """One user's mappings for one source, loaded once per import run.

    Read-only view: this never writes, so preview and apply cannot accidentally
    persist a mapping as a side effect of merely looking at a file.
    """

    def __init__(self, mappings):
        self._by_key = {}
        for mapping in mappings:
            self._by_key[self.key_of(mapping)] = mapping

    @staticmethod
    def key_of(mapping):
        return (mapping.source, mapping.media_type, mapping.source_key)

    def get(self, key):
        """The mapping for a ``(source, media_type, source_key)`` triple."""
        if not key:
            return None
        return self._by_key.get(tuple(key))

    def media_for(self, record):
        """The mapped ``MediaItem`` for a record, or ``None``.

        Returns ``None`` when there is no mapping, and ALSO when the mapping's
        target row has disappeared or has the wrong media type — a stale
        mapping must not resurrect a deleted title or silently apply a movie
        mapping to a show.
        """
        mapping = self.get(resolution_key(record))
        if mapping is None:
            return None
        item = db.session.get(MediaItem, mapping.media_id)
        if item is None:
            return None
        if item.media_type != mapping.media_type:
            return None
        return item

    def keys(self):
        return set(self._by_key)

    def __len__(self):
        return len(self._by_key)


def load_mapping_set(user_id, source) -> MappingSet:
    """Load this user's mappings for one source. One query per import run."""
    rows = db.session.execute(
        db.select(ImportSourceMapping).where(
            ImportSourceMapping.user_id == user_id,
            ImportSourceMapping.source == source,
        ).order_by(ImportSourceMapping.id)
    ).scalars().all()
    return MappingSet(rows)


def save_mapping(user_id, source, media_type, source_key, media_id,
                 source_title=None) -> ImportSourceMapping:
    """Create or update one mapping. Returns the stored row.

    Validation is strict and happens BEFORE any write, because every failure
    mode here is a security or correctness boundary:

    * ``source`` must be an adapter we actually support, so a caller cannot
      mint a mapping namespace that no import will ever consult;
    * ``media_type`` must be ``movie`` or ``tv`` and must match the target
      row's real type, so a movie mapping cannot be applied to a show;
    * the target ``MediaItem`` must exist, so a mapping cannot dangle;
    * ``source_key`` must be non-empty and bounded.

    Re-saving the same identity UPDATES the target rather than raising, because
    the user changing their mind is the expected flow, not an error.
    """
    if source not in SOURCES:
        raise MappingError('Unknown import source %r.' % (source,))
    if media_type not in MEDIA_TYPES:
        raise MappingError('A mapping must target a movie or a TV show.')

    key = (source_key or '').strip()
    if not key:
        raise MappingError('A mapping needs a source key.')
    if len(key) > MAX_SOURCE_KEY:
        raise MappingError('That source key is too long to store.')

    item = db.session.get(MediaItem, media_id) if media_id else None
    if item is None:
        raise MappingError('That title does not exist in FrameIQ.')
    if item.media_type != media_type:
        raise MappingError(
            'That title is a %s, not a %s.' % (item.media_type, media_type))

    title = (source_title or '').strip() or None
    if title and len(title) > MAX_SOURCE_TITLE:
        title = title[:MAX_SOURCE_TITLE]

    existing = db.session.execute(
        db.select(ImportSourceMapping).where(
            ImportSourceMapping.user_id == user_id,
            ImportSourceMapping.source == source,
            ImportSourceMapping.media_type == media_type,
            ImportSourceMapping.source_key == key,
        )
    ).scalars().first()

    if existing is not None:
        existing.media_id = item.id
        existing.media_tmdb_id = item.tmdb_id
        if title:
            existing.source_title = title
        db.session.flush()
        return existing

    mapping = ImportSourceMapping(
        user_id=user_id,
        source=source,
        media_type=media_type,
        source_key=key,
        source_title=title,
        media_id=item.id,
        media_tmdb_id=item.tmdb_id,
    )
    db.session.add(mapping)
    db.session.flush()
    return mapping


def delete_mapping(user_id, mapping_id) -> None:
    """Remove one of THIS user's mappings.

    Scoped by ``user_id`` in the WHERE clause rather than looked up and then
    compared, so there is no path where another user's mapping id deletes their
    row.

    Deleting a mapping never touches watch history: the mapping is import
    bookkeeping, not a record that anything was watched. The imported Diary
    entries and episode rows stay exactly as they are.
    """
    removed = db.session.execute(
        db.delete(ImportSourceMapping).where(
            ImportSourceMapping.id == mapping_id,
            ImportSourceMapping.user_id == user_id,
        )
    )
    if not removed.rowcount:
        raise MappingError('That mapping no longer exists.')
    db.session.flush()


def list_mappings(user_id, source=None) -> List[Dict]:
    """This user's mappings, newest first, with staleness reported.

    A mapping whose target ``MediaItem`` has been deleted, or whose type no
    longer matches, is still LISTED with ``stale: true`` rather than hidden —
    a mapping the user cannot see is a mapping they can never clean up.
    """
    query = db.select(ImportSourceMapping).where(
        ImportSourceMapping.user_id == user_id)
    if source:
        query = query.where(ImportSourceMapping.source == source)
    rows = db.session.execute(
        query.order_by(ImportSourceMapping.updated_at.desc(),
                       ImportSourceMapping.id.desc())
    ).scalars().all()

    # One query for every referenced title instead of one per mapping.
    media_ids = {row.media_id for row in rows if row.media_id}
    live: Dict[int, MediaItem] = {}
    if media_ids:
        items = db.session.execute(
            db.select(MediaItem).where(MediaItem.id.in_(media_ids))
        ).scalars().all()
        live = {item.id: item for item in items}

    result = []
    for row in rows:
        item = live.get(row.media_id)
        stale = item is None or item.media_type != row.media_type
        result.append({
            'id': row.id,
            'source': row.source,
            'media_type': row.media_type,
            'source_key': row.source_key,
            'source_title': row.source_title,
            'media_id': row.media_id,
            'media_title': item.title if item is not None else None,
            'media_year': item.release_date.year
            if item is not None and item.release_date else None,
            'media_tmdb_id': item.tmdb_id if item is not None else None,
            'stale': stale,
            'stale_reason': (
                'the mapped title no longer exists' if item is None
                else 'the mapped title is now a %s' % item.media_type
            ) if stale else None,
            'updated_at': row.updated_at.isoformat()
            if row.updated_at else None,
        })
    return result