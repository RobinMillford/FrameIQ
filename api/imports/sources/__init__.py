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
import html as html_mod
import io
import re
from dataclasses import replace
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


def _letterboxd_film_slug(uri):
    """The FILM slug from any Letterboxd URI, ignoring a per-watch ordinal.

    A Letterboxd export uses two different URI shapes:

    * ``https://boxd.it/29qU`` / ``.../film/the-matrix`` — the film page
    * ``.../film/the-matrix/2`` — one specific *watch*, used by ``diary.csv``

    For review identity the trailing zero-based ordinal is dropped, so every
    watch of one film collapses onto the same key. Returning ``None`` for such
    a URI instead would make a rewatched film look like two different films,
    and the second review would be reported as an unidentifiable row.
    """
    if not uri:
        return None
    parts = [p for p in str(uri).strip().split('/') if p]
    if not parts:
        return None
    slug = parts[-1]
    if slug.isdigit() and len(parts) >= 2:
        slug = parts[-2]
    if not slug or slug.isdigit():
        # A bare ordinal with no film segment carries no identity at all.
        return None
    return slug


_BLOCK_TAGS = ('p', 'div', 'br', 'li', 'tr', 'blockquote', 'h1', 'h2', 'h3',
               'h4', 'h5', 'h6')


def _html_to_text(raw):
    """Letterboxd review bodies are HTML; FrameIQ review bodies are plain text.

    Letterboxd documents its ``Review`` column as "Text/HTML ... accepts the
    same set of HTML tags as on the Letterboxd website", so a real export can
    contain ``<p>``, ``<em>``, ``<a href>`` and friends.

    Storing that markup verbatim would be wrong twice over: FrameIQ renders
    review bodies as auto-escaped text, so the user would literally see
    ``<p>`` in their own review. Converting block boundaries to newlines and
    dropping the tags keeps the author's words and loses only presentation,
    which FrameIQ has no way to render faithfully anyway.
    """
    if raw is None:
        return None
    text = str(raw)
    if not text.strip():
        return None

# Turn block-level tags into line breaks BEFORE stripping, so paragraphs
    # do not run together into one line.
    for tag in _BLOCK_TAGS:
        text = re.sub(r'</?\s*%s\b[^>]*>' % tag, '\n', text, flags=re.I)

    # Drop <script>/<style> CONTENT, not just the tags. Flattening the tags
    # would paste the program text into the middle of the user's review.
    text = re.sub(r'<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>', ' ',
                  text, flags=re.I | re.S)
    # An unterminated <script> would otherwise keep everything after it.
    text = re.sub(r'<\s*(script|style)\b[^>]*>.*\Z', ' ', text,
                  flags=re.I | re.S)
    # Every remaining tag is dropped, including its attributes.
    text = re.sub(r'<[^>]+>', '', text)

    # Unescape entities that survive, so the text reads like prose rather than
    # "&amp;" appearing literally in the review body.
    text = html_mod.unescape(text)
    # Reviews may carry invisible characters left over from copy/paste: no-break
    # spaces and the zero-width family (ZWSP/ZWJ/ZWNJ/BOM). They are invisible
    # but break equality, so two copies of the same review compare unequal.
    text = text.replace('\xa0', ' ')
    # Explicit escapes, not literal invisible characters: a zero-width
    # codepoint in source is invisible in review and silently lost by
    # editors, so the class below can quietly lose a member.
    text = re.sub('[\u200b\u200c\u200d\u2060\ufeff]', '', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() or None


def _excerpt(text, limit=60):
    """A short, safe preview of a bad row. Never the whole row."""
    if text is None:
        return None
    value = str(text).replace('\n', ' ').strip()
    return value[:limit] if value else None


# ── Letterboxd ───────────────────────────────────────────────────────────────

def _basename(member):
    return member.name.rsplit('/', 1)[-1].lower()


def _int_or_none(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_csv_member(member, filename, invalid):
    """Decode one CSV member into a DictReader, or None if unusable."""
    try:
        text = member.data.decode('utf-8-sig')
    except UnicodeDecodeError:
        invalid.append(InvalidRow(SOURCE_LETTERBOXD, 0,
                                  '%s is not valid UTF-8' % filename))
        return None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        invalid.append(InvalidRow(SOURCE_LETTERBOXD, 0,
                                  '%s has no header row' % filename))
        return None
    return reader


def _watched_row_to_record(row, position, invalid):
    """One ``watched.csv`` row → a record, or None if the row is unusable."""
    name = (row.get('Name') or row.get('Film Name') or '').strip()
    if not name:
        invalid.append(InvalidRow(
            SOURCE_LETTERBOXD, position, 'row has no film name',
            _excerpt(row.get('Name'))))
        return None

    slug = _letterboxd_id(row.get('Letterboxd URI'))
    if not slug:
        # No stable identity => repeated imports could duplicate.
        invalid.append(InvalidRow(
            SOURCE_LETTERBOXD, position,
            'row has no Letterboxd URI, so it cannot be identified reliably',
            _excerpt(name)))
        return None

    return MovieImportRecord(
        source=SOURCE_LETTERBOXD,
        source_key=(SOURCE_LETTERBOXD, 'watched', slug),
        title=name,
        release_year=_int_or_none(row.get('Year')),
        watched_at=_parse_date(row.get('Date')),
        rating=_parse_rating(row.get('Rating')),
        # Filled in from reviews.csv below when the film has one.
        review_text=None,
        source_metadata={'letterboxd_uri': row.get('Letterboxd URI')},
    )


def _parse_watched_members(members, records, invalid):
    """Append records for every ``watched.csv`` row; return first-per-film."""
    first_record = {}
    for member in sorted(members, key=lambda m: m.name):
        reader = _read_csv_member(member, 'watched.csv', invalid)
        if reader is None:
            continue
        for position, row in enumerate(reader, start=2):
            record = _watched_row_to_record(row, position, invalid)
            if record is None:
                continue
            records.append(record)
            first_record.setdefault(record.source_key[-1], record)
    return first_record


def _review_row_record(row, position, body, invalid):
    """A review-only film (absent from watched.csv) → its own record."""
    name = (row.get('Name') or row.get('Film Name') or '').strip()
    return MovieImportRecord(
        source=SOURCE_LETTERBOXD,
        source_key=(SOURCE_LETTERBOXD, 'review', _letterboxd_film_slug(
            row.get('Letterboxd URI'))),
        title=name,
        release_year=_int_or_none(row.get('Year')),
        watched_at=_parse_date(row.get('Date')),
        rating=_parse_rating(row.get('Rating')),
        review_text=body,
        source_metadata={'letterboxd_uri': row.get('Letterboxd URI'),
                         'from_reviews_csv': True},
    )


def _apply_review_row(row, position, review_column, name_column, body,
                      first_record, records, invalid):
    """Attach one review to its film, without creating a second watch event."""
    slug = _letterboxd_film_slug(row.get('Letterboxd URI'))
    name = (row.get(name_column) or '').strip() if name_column else ''
    if not slug:
        invalid.append(InvalidRow(
            SOURCE_LETTERBOXD, position,
            'review row has no usable Letterboxd URI, so its film cannot be '
            'identified reliably', _excerpt(name)))
        return

    existing = first_record.get(slug)
    if existing is not None:
        if existing.review_text:
            # One body per film; the first wins and the duplicate is reported
            # rather than silently overwritten.
            invalid.append(InvalidRow(
                SOURCE_LETTERBOXD, position,
                'more than one review for this film; the first was kept',
                _excerpt(name)))
            return
        updated = replace(existing, review_text=body)
        records[records.index(existing)] = updated
        first_record[slug] = updated
        return

    orphan = _review_row_record(row, position, body, invalid)
    records.append(orphan)
    first_record[slug] = orphan


def _parse_review_member(member, first_record, records, invalid):
    reader = _read_csv_member(member, 'reviews.csv', invalid)
    if reader is None:
        return

    # Guard rather than guess: with no review body column the file is reported,
    # because silently treating it as empty would look like "wrote no reviews".
    review_column = _first_present(reader.fieldnames, ('Review', 'Review Text'))
    if review_column is None:
        invalid.append(InvalidRow(
            SOURCE_LETTERBOXD, 0,
            'reviews.csv has no "Review" column, so its review text cannot be '
            'read; the file was left unimported rather than guessed at'))
        return

    name_column = _first_present(reader.fieldnames, ('Name', 'Film Name'))
    for position, row in enumerate(reader, start=2):
        body = _html_to_text(row.get(review_column))
        if not body:
            # An empty body is not an error and must not create a record.
            continue
        _apply_review_row(row, position, review_column, name_column, body,
                          first_record, records, invalid)


def parse_letterboxd(members) -> Tuple[
        List[MovieImportRecord], List[InvalidRow]]:
    """Letterboxd ZIP → movie records + invalid rows.

    ``watched.csv`` is the watch history and is the only file treated as
    history. ``diary.csv`` also exists in a Letterboxd export but overlaps it;
    reading both would double-count every film, so exactly one is used.

    ``ratings.csv`` and ``watchlist.csv`` are deliberately NOT imported: a
    rating without a watch event is not a watch event, and the watchlist is not
    watch history. They are reported as unsupported rather than invented.

    ``reviews.csv`` IS read (Task F7) for the review body only. It never
    becomes extra watch history: a Letterboxd review is per-FILM, so emitting
    a row per review next to ``watched.csv`` would double-count every reviewed
    film. Each review attaches to the FIRST watch of that film and a rewatch
    stays a plain watch. A review whose film is missing from ``watched.csv``
    (only possible in a partial export) becomes its own record, because a
    review still asserts the film was watched.
    """
    records: List[MovieImportRecord] = []
    invalid: List[InvalidRow] = []

    watched_members = [m for m in members if _basename(m) == 'watched.csv']
    review_members = [m for m in members if _basename(m) == 'reviews.csv']
    if not watched_members:
        raise ValueError(
            'No watched.csv found — is this a Letterboxd data export?')

    first_record = _parse_watched_members(watched_members, records, invalid)
    for member in sorted(review_members, key=lambda m: m.name):
        _parse_review_member(member, first_record, records, invalid)
    return records, invalid


def _first_present(fieldnames, candidates):
    for candidate in candidates:
        for name in fieldnames:
            if name and name.strip().lower() == candidate.lower():
                return name
    return None


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
