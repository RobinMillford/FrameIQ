"""Deterministic, offline resolution (Task F6).

Resolves a normalized record to a local ``MediaItem``. The ladder is fixed and
strictly ordered, and every rung is local-only:

  1. a stable external id the record already carries (TV Time supplies a TMDb
     id, so this resolves exactly and needs no title matching at all);
  2. a unique local title match **that is also unambiguous** — matched on
     normalised title *and* release year when the record has a year.

There is deliberately no third "best fuzzy match" rung. Guessing between two
candidate films is how an importer silently corrupts someone's history, so an
ambiguous or unknown record is reported and the user decides.

**No network, ever.** ``MediaItem`` rows already in the database are the whole
universe. A title the user has never browsed is *unresolved*, not a reason to
call TMDb — import must be offline-testable and must not depend on a third
party being up.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from models import MediaItem, db

RESOLVED_BY_EXTERNAL_ID = 'external_id'
RESOLVED_BY_UNIQUE_TITLE = 'unique_title'
AMBIGUOUS = 'ambiguous'
UNRESOLVED = 'unresolved'


@dataclass(frozen=True)
class Resolution:
    """Outcome for one record."""

    status: str
    media_item: Optional[MediaItem] = None
    detail: Optional[str] = None
    candidates: Tuple[int, ...] = ()

    @property
    def is_resolved(self):
        return self.status in (RESOLVED_BY_EXTERNAL_ID,
                               RESOLVED_BY_UNIQUE_TITLE)


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
        self._load()

    def _load(self):
        rows = db.session.execute(
            db.select(MediaItem).order_by(MediaItem.id)
        ).scalars().all()
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


def resolve_movie(record, index: MediaIndex) -> Resolution:
    """Resolve a movie record. Local-only, deterministic, never guesses."""
    if record.external_id:
        item = index.by_external_id('movie', record.external_id)
        if item is not None:
            return Resolution(RESOLVED_BY_EXTERNAL_ID, item)

    match = index.by_title(record.title, record.release_year, 'movie')
    if match is AMBIGUOUS_SENTINEL:
        return Resolution(AMBIGUOUS, detail=(
            'Multiple local titles match %r; import this one from the app '
            'first so it is unambiguous.' % record.title))
    if match is not None:
        return Resolution(RESOLVED_BY_UNIQUE_TITLE, match)
    return Resolution(UNRESOLVED, detail=(
        'No local title matches %r%s. Open it in FrameIQ once, then re-run.'
        % (record.title,
           ' (%s)' % record.release_year if record.release_year else '')))


def resolve_episode(record, index: MediaIndex) -> Resolution:
    """Resolve a TV episode's show.

    TV records almost always carry a TMDb id (TV Time supplies one), so this
    normally resolves on the first rung with no title matching.
    """
    if record.show_external_id:
        item = index.by_external_id('tv', record.show_external_id)
        if item is not None:
            return Resolution(RESOLVED_BY_EXTERNAL_ID, item)

    match = index.by_title(record.show_title, None, 'tv')
    if match is AMBIGUOUS_SENTINEL:
        return Resolution(AMBIGUOUS, detail=(
            'Multiple local shows match %r.' % record.show_title))
    if match is not None:
        return Resolution(RESOLVED_BY_UNIQUE_TITLE, match)
    return Resolution(UNRESOLVED, detail=(
        'No local show matches %r. Open it in FrameIQ once, then re-run.'
        % record.show_title))