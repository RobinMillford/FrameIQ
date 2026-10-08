"""Deterministic, offline resolution (Task F6, extended by Task F7).

Resolves a normalized record to a local ``MediaItem``. The ladder is fixed and
strictly ordered, and every rung is local-only:

  0. **a mapping the user saved themselves** (Task F7) — outranks everything
     automatic, because the user compared candidates and chose;
  1. a stable external id the record already carries (TV Time supplies a TMDb
     id, so this resolves exactly and needs no title matching at all);
  2. a unique local title match **that is also unambiguous** — matched on
     normalised title *and* release year when the record has a year.

There is deliberately no "best fuzzy match" rung. Guessing between two
candidate films is how an importer silently corrupts someone's history, so an
ambiguous or unknown record is reported, offered candidates, and the user
decides.

**No network, ever.** ``MediaItem`` rows already in the database are the whole
universe. A title the user has never browsed is *unresolved*, not a reason to
call TMDb — import must be offline-testable and must not depend on a third
party being up. Candidate generation therefore searches local rows only; a
title that does not exist locally simply has no candidates.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from models import MediaItem, db

RESOLVED_BY_MAPPING = 'mapping'
RESOLVED_BY_SELECTION = 'selection'
RESOLVED_BY_EXTERNAL_ID = 'external_id'
RESOLVED_BY_UNIQUE_TITLE = 'unique_title'
AMBIGUOUS = 'ambiguous'
UNRESOLVED = 'unresolved'

# Bound on candidates offered per record. Enough to choose from, small enough
# that a pathological title cannot return the whole MediaItem table.
MAX_CANDIDATES = 12


@dataclass(frozen=True)
class Resolution:
    """Outcome for one record."""

    status: str
    media_item: Optional[MediaItem] = None
    detail: Optional[str] = None
    candidates: Tuple[int, ...] = ()

    @property
    def is_resolved(self):
        return self.status in (RESOLVED_BY_MAPPING, RESOLVED_BY_SELECTION,
                               RESOLVED_BY_EXTERNAL_ID, RESOLVED_BY_UNIQUE_TITLE)


def normalize_title(title):
    """Fold a title for comparison.

    Unicode-normalised, casefolded, punctuation collapsed to single spaces.
    Deliberately conservative: it folds *presentation* differences only, never
    words, so two genuinely different films cannot collapse into one key.

    The character class is a negated ``\\w`` under Unicode semantics,
    NOT ``[a-z0-9]``. An ASCII-only class looks harmless and is a real bug on
    this product: it deletes every Bengali, Arabic, Japanese and Cyrillic
    character, so those titles normalise to the EMPTY string, match nothing,
    and every non-Latin film lands in "unresolved" forever. FrameIQ's own
    F5 export explicitly preserves those scripts, so import has to resolve
    them too.
    """
    if not title:
        return ''
    text = unicodedata.normalize('NFKD', str(title))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    # ``\\w`` is Unicode-aware by default: letters and digits of any script,
    # plus underscore. Everything else collapses to a single space.
    text = re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


class MediaIndex:
    """A preloaded, batched view of local media for one import run.

    Built ONCE per import so resolution is O(1) per record in memory and the
    query count is flat regardless of file size (see §18). Staleness within a
    single run is impossible because the writers cannot add media.
    """

    def __init__(self):
        self._by_tmdb: Dict[Tuple[str, int], MediaItem] = {}
        self._by_title: Dict[Tuple[str, Optional[int]], List[MediaItem]] = {}
        # Flat view for candidate generation, so scanning every local title is
        # a loop over memory instead of a query per record.
        self._all: List[MediaItem] = []
        self._load()

    def _load(self):
        rows = db.session.execute(
            db.select(MediaItem).order_by(MediaItem.id)
        ).scalars().all()
        self._all = list(rows)
        for row in rows:
            self._by_tmdb[(row.media_type, row.tmdb_id)] = row
            key = (normalize_title(row.title), row.release_date.year
                   if row.release_date else None)
            self._by_title.setdefault(key, []).append(row)
            # Also index a year-agnostic bucket so a record that has no year
            # can still match — but only when that bucket is unique, so an
            # ambiguous title can never be auto-picked.
            loose = (normalize_title(row.title), None)
            self._by_title.setdefault(loose, []).append(row)

    def __len__(self):
        return len(self._by_tmdb)

    def all_items(self):
        """Every local ``MediaItem``, in stable id order."""
        return self._all

    def by_external_id(self, media_type, external_id):
        try:
            tmdb_id = int(external_id)
        except (TypeError, ValueError):
            return None
        return self._by_tmdb.get((media_type, tmdb_id))

    def by_title(self, title, release_year, media_type):
        """Unique local match for a title, else ``None``.

        Tries the exact (title, year) bucket first, then the year-agnostic
        bucket. Both must resolve to exactly one candidate of the right media
        type — two candidates means ambiguous, and we refuse rather than pick.
        """
        key = normalize_title(title)
        if not key:
            return None
        buckets = []
        if release_year:
            buckets.append(self._by_title.get((key, int(release_year)), []))
        buckets.append(self._by_title.get((key, None), []))
        for bucket in buckets:
            candidates = [m for m in bucket if m.media_type == media_type]
            # Deduplicate: a row indexed in both buckets must not look like
            # two candidates and create a false ambiguity.
            unique = {m.id: m for m in candidates}
            if len(unique) == 1:
                return next(iter(unique.values()))
            if len(unique) > 1:
                return AMBIGUOUS_SENTINEL
        return None


class _Ambiguous:
    """Marker distinguishing 'many candidates' from 'none'."""

    def __repr__(self):
        return '<ambiguous>'


AMBIGUOUS_SENTINEL = _Ambiguous()


def resolve_movie(record, index: MediaIndex, mapping_set=None,
                  selection=None) -> Resolution:
    """Resolve a movie record. Local-only, deterministic, never guesses.

    Order of trust: saved mapping → explicit user selection → external id →
    unique title. A mapping or a selection both outrank inference, and neither
    is ever overridden by a heuristic.
    """
    if mapping_set is not None:
        mapped = mapping_set.media_for(record)
        if mapped is not None:
            return Resolution(RESOLVED_BY_MAPPING, mapped)

    if selection is not None:
        # The user picked this title explicitly for this exact record.
        return Resolution(RESOLVED_BY_SELECTION, selection)

    if record.external_id:
        item = index.by_external_id('movie', record.external_id)
        if item is not None:
            return Resolution(RESOLVED_BY_EXTERNAL_ID, item)

    match = index.by_title(record.title, record.release_year, 'movie')
    if match is AMBIGUOUS_SENTINEL:
        return Resolution(AMBIGUOUS, detail=(
            'Multiple local titles match %r; choose the right one below.'
            % record.title))
    if match is not None:
        return Resolution(RESOLVED_BY_UNIQUE_TITLE, match)
    return Resolution(UNRESOLVED, detail=(
        'No local title matches %r%s. Choose one below, or search for it.'
        % (record.title,
           ' (%s)' % record.release_year if record.release_year else '')))


def resolve_episode(record, index: MediaIndex, mapping_set=None,
                    selection=None) -> Resolution:
    """Resolve a TV episode's show.

    TV records almost always carry a TMDb id (TV Time supplies one), so this
    normally resolves on the external-id rung with no title matching.

    A saved show mapping resolves EVERY episode of that show at once, which is
    how one user decision covers a whole season import (bulk resolution).
    """
    if mapping_set is not None:
        mapped = mapping_set.media_for(record)
        if mapped is not None:
            return Resolution(RESOLVED_BY_MAPPING, mapped)

    if selection is not None:
        return Resolution(RESOLVED_BY_SELECTION, selection)

    if record.show_external_id:
        item = index.by_external_id('tv', record.show_external_id)
        if item is not None:
            return Resolution(RESOLVED_BY_EXTERNAL_ID, item)

    match = index.by_title(record.show_title, None, 'tv')
    if match is AMBIGUOUS_SENTINEL:
        return Resolution(AMBIGUOUS, detail=(
            'Multiple local shows match %r; choose the right one below.'
            % record.show_title))
    if match is not None:
        return Resolution(RESOLVED_BY_UNIQUE_TITLE, match)
    return Resolution(UNRESOLVED, detail=(
        'No local show matches %r. Choose one below, or search for it.'
        % record.show_title))


def _year_of(item):
    return item.release_date.year if item.release_date else None


def _rank_candidates(exact, partial, release_year, limit):
    """Order candidates: year agreement first, then everything else by id.

    A stable order matters — the same file must always offer the same list in
    the same sequence, or "the first option" changes between previews.
    """
    year = release_year if release_year else None
    agreed = [m for m in exact if _year_of(m) == year]
    other = [m for m in exact if _year_of(m) != year]
    for group in (agreed, other, partial):
        group.sort(key=lambda m: m.id)
    merged = {m.id: m for m in agreed + other + partial}
    return list(merged.values())[:limit]


def candidates_for(index: MediaIndex, title, media_type,
                   release_year=None, limit=MAX_CANDIDATES) -> List[MediaItem]:
    """Deterministic LOCAL candidates for an unresolved record.

    Ranked, and never auto-selected: normalised-title matches first (with the
    year agreeing ahead of the year differing), then local titles that CONTAIN
    the search text.

    Everything is served from the already-preloaded :class:`MediaIndex`, so this
    issues **no queries** and cannot get slower as the file grows. It is also
    purely local — no TMDb call — so a title the user has never browsed simply
    has no candidates, which is honest rather than slow.
    """
    needle = normalize_title(title)
    if not needle or media_type not in ('movie', 'tv'):
        return []

    exact, partial = [], []
    for item in index.all_items():
        if item.media_type != media_type:
            continue
        local = normalize_title(item.title)
        if local == needle:
            exact.append(item)
        elif needle in local:
            partial.append(item)
    return _rank_candidates(exact, partial, release_year, limit)


def search_local(index: MediaIndex, query, media_type=None,
                 limit=MAX_CANDIDATES) -> List[MediaItem]:
    """Explicit, user-initiated local title search for the resolve UI.

    Still offline. A user asking "find me Dune" gets local rows only; if the
    film is not in the database the answer is an empty list, and the UI says so
    rather than silently searching somewhere else.
    """
    needle = normalize_title(query)
    if not needle:
        return []
    hits = []
    for item in index.all_items():
        if media_type and item.media_type != media_type:
            continue
        if needle in normalize_title(item.title):
            hits.append(item)
    hits.sort(key=lambda m: m.id)
    return hits[:limit]


def media_payload(item: MediaItem) -> Dict:
    """Minimal card payload for candidate lists.

    Only fields the resolve UI actually renders. Deliberately not the full
    TMDb detail shape: importing must not require fetching a title's details
    from a third party just to draw a choice.
    """
    year = item.release_date.year if item.release_date else None
    kind = 'movie' if item.media_type == 'movie' else 'tv'
    return {
        'media_id': item.id,
        'tmdb_id': item.tmdb_id,
        'media_type': item.media_type,
        'title': item.title,
        'year': year,
        'poster_path': item.poster_path,
        # Detail routes are keyed by TMDb id (routes/details.py).
        'url': '/%s/%s' % (kind, item.tmdb_id) if item.tmdb_id else None,
    }