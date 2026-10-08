"""Source adapters (Task F6).

Each adapter's only job is: bytes in, normalized records out. It never touches
the database, never resolves a title, and never imports anything from the
canonical TV or diary write paths. That isolation is what makes a new source a
single file rather than a cross-cutting change.

Sources
-------
``letterboxd``
    A real Letterboxd data export: a ZIP of CSVs. Letterboxd's export format
    is film-centric, so this adapter produces **movie records only**. It never
    fabricates TV seasons/episodes, because the source has none.

``tvtime``
    A TV Time GDPR export: a JSON document, optionally wrapped in a ZIP.

Both report malformed rows as :class:`InvalidRow` rather than dropping them, so
the preview can say "3 rows were malformed" instead of silently losing data.
"""
import csv
import io
from datetime import datetime
from typing import List, Tuple

from api.imports.records import (InvalidRow, MovieImportRecord,
                                 TVEpisodeImportRecord)

SOURCE_LETTERBOXD = 'letterboxd'
SOURCE_TVTIME = 'tvtime'

SOURCES = (SOURCE_LETTERBOXD, SOURCE_TVTIME)

# Letterboxd ratings are 0.5–5 in 0.5 steps.
_MAX_RATING = 5.0
# TV Time may carry a 0–10 scale; normalise to FrameIQ's 0.5–5 stars.
_TVTIME_MAX_RATING = 10.0


# ── shared helpers ───────────────────────────────────────────────────────────

def _parse_date(raw):
    """Lenient date parsing. Returns ``None`` when unusable.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM`` (Letterboxd's format) and
    ISO-8601 with a ``Z``. Returns ``None`` rather than raising so one bad
    cell does not fail the whole file — the caller decides whether a missing
    date is fatal for that row.
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date()


def _parse_rating(raw, scale_max=_MAX_RATING):
    """Clamp to FrameIQ's 0.5–5.0 star scale; ``None`` when absent/invalid.

    A rating of 0 in Letterboxd means "no rating" and must not become 0 stars,
    because ``DiaryEntry.rating`` is nullable and ``0`` would look like a
    deliberate rating.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if scale_max != _MAX_RATING:
        value = value * (_MAX_RATING / scale_max)
    value = round(value * 2) / 2.0
    return max(0.5, min(_MAX_RATING, value))


def _letterboxd_id(uri):
    """``/film/dune-2021/`` → ``dune-2021`` (stable across exports)."""
    if not uri:
        return None
    parts = [p for p in str(uri).strip().split('/') if p]
    return parts[-1] if parts else None


def _excerpt(text, limit=60):
    """A short, safe preview of a bad row. Never the whole row."""
    if text is None:
        return None
    value = str(text).replace('\n', ' ').strip()
    return value[:limit] if value else None


# ── Letterboxd ───────────────────────────────────────────────────────────────

def parse_letterboxd(members) -> Tuple[
        List[MovieImportRecord], List[InvalidRow]]:
    """Letterboxd ZIP → movie records + invalid rows.

    ``watched.csv`` is the watch history and is the only file treated as
    history. ``diary.csv`` also exists in a Letterboxd export but overlaps it;
    reading both would double-count every film, so exactly one is used.

    ``ratings.csv`` and ``watchlist.csv`` are deliberately NOT imported: a
    rating without a watch event is not a watch event, and the watchlist is not
    watch history. They are reported as unsupported rather than invented.
    """
    wanted = {'watched.csv'}
    records: List[MovieImportRecord] = []
    invalid: List[InvalidRow] = []

    csv_members = [m for m in members
                   if m.name.rsplit('/', 1)[-1].lower() in wanted]
    if not csv_members:
        raise ValueError(
            'No watched.csv found — is this a Letterboxd data export?')

    for member in sorted(csv_members, key=lambda m: m.name):
        try:
            text = member.data.decode('utf-8-sig')
        except UnicodeDecodeError:
            invalid.append(InvalidRow(
                SOURCE_LETTERBOXD, 0,
                'watched.csv is not valid UTF-8'))
            continue

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            invalid.append(InvalidRow(
                SOURCE_LETTERBOXD, 0, 'watched.csv has no header row'))
            continue

        for position, row in enumerate(reader, start=2):
            name = (row.get('Name') or '').strip()
            if not name:
                invalid.append(InvalidRow(
                    SOURCE_LETTERBOXD, position,
                    'row has no film name', _excerpt(row.get('Name'))))
                continue

            year = None
            raw_year = (row.get('Year') or '').strip()
            if raw_year:
                try:
                    year = int(raw_year)
                except ValueError:
                    year = None

            slug = _letterboxd_id(row.get('Letterboxd URI'))
            if not slug:
                # No stable identity => repeated imports could duplicate.
                invalid.append(InvalidRow(
                    SOURCE_LETTERBOXD, position,
                    'row has no Letterboxd URI, so it cannot be identified '
                    'reliably', _excerpt(name)))
                continue

            records.append(MovieImportRecord(
                source=SOURCE_LETTERBOXD,
                source_key=(SOURCE_LETTERBOXD, 'watched', slug),
                title=name,
                release_year=year,
                watched_at=_parse_date(row.get('Date')),
                rating=_parse_rating(row.get('Rating')),
                # Letterboxd exports no review text in watched.csv, and the
                # diary body is a separate user-authored artefact this
                # adapter deliberately does not reach for.
                review_text=None,
                source_metadata={'letterboxd_uri': row.get('Letterboxd URI')},
            ))
    return records, invalid


# ── TV Time ──────────────────────────────────────────────────────────────────

def parse_tvtime(document) -> Tuple[
        List[TVEpisodeImportRecord], List[MovieImportRecord],
        List[InvalidRow]]:
    """TV Time GDPR JSON → episode records (+ movies if present) + invalid rows.

    TV Time is a TV tracker, but its export may also carry a ``movies`` array.
    Those are parsed as movie records so a TV-only user is unaffected and a
    mixed export does not silently drop half the data.
    """
    episodes: List[TVEpisodeImportRecord] = []
    movies: List[MovieImportRecord] = []
    invalid: List[InvalidRow] = []

    if not isinstance(document, dict):
        raise ValueError('TV Time export must be a JSON object.')

    shows = document.get('shows') or []
    if not isinstance(shows, list):
        raise ValueError('TV Time export has a malformed "shows" array.')

    for show_index, show in enumerate(shows):
        if not isinstance(show, dict):
            invalid.append(InvalidRow(SOURCE_TVTIME, show_index,
                                      'show entry is not an object'))
            continue
        show_name = (show.get('name') or '').strip()
        show_tmdb = show.get('tmdb_id')
        show_tvdb = show.get('tvdb_id')
        if not show_name or not (show_tmdb or show_tvdb):
            invalid.append(InvalidRow(
                SOURCE_TVTIME, show_index,
                'show has no name or no external id', _excerpt(show_name)))
            continue
        # TMDb id is the identity FrameIQ can actually use; keep TVdb as
        # metadata rather than pretending it is resolvable.
        show_key = str(show_tmdb or show_tvdb)

        seasons = show.get('seasons') or []
        if not isinstance(seasons, list):
            invalid.append(InvalidRow(
                SOURCE_TVTIME, show_index, 'show has a malformed "seasons" '
                'array', _excerpt(show_name)))
            continue

        for season in seasons:
            if not isinstance(season, dict):
                invalid.append(InvalidRow(
                    SOURCE_TVTIME, show_index, 'season entry is not an object',
                    _excerpt(show_name)))
                continue
            try:
                season_number = int(season.get('number'))
            except (TypeError, ValueError):
                invalid.append(InvalidRow(
                    SOURCE_TVTIME, show_index, 'season has no usable number',
                    _excerpt(show_name)))
                continue

            for episode in season.get('episodes') or []:
                if not isinstance(episode, dict):
                    invalid.append(InvalidRow(
                        SOURCE_TVTIME, show_index,
                        'episode entry is not an object', _excerpt(show_name)))
                    continue
                if episode.get('is_specials'):
                    # FrameIQ's canonical ledger excludes specials (F4), so a
                    # special is reported, never imported as a regular season.
                    invalid.append(InvalidRow(
                        SOURCE_TVTIME, show_index,
                        'specials are not canonical watched episodes',
                        _excerpt(episode.get('name'))))
                    continue
                try:
                    episode_number = int(episode.get('number'))
                except (TypeError, ValueError):
                    invalid.append(InvalidRow(
                        SOURCE_TVTIME, show_index,
                        'episode has no usable number',
                        _excerpt(episode.get('name'))))
                    continue
                watched = episode.get('last_watched')
                if not watched:
                    # Never watched => not history.
                    continue

                episodes.append(TVEpisodeImportRecord(
                    source=SOURCE_TVTIME,
                    source_key=(SOURCE_TVTIME, show_key,
                                str(season_number), str(episode_number)),
                    show_title=show_name,
                    show_external_id=show_key,
                    season_number=season_number,
                    episode_number=episode_number,
                    episode_title=(episode.get('name') or '').strip() or None,
                    watched_at=_parse_date(watched),
                    # TV Time has no per-episode rating; None is honest.
                    rating=None,
                    source_metadata={
                        'tvtime_show_id': show.get('id'),
                        'tvtime_episode_id': episode.get('id'),
                        'first_aired': episode.get('first_aired'),
                    },
                ))

    for movie_index, movie in enumerate(document.get('movies') or []):
        if not isinstance(movie, dict):
            invalid.append(InvalidRow(SOURCE_TVTIME, movie_index,
                                      'movie entry is not an object'))
            continue
        name = (movie.get('name') or movie.get('title') or '').strip()
        tmdb_id = movie.get('tmdb_id')
        watched = movie.get('last_watched') or movie.get('watched_at')
        if not name or not tmdb_id or not watched:
            invalid.append(InvalidRow(
                SOURCE_TVTIME, movie_index,
                'movie has no name, no tmdb_id or no watch date',
                _excerpt(name)))
            continue
        movies.append(MovieImportRecord(
            source=SOURCE_TVTIME,
            source_key=(SOURCE_TVTIME, 'movies', str(tmdb_id)),
            title=name,
            external_id=str(tmdb_id),
            release_year=movie.get('year'),
            watched_at=_parse_date(watched),
            rating=_parse_rating(movie.get('rating'), scale_max=_TVTIME_MAX_RATING),
            source_metadata={'tvtime_movie_id': movie.get('id')},
        ))
    return episodes, movies, invalid
