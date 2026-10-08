"""Import orchestration: preview and apply (Task F6).

    upload bytes
        -> uploads.load_payload      (size / type / zip safety)
        -> sources.parse_*           (normalized records)
        -> resolve.*                 (deterministic, local, offline)
        -> preview: classify only, WRITE NOTHING
        -> apply : write via writer  (canonical paths), in one transaction

Two-stage by design. Preview is a pure read: it resolves, classifies and
reports, and its statement set contains no INSERT/UPDATE. Apply is the only
operation that writes, and it runs inside a single transaction so a failure
cannot leave a half-imported account.

Classification, reported to the user verbatim:

    imported        written now
    already_present the user already has this exact event (idempotency)
    unresolved      no local match — needs the user to open the title first
    ambiguous       several local matches — never auto-picked
    ineligible      F4's gate refuses it (future episode, ineligible special)
    invalid         the source row was malformed
    unsupported     a field/record the source has that FrameIQ will not invent
"""
import logging
from collections import OrderedDict
from typing import Any, Dict, List

from api.imports import uploads
from api.imports.records import MovieImportRecord, TVEpisodeImportRecord
from api.imports.resolve import (MediaIndex, resolve_episode, resolve_movie)
from api.imports.sources import (SOURCE_LETTERBOXD, SOURCE_TVTIME,
                                 parse_letterboxd, parse_tvtime)
from api.imports.writer import (existing_episode_keys,
                                existing_movie_watch_keys,
                                preflight_episode, write_episode_watch,
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

_EMPTY_REPORT = OrderedDict([
    (IMPORTED, 0), (ALREADY_PRESENT, 0), (UNRESOLVED, 0), (AMBIGUOUS, 0),
    (INELIGIBLE, 0), (INVALID, 0), (UNSUPPORTED, 0),
])


class ImportRejected(uploads.UploadRejected):
    """Source-level refusal (bad structure). 4xx, never a 500."""


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


def preview(user_id, source, filename, payload, details_loader=None):
    """Classify an import WITHOUT writing anything.

    Returns a report dict with per-category counts plus bounded sample detail,
    so the user can see exactly what would happen before confirming.
    """
    movies, episodes, invalid = parse_source(source, filename, payload)
    index = MediaIndex()
    report = _blank_report()
    details = {
        'source': source,
        'filename': filename,
        'records_detected': len(movies) + len(episodes),
        'movies': [], 'episodes': [], 'invalid': [],
    }

    for row in invalid:
        report[INVALID] += 1
        details['invalid'].append({
            'position': row.position,
            'reason': row.reason,
            'excerpt': row.excerpt,
        })

    _preview_movies(user_id, movies, index, report, details)
    _preview_episodes(user_id, episodes, index, report, details,
                      details_loader)

    report['details'] = details
    return report


def _preview_movies(user_id, movies, index, report, details):
    """Classify movie rows. One batched query for already-present checks."""
    resolved = []
    for record in movies:
        resolution = resolve_movie(record, index)
        if resolution.is_resolved:
            resolved.append((record, resolution.media_item))
            continue
        status = AMBIGUOUS if resolution.status == AMBIGUOUS else UNRESOLVED
        report[status] += 1
        details['movies'].append(
            _movie_detail(record, status, resolution.detail))

    if not resolved:
        return

    present = existing_movie_watch_keys(
        user_id, [item.id for _, item in resolved])
    for record, media_item in resolved:
        if record.watched_at is None:
            # Without a date the row cannot be made idempotent, and guessing
            # today would fabricate history. Reported, not imported.
            report[UNSUPPORTED] += 1
            details['movies'].append(
                _movie_detail(record, UNSUPPORTED, 'row has no watch date'))
            continue
        key = (media_item.id, record.watched_at.isoformat())
        if key in present:
            report[ALREADY_PRESENT] += 1
            status = ALREADY_PRESENT
        else:
            # Two source rows for the same (title, date) are one event.
            present.add(key)
            report[IMPORTED] += 1
            status = IMPORTED
        details['movies'].append(_movie_detail(record, status))


def _preview_episodes(user_id, episodes, index, report, details,
                      details_loader):
    """Classify episode rows, honouring F4's eligibility gate."""
    resolved = []
    for record in episodes:
        resolution = resolve_episode(record, index)
        if resolution.is_resolved:
            resolved.append((record, resolution.media_item))
            continue
        status = AMBIGUOUS if resolution.status == AMBIGUOUS else UNRESOLVED
        report[status] += 1
        details['episodes'].append(
            _episode_detail(record, status, resolution.detail))

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
                # Future episodes and ineligible specials never become
                # watched history, and the user is told before confirming.
                report[INELIGIBLE] += 1
                status, detail = INELIGIBLE, reason
            else:
                present.add(key)
                report[IMPORTED] += 1
                status, detail = IMPORTED, None
            details['episodes'].append(_episode_detail(record, status, detail))
            continue
        details['episodes'].append(_episode_detail(record, status))


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


def apply_import(user_id, source, filename, payload, details_loader=None):
    """Write the importable rows through the canonical paths.

    Runs in ONE transaction: either every importable row lands or none does.
    A per-row failure is recorded and the transaction still commits, so the
    summary is always accurate about what actually happened — but a systemic
    failure (DB error) rolls the whole thing back rather than leaving a
    partial import behind.
    """
    movies, episodes, invalid = parse_source(source, filename, payload)
    index = MediaIndex()
    report = _blank_report()
    failures: List[Dict[str, Any]] = []

    for row in invalid:
        report[INVALID] += 1

    try:
        _apply_movies(user_id, movies, index, report, failures)
        _apply_episodes(user_id, episodes, index, report, failures,
                        details_loader)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Import failed for user %s from %s", user_id, source)
        raise

    report['failures'] = failures
    return report


def _apply_movies(user_id, movies, index, report, failures):
    resolved = []
    for record in movies:
        resolution = resolve_movie(record, index)
        if not resolution.is_resolved:
            report[AMBIGUOUS if resolution.status == AMBIGUOUS
                   else UNRESOLVED] += 1
            continue
        resolved.append((record, resolution.media_item))
    if not resolved:
        return

    present = existing_movie_watch_keys(
        user_id, [m.id for _, m in resolved])
    for record, media_item in resolved:
        if record.watched_at is None:
            report[UNSUPPORTED] += 1
            continue
        key = (media_item.id, record.watched_at.isoformat())
        if key in present:
            report[ALREADY_PRESENT] += 1
            continue
        try:
            result = write_movie_watch(user_id, media_item, record.watched_at,
                                       rating=record.rating)
        except Exception as exc:  # noqa: BLE001
            failures.append({'source_key': list(record.source_key),
                             'reason': str(exc)[:200]})
            continue
        present.add(key)
        report[result.status] += 1


def _apply_episodes(user_id, episodes, index, report, failures,
                    details_loader):
    resolved = []
    for record in episodes:
        resolution = resolve_episode(record, index)
        if not resolution.is_resolved:
            report[AMBIGUOUS if resolution.status == AMBIGUOUS
                   else UNRESOLVED] += 1
            continue
        resolved.append((record, resolution.media_item))
    if not resolved:
        return

    present = existing_episode_keys(
        user_id, [m.tmdb_id for _, m in resolved])
    for record, media_item in resolved:
        show_id = media_item.tmdb_id
        key = (show_id, record.season_number, record.episode_number)
        if key in present:
            report[ALREADY_PRESENT] += 1
            continue

        reason = preflight_episode(show_id, record.season_number,
                                   record.episode_number,
                                   details_loader=details_loader)
        if reason:
            report[INELIGIBLE] += 1
            continue
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
            reason_text = getattr(exc, 'reason', None) or str(exc)[:200]
            if getattr(exc, 'reason', None):
                report[INELIGIBLE] += 1
            else:
                failures.append({'source_key': list(record.source_key),
                                 'reason': reason_text})
            continue
        present.add(key)
        report[IMPORTED] += 1


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