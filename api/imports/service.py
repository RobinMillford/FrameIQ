"""Import orchestration: preview and apply (Tasks F6, F7).

    upload bytes
        -> uploads.load_payload      (size / type / zip safety)
        -> sources.parse_*           (normalized records)
        -> mappings.load_mapping_set (the user's saved answers)
        -> resolve.*                 (deterministic, local, offline)
        -> preview: classify only, WRITE NOTHING
        -> apply : write via writer  (canonical paths), in one transaction

Two-stage by design. Preview is a pure read: it resolves, classifies and
reports, and it never persists a mapping — including when the user supplies
choices — because a mapping is a durable answer and must not appear as a side
effect of looking at a file. Apply is the only operation that writes, and it
runs inside a single transaction so a failure cannot leave a half-imported
account.

Why there is no server-side import session
------------------------------------------
F7's resolve step needs to carry the user's choices from preview to apply. The
obvious design is an upload, then a server-side session holding the parsed
rows. This module deliberately does NOT do that: the browser already holds the
``File``, and choices are a few kilobytes of JSON, so apply simply re-uploads
the same file with the selections attached. Re-parsing at apply is what makes
the server able to check every submitted key against the records actually
present in that file — a session id would only remove that check.

So the flow stays stateless, no raw upload is persisted, and a preview cannot
be replayed against a different file than the one that was previewed.

Classification, reported to the user verbatim:

    imported        written now
    already_present the user already has this exact event (idempotency)
    unresolved      no local match — the user picks a candidate or skips it
    ambiguous       several local matches — never auto-picked
    ineligible      F4's gate refuses it (future episode, ineligible special)
    invalid         the source row was malformed
    unsupported     a field/record the source has that FrameIQ will not invent
    conflict        the user already has this review; theirs was kept
"""
import logging
from collections import OrderedDict
from typing import Any, Dict, List

from api.imports import uploads
from api.imports.mappings import (MappingError, decode_key, encode_key,
                                  load_mapping_set, resolution_key, save_mapping)
from api.imports.records import MovieImportRecord, TVEpisodeImportRecord
from api.imports.resolve import (MediaIndex, candidates_for, media_payload,
                                 resolve_episode, resolve_movie)
from api.imports.sources import (SOURCE_LETTERBOXD, SOURCE_TVTIME,
                                 parse_letterboxd, parse_tvtime)
from api.imports.writer import (existing_episode_keys,
                                existing_movie_watch_keys,
                                existing_review_keys, preflight_episode,
                                write_episode_watch, write_movie_review,
                                write_movie_watch)
from models import db

logger = logging.getLogger(__name__)

IMPORTED = 'imported'
ALREADY_PRESENT = 'already_present'
UNRESOLVED = 'unresolved'
AMBIGUOUS = 'ambiguous'
INELIGIBLE = 'ineligible'
INVALID = 'invalid'
UNSUPPORTED = 'unsupported'
CONFLICT = 'conflict'

_EMPTY_REPORT = OrderedDict([
    (IMPORTED, 0), (ALREADY_PRESENT, 0), (UNRESOLVED, 0), (AMBIGUOUS, 0),
    (INELIGIBLE, 0), (INVALID, 0), (UNSUPPORTED, 0), (CONFLICT, 0),
])

# Candidates shown inline per unresolved record. The full list is reachable
# through the explicit search endpoint; the preview only needs enough to be
# useful without turning a 5,000-row file into a 60,000-card page.
CANDIDATES_IN_PREVIEW = 6


class ImportRejected(uploads.UploadRejected):
    """Source-level refusal (bad structure). 4xx, never a 500."""


class SelectionRejected(ImportRejected):
    """A submitted choice was not valid for THIS file. 4xx, never a 500."""


def _blank_report():
    return OrderedDict(_EMPTY_REPORT)


def parse_source(source, filename, payload):
    """Bytes → (movies, episodes, invalid rows). Never writes."""
    document, members = uploads.load_payload(source, filename, payload)

    if source == SOURCE_LETTERBOXD:
        if members is None:
            raise ImportRejected('A Letterboxd export must be a ZIP archive.')
        try:
            records, invalid = parse_letterboxd(members)
        except ValueError as exc:
            # The adapter reports a structurally-wrong document as ValueError;
            # translate it so callers catch one error type.
            raise ImportRejected(str(exc)[:200])
        return records, [], invalid

    if source == SOURCE_TVTIME:
        if document is None:
            raise ImportRejected('A TV Time export must contain JSON.')
        try:
            episodes, movies, invalid = parse_tvtime(document)
        except ValueError as exc:
            raise ImportRejected(str(exc)[:200])
        return movies, episodes, invalid

    raise ImportRejected('Unknown import source.', status=404)


def _validated_keys(source, movies, episodes, selections):
    """Well-formed keys from the client that exist in this file.

    Raises 4xx for anything malformed or unknown. Silence here would let an
    apply write fewer rows than the preview promised and still report success.
    """
    if not isinstance(selections, dict):
        raise SelectionRejected('Selections must be an object.')

    present = set()
    for record in list(movies) + list(episodes):
        key = resolution_key(record)
        if key is not None:
            present.add(key)

    validated = {}
    for raw_key, media_id in selections.items():
        key = decode_key(raw_key, source)
        if key is None:
            raise SelectionRejected(
                'That selection is not a valid source identity.')
        if key not in present:
            raise SelectionRejected(
                'That selection does not match any row in this file, so it '
                'was refused rather than guessed at.')
        try:
            validated[raw_key] = (key, int(media_id))
        except (TypeError, ValueError):
            raise SelectionRejected('That selection has no valid title.')
    return validated


def build_selection_lookup(source, movies, episodes, selections):
    """Validate submitted choices against this file's own records.

    Returns ``{encoded_resolution_key: MediaItem}``.

    Every entry is checked three ways before it may influence a write, because
    this is the one path where client-supplied data decides which of the user's
    titles a watch event lands on:

      1. the key must be well-formed and belong to ``source``;
      2. a record with that resolution identity must exist **in this file** —
         otherwise a client could map identities it invented and steer an
         unrelated row onto an unrelated title;
      3. the target must be a real ``MediaItem`` whose ``media_type`` matches
         the key, so a movie choice cannot be applied to a show.

    A malformed entry raises :class:`SelectionRejected` (a 4xx) rather than
    being dropped, so the client is told instead of silently importing less
    than the preview said.
    """
    if not selections:
        return {}

    from models import MediaItem  # local import keeps module import light

    validated = _validated_keys(source, movies, episodes, selections)
    if not validated:
        return {}

    items = db.session.execute(
        db.select(MediaItem).where(
            MediaItem.id.in_(sorted({v[1] for v in validated.values()})))
    ).scalars().all()
    by_id = {item.id: item for item in items}

    lookup = {}
    for raw_key, (key, media_id) in validated.items():
        item = by_id.get(media_id)
        if item is None:
            raise SelectionRejected('That title does not exist in FrameIQ.')
        if item.media_type != key[1]:
            raise SelectionRejected('That title is a %s, not a %s.'
                                    % (item.media_type, key[1]))
        lookup[encode_key(key)] = item
    return lookup


def _selection_for(lookup, record):
    """The user's explicit choice for this record, if any."""
    key = resolution_key(record)
    if not key:
        return None
    return lookup.get(encode_key(key))


def _resolve(record, index, mapping_set, lookup, is_episode):
    """Resolution with mapping and explicit choice ahead of inference."""
    selection = _selection_for(lookup, record)
    if is_episode:
        return resolve_episode(record, index, mapping_set, selection)
    return resolve_movie(record, index, mapping_set, selection)


def _candidate_payloads(index, record, is_episode, limit=CANDIDATES_IN_PREVIEW):
    """Deterministic local candidates for a record that did not resolve."""
    if is_episode:
        return [media_payload(m) for m in
                candidates_for(index, record.show_title, 'tv', limit=limit)]
    return [media_payload(m) for m in
            candidates_for(index, record.title, 'movie',
                           record.release_year, limit=limit)]


def _needs_choice(resolution):
    return resolution.status in (AMBIGUOUS, UNRESOLVED)


def preview(user_id, source, filename, payload, details_loader=None,
            selections=None):
    """Classify an import WITHOUT writing anything.

    Writes no rows — including no mappings. Supplied ``selections`` are applied
    to the classification so the preview reflects what apply would do, but they
    are not persisted here; :func:`apply_import` owns every write.

    Unresolved and ambiguous records carry a bounded list of local candidates so
    the user can resolve them without leaving the page.
    """
    movies, episodes, invalid = parse_source(source, filename, payload)
    index = MediaIndex()
    mapping_set = load_mapping_set(user_id, source)
    lookup = build_selection_lookup(source, movies, episodes, selections)

    report = _blank_report()
    details = {
        'source': source,
        'filename': filename,
        'records_detected': len(movies) + len(episodes),
        'movies': [], 'episodes': [], 'invalid': [],
        'mappings_applied': len(mapping_set),
        'selections_applied': len(lookup),
    }

    for row in invalid:
        report[INVALID] += 1
        details['invalid'].append({
            'position': row.position,
            'reason': row.reason,
            'excerpt': row.excerpt,
        })

    _preview_movies(user_id, movies, index, mapping_set, lookup, report,
                    details)
    _preview_episodes(user_id, episodes, index, mapping_set, lookup, report,
                      details, details_loader)

    report['details'] = details
    return report


def _preview_movies(user_id, movies, index, mapping_set, lookup, report,
                    details):
    """Classify movie rows. Batched: one query for present-checks, one for reviews."""
    resolved = []
    for record in movies:
        resolution = _resolve(record, index, mapping_set, lookup, False)
        if _needs_choice(resolution):
            report[resolution.status] += 1
            entry = _movie_detail(record, resolution.status, resolution.detail)
            entry['resolution_key'] = encode_key(resolution_key(record))
            entry['candidates'] = _candidate_payloads(index, record, False)
            details['movies'].append(entry)
            continue
        if resolution.is_resolved:
            resolved.append((record, resolution.media_item))
            continue
        # Not resolvable and not choice-able (defensive: every non-resolved
        # status above is choice-able, so this is unreachable in practice).
        report[UNRESOLVED] += 1
        details['movies'].append(
            _movie_detail(record, UNRESOLVED, resolution.detail))

    if not resolved:
        return

    present = existing_movie_watch_keys(
        user_id, [item.id for _, item in resolved])
    reviewed = existing_review_keys(user_id, [item.id for _, item in resolved])
    for record, media_item in resolved:
        detail = _movie_preview_status(record, media_item, present,
                                       reviewed, report)
        entry = _movie_detail(record, detail.pop('status'), detail.pop('reason'))
        if detail.get('review_status'):
            entry['review_status'] = detail['review_status']
        details['movies'].append(entry)


def _movie_preview_status(record, media_item, present, reviewed, report):
    """Classify one resolved movie row; returns status + optional review note."""
    review_status = None
    if record.review_text:
        # A review is imported independently of the watch event, so it is
        # classified on its own key (Review), not the diary key.
        if media_item.id in reviewed:
            review_status = CONFLICT
            report[CONFLICT] += 1
        elif record.rating is None:
            review_status = UNSUPPORTED
            report[UNSUPPORTED] += 1
        else:
            review_status = IMPORTED
            report[IMPORTED] += 1

    if record.watched_at is None:
        # Without a date the row cannot be made idempotent, and guessing today
        # would fabricate history. The REVIEW may still import on its own.
        report[UNSUPPORTED] += 1
        return {'status': UNSUPPORTED,
                'reason': 'row has no watch date',
                'review_status': review_status}

    key = (media_item.id, record.watched_at.isoformat())
    if key in present:
        report[ALREADY_PRESENT] += 1
        status = ALREADY_PRESENT
    else:
        present.add(key)
        report[IMPORTED] += 1
        status = IMPORTED
    return {'status': status, 'reason': None, 'review_status': review_status}


def _preview_episodes(user_id, episodes, index, mapping_set, lookup, report,
                      details, details_loader):
    """Classify episode rows, honouring F4's eligibility gate."""
    resolved = []
    for record in episodes:
        resolution = _resolve(record, index, mapping_set, lookup, True)
        if _needs_choice(resolution):
            report[resolution.status] += 1
            entry = _episode_detail(record, resolution.status, resolution.detail)
            entry['resolution_key'] = encode_key(resolution_key(record))
            entry['candidates'] = _candidate_payloads(index, record, True)
            details['episodes'].append(entry)
            continue
        if resolution.is_resolved:
            resolved.append((record, resolution.media_item))
            continue
        report[UNRESOLVED] += 1
        details['episodes'].append(
            _episode_detail(record, UNRESOLVED, resolution.detail))

    if not resolved:
        return

    present = existing_episode_keys(
        user_id, [item.tmdb_id for _, item in resolved])
    for record, media_item in resolved:
        key = (media_item.tmdb_id, record.season_number,
               record.episode_number)
        if key in present:
            report[ALREADY_PRESENT] += 1
            status = ALREADY_PRESENT
        else:
            reason = preflight_episode(media_item.tmdb_id,
                                       record.season_number,
                                       record.episode_number,
                                       details_loader=details_loader)
            if reason:
                # Future episodes and ineligible specials never become watched
                # history, and the user is told before confirming.
                report[INELIGIBLE] += 1
                status, detail = INELIGIBLE, reason
            else:
                present.add(key)
                report[IMPORTED] += 1
                status, detail = IMPORTED, None
        details['episodes'].append(_episode_detail(record, status, detail))


def _movie_detail(record: MovieImportRecord, status, detail=None):
    return {
        'status': status,
        'title': record.title,
        'release_year': record.release_year,
        'external_id': record.external_id,
        'watched_at': record.watched_at.isoformat()
        if record.watched_at else None,
        'rating': record.rating,
        'source_key': list(record.source_key),
        'has_review': bool(record.review_text),
        'detail': detail,
    }


def _episode_detail(record: TVEpisodeImportRecord, status, detail=None):
    return {
        'status': status,
        'show_title': record.show_title,
        'season_number': record.season_number,
        'episode_number': record.episode_number,
        'episode_title': record.episode_title,
        'watched_at': record.watched_at.isoformat()
        if record.watched_at else None,
        'rating': record.rating,
        'source_key': list(record.source_key),
        'detail': detail,
    }


def apply_import(user_id, source, filename, payload, details_loader=None,
                 selections=None, save_mappings=False):
    """Write the importable rows through the canonical paths.

    Runs in ONE transaction: either every importable row lands or none does.
    A per-row failure is recorded and the transaction still commits, so the
    summary is always accurate about what actually happened — but a systemic
    failure (DB error) rolls the whole thing back rather than leaving a
    partial import behind.

    ``selections`` are the user's explicit per-title choices; they are
    re-validated against this file before anything is written.

    ``save_mappings`` persists a mapping for every identity that resolved. It
    defaults to OFF: freezing a heuristic match into a permanent override is a
    decision the user should make knowingly, not something an import does by
    surprise.
    """
    movies, episodes, invalid = parse_source(source, filename, payload)
    index = MediaIndex()
    mapping_set = load_mapping_set(user_id, source)
    lookup = build_selection_lookup(source, movies, episodes, selections)
    report = _blank_report()
    failures: List[Dict[str, Any]] = []
    saved = 0

    for row in invalid:
        report[INVALID] += 1

    try:
        saved += _apply_movies(user_id, movies, index, mapping_set, lookup,
                               report, failures, save_mappings, source)
        saved += _apply_episodes(user_id, episodes, index, mapping_set, lookup,
                                 report, failures, details_loader,
                                 save_mappings, source)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Import failed for user %s from %s", user_id, source)
        raise

    report['failures'] = failures
    report['mappings_saved'] = saved
    # F6 set `details` only in preview, so summary_payload always reported
    # records_detected as 0 for an apply — the client showed "imported N" with
    # no denominator. Apply has no per-row samples by design (the rows are
    # written now), but the denominator is needed for the confirmation text.
    report['details'] = {
        'records_detected': len(movies) + len(episodes),
        'mappings_applied': len(mapping_set),
        'selections_applied': len(lookup),
    }
    return report


def _persist_mapping(user_id, source, record, media_item):
    """Save one identity -> title mapping. Never fatal to the import."""
    key = resolution_key(record)
    if key is None:
        return 0
    title = getattr(record, 'show_title', None) or record.title
    try:
        save_mapping(user_id, source, key[1], key[2], media_item.id,
                     source_title=title)
    except MappingError as exc:
        # A mapping that cannot be stored must not roll back the user's watch
        # history; the rows are already written and are worth keeping.
        logger.warning("Could not save mapping for %s: %s", key, exc)
        return 0
    return 1


def _apply_movies(user_id, movies, index, mapping_set, lookup, report, failures,
                  save_mappings, source):
    resolved = []
    for record in movies:
        resolution = _resolve(record, index, mapping_set, lookup, False)
        if not resolution.is_resolved:
            report[AMBIGUOUS if resolution.status == AMBIGUOUS
                   else UNRESOLVED] += 1
            continue
        resolved.append((record, resolution.media_item))
    if not resolved:
        return 0

    saved = 0
    present = existing_movie_watch_keys(
        user_id, [m.id for _, m in resolved])
    reviewed = existing_review_keys(user_id, [m.id for _, m in resolved])
    for record, media_item in resolved:
        if save_mappings:
            saved += _persist_mapping(user_id, source, record, media_item)

        if record.watched_at is not None:
            key = (media_item.id, record.watched_at.isoformat())
            if key in present:
                report[ALREADY_PRESENT] += 1
            else:
                try:
                    result = write_movie_watch(user_id, media_item,
                                               record.watched_at,
                                               rating=record.rating)
                except Exception as exc:  # noqa: BLE001
                    failures.append({'source_key': list(record.source_key),
                                     'reason': str(exc)[:200]})
                    continue
                present.add(key)
                report[result.status] += 1
        else:
            report[UNSUPPORTED] += 1

        saved += _apply_review(user_id, record, media_item, reviewed, report,
                               failures)
    return saved


def _apply_review(user_id, record, media_item, reviewed, report, failures):
    """Import one review body. Conflicts are reported, never resolved."""
    if not record.review_text:
        return 0
    if media_item.id in reviewed:
        report[CONFLICT] += 1
        return 0
    try:
        result = write_movie_review(user_id, media_item, record.review_text,
                                    record.rating, record.watched_at)
    except Exception as exc:  # noqa: BLE001
        failures.append({'source_key': list(record.source_key),
                         'reason': str(exc)[:200]})
        return 0
    if result.status == 'imported':
        reviewed.add(media_item.id)
    elif result.status == 'conflict':
        reviewed.add(media_item.id)
    report[result.status if result.status in report else UNSUPPORTED] += 1
    return 0


def _apply_episodes(user_id, episodes, index, mapping_set, lookup, report,
                    failures, details_loader, save_mappings, source):
    resolved = []
    for record in episodes:
        resolution = _resolve(record, index, mapping_set, lookup, True)
        if not resolution.is_resolved:
            report[AMBIGUOUS if resolution.status == AMBIGUOUS
                   else UNRESOLVED] += 1
            continue
        resolved.append((record, resolution.media_item))
    if not resolved:
        return 0

    saved = 0
    # A show identity that maps is saved ONCE, not once per episode.
    saved_keys = set()
    present = existing_episode_keys(
        user_id, [m.tmdb_id for _, m in resolved])
    for record, media_item in resolved:
        if save_mappings:
            key = resolution_key(record)
            marker = encode_key(key) if key else None
            if marker not in saved_keys:
                saved += _persist_mapping(user_id, source, record, media_item)
                saved_keys.add(marker)

        status = _write_one_episode(user_id, record, media_item, present,
                                    report, failures, details_loader)
        if status is not None:
            present.add(status)
    return saved


def _write_one_episode(user_id, record, media_item, present, report, failures,
                       details_loader):
    """Write one episode through the canonical path.

    Returns the ``(show, season, episode)`` key on success so the caller can
    suppress a later duplicate row, or ``None`` when nothing was written.
    """
    show_id = media_item.tmdb_id
    key = (show_id, record.season_number, record.episode_number)
    if key in present:
        report[ALREADY_PRESENT] += 1
        return None

    reason = preflight_episode(show_id, record.season_number,
                               record.episode_number,
                               details_loader=details_loader)
    if reason:
        # F4's gate: a future or ineligible-special episode is never history.
        report[INELIGIBLE] += 1
        return None

    try:
        # THE canonical write path. Never insert TVEpisodeWatch here.
        write_episode_watch(
            user_id, show_id, record.season_number,
            record.episode_number, record.watched_at,
            rating=record.rating,
            episode_name=record.episode_title,
            is_rewatch=record.is_rewatch)
    except Exception as exc:  # noqa: BLE001
        # The canonical path calls db.session.rollback() before raising
        # EpisodeNotAired, which discards nothing here because nothing was
        # written yet for this record.
        if getattr(exc, 'reason', None):
            report[INELIGIBLE] += 1
        else:
            failures.append({'source_key': list(record.source_key),
                             'reason': str(exc)[:200]})
        return None
    report[IMPORTED] += 1
    return key


def summary_payload(report):
    """The report as sent to the client — counts plus bounded detail.

    Detail samples are capped so a 10,000-row import cannot produce an
    unbounded response body (and therefore an unbounded page render).
    """
    detail = report.get('details', {})
    out = {key: value for key, value in report.items() if key != 'details'}
    out['records_detected'] = detail.get('records_detected', 0)
    out['failures'] = report.get('failures', [])[:50]
    out['samples'] = {
        'movies': detail.get('movies', [])[:100],
        'episodes': detail.get('episodes', [])[:100],
        'invalid': detail.get('invalid', [])[:100],
    }
    return out


MAX_DETAIL_ROWS = 100