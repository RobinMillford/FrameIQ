#!/usr/bin/env python3
"""Batch cast enrichment for existing MediaItems (Feature F9).

The ONLY component allowed to contact TMDb for cast data. It moves remote
title-level credits into durable local state (models/cast.py) so that:

  - api/statistics.py can populate its ``actors`` series from LOCAL rows
  - api/taste_profile.py can compute actor affinity from LOCAL rows
  - no request path ever performs a credits lookup

Bounded by design: at most MAX_MEDIA_ITEMS titles per run (overridable with
``--limit``), one details call each, through the existing cached/retrying TMDb
layer (api.tmdb.cache). Restartable: titles already carrying
``cast_enriched_at`` are skipped, so re-running continues where the last run
stopped.

Idempotency: a re-run issues NO external call for an already-enriched title,
never duplicates Person rows (stable tmdb_person_id key), never duplicates
(media, person) associations, and updates character/order in place when TMDb
re-orders a billing list.

Empty-vs-failed semantics: a title processed successfully with zero cast members
still sets ``cast_enriched_at`` — "enriched and empty" stays distinguishable
from "not yet enriched" (NULL). A FAILED title is NOT marked, so it is
selected again on the next run.

Credits are upserted, never pruned. The credits payload is a truncated top-N
billing list; a transient short response must not be able to delete rows that
are still correct, so re-enrichment only ever adds and updates.

Movies AND TV: see models/cast.py for why TV *cast* is series-level evidence
while TV *crew* is episode-aggregated and therefore refused for directors. This
script reads only the title-level ``cast`` array from a title-level details
call and never requests per-episode credits, so no episode guest star can become
a series credit.

Exits: 0 = all selected titles enriched · 1 = one or more title failures ·
2 = fatal startup/infrastructure error.

Run: python scripts/enrich_cast.py [--limit N] [--media-type movie|tv|both]
                               [--dry-run]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_MEDIA_ITEMS = 100        # hard default per-run bound
# Hard CEILING, not just a default. scripts/enrich_directors.py treats 100 as a
# hard bound with no override at all; F9 allows smaller batches for operator
# convenience but must not let a mistyped --limit turn one run into an
# unbounded TMDb crawl. 10x the default is generous and still finite.
MAX_MEDIA_ITEMS_CEILING = 1000
# The title-level details fetchers already truncate their cast list to this
# length (api/tmdb/movies.py, api/tmdb/tv.py). Persisting more would mean
# issuing a different request than the one the rest of the app uses.
MAX_CAST_PER_TITLE = 30
# Courtesy gap between titles. The cached TMDb layer already rate-limits
# concurrent work; this bounds request *rate* for a long sequential batch.
# Validated non-negative before any work starts: time.sleep() raises on a
# negative value, which would otherwise abort a run mid-batch with a traceback.
REQUEST_SPACING_SECONDS = 0.25
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
# 'movie', 'tv' or 'both'. Both is the default: cast enrichment is defined for
# tv as well as movie (models/cast.py).
DEFAULT_MEDIA_TYPE = 'both'
DRY_RUN = os.environ.get('ENRICH_DRY_RUN') == '1'  # operators can preview

_RUNS = {}


def _load():
    """Import the app exactly once (same lazy pattern as the other
    scripts) so module-level constants exist before selection runs."""
    if _RUNS:
        return
    from app import app
    from models import db
    _RUNS.update(app=app, db=db)


def _media_type_filter(media_type):
    """SQLAlchemy filter for the requested scope, or None for 'both'."""
    from models import MediaItem
    if media_type in (None, 'both'):
        return None
    return MediaItem.media_type == media_type


def _select_pending(db, limit, media_type=DEFAULT_MEDIA_TYPE):
    """Bounded, stable-order selection of titles not yet cast-enriched.

    Ordered by MediaItem.id so a run is reproducible and a resumed run picks up
    from the same deterministic frontier rather than an arbitrary one.
    """
    from models import MediaItem
    query = MediaItem.query.filter(MediaItem.cast_enriched_at.is_(None))
    scope = _media_type_filter(media_type)
    if scope is not None:
        query = query.filter(scope)
    return query.order_by(MediaItem.id).limit(limit).all()


def count_pending(db, media_type=DEFAULT_MEDIA_TYPE):
    from models import MediaItem
    query = MediaItem.query.filter(MediaItem.cast_enriched_at.is_(None))
    scope = _media_type_filter(media_type)
    if scope is not None:
        query = query.filter(scope)
    return query.count()


def _title_level_cast(payload):
    """The title-level ``cast`` list from a details payload.

    Two accepted shapes, and the reason for the second one matters:

    * ``{'cast': [...]}`` — what the project's own fetchers return. Both
      api.tmdb.movies.fetch_movie_details and api.tmdb.tv.fetch_tv_show_details
      build a presentation dict whose ``cast`` is the truncated billing list.
    * ``{'credits': {'cast': [...]}}`` — a raw TMDb body, accepted so the
      function does not silently return nothing if it is ever handed one.

    The second shape is a *title-level* ``credits.cast``, which is the same
    series-level evidence for TV. Neither shape is per-episode credits: this
    function never looks at season or episode payloads, and the fetchers never
    request them.

    Returns a list of ``(tmdb_person_id, name, character, profile_url)`` in the
    payload's own order, de-duplicated by person id keeping the FIRST billing
    position. That is what makes credit_order meaningful and what keeps a dual
    role from occupying two rows.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get('cast')
    if entries is None:
        entries = (payload.get('credits') or {}).get('cast')
    if not isinstance(entries, list):
        return []

    seen, ordered = set(), []
    for position, member in enumerate(entries[:MAX_CAST_PER_TITLE]):
        if not isinstance(member, dict):
            continue
        raw_id = member.get('id')
        name = (member.get('name') or '').strip()
        if raw_id is None or not name:
            continue
        try:
            person_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if person_id in seen:
            continue  # dual role: keep the higher-billed character
        seen.add(person_id)
        character = (member.get('character') or '').strip() or None
        profile_url = (member.get('profile_path') or '').strip() or None
        ordered.append((person_id, name, character, profile_url))
    return ordered


def _upsert_person(db, Person, person_id, name, profile_url):
    """Get-or-create by STABLE person id; display-name changes UPDATE the
    existing row instead of forking a duplicate person."""
    person = Person.query.filter_by(tmdb_person_id=person_id).first()
    if person is None:
        person = Person(tmdb_person_id=person_id, name=name, source='tmdb',
                        profile_url=profile_url)
        db.session.add(person)
        db.session.flush()  # assign PK before association insert
    else:
        if person.name != name:
            person.name = name
        # Only refresh the URL when the new payload carries one: a payload that
        # omits profile_path must not erase a URL we already hold.
        if profile_url and person.profile_url != profile_url:
            person.profile_url = profile_url
    return person


def _associate(db, MediaCast, media_item, person, character, credit_order):
    """Unique (media, person) pair — never duplicated on re-runs.

    Character and billing order are updated in place so a TMDb re-order is
    reflected without creating a second row.
    """
    existing = MediaCast.query.filter_by(
        media_item_id=media_item.id, person_id=person.id).first()
    if existing is not None:
        if existing.credit_order != credit_order:
            existing.credit_order = credit_order
        if character and existing.character != character:
            existing.character = character
        return existing, False
    assoc = MediaCast(media_item_id=media_item.id, person_id=person.id,
                      character=character, credit_order=credit_order)
    db.session.add(assoc)
    return assoc, True


def _fetch_with_backoff(fetch_details, tmdb_id, attempts, base_delay, sleep=time.sleep):
    """Call ``fetch_details``, retrying transient failures with linear backoff.

    ``sleep`` is injectable so tests exercise the retry path without spending
    real seconds. A missing title is NOT retried: the underlying fetchers raise
    LookupError for "not found", and repeating that just burns quota.
    """
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fetch_details(tmdb_id)
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            last = exc
            if attempt >= max(1, attempts):
                break
            if base_delay:
                sleep(base_delay * attempt)
    raise last


def enrich_media_item(db, models, media_item, fetch_details, now,
                      attempts=DEFAULT_RETRY_ATTEMPTS,
                      base_delay=DEFAULT_RETRY_BASE_DELAY,
                      sleep=time.sleep):
    """Enrich ONE MediaItem. Returns 'ok' | 'empty' | raises.

    Atomic per item: either the associations + enriched marker commit
    together, or nothing is written and the item stays retryable. The marker is
    written ONLY on success, so a failure is never mistaken for "enriched and
    empty".
    """
    Person, MediaCast = models
    data = _fetch_with_backoff(fetch_details, media_item.tmdb_id, attempts,
                               base_delay, sleep=sleep)
    cast = _title_level_cast(data)

    for credit_order, (person_id, name, character, profile_url) \
            in enumerate(cast):
        person = _upsert_person(db, Person, person_id, name, profile_url)
        _associate(db, MediaCast, media_item, person, character, credit_order)

    media_item.cast_enriched_at = now
    db.session.commit()
    return 'empty' if not cast else 'ok'


def _fetcher_for(media_type):
    """The title-level details fetcher for a media type.

    Resolved lazily and by name so tests can monkeypatch the module attribute
    the fetchers are imported under.
    """
    if media_type == 'tv':
        from api.tmdb.tv import fetch_tv_show_details
        return fetch_tv_show_details
    from api.tmdb.movies import fetch_movie_details
    return fetch_movie_details


def validate_options(limit, spacing):
    """Reject out-of-range inputs BEFORE any selection or network call.

    Returns ``(limit, spacing)`` normalised. Raises ValueError with an
    operator-readable message so a bad flag aborts the run at argument-parse
    time rather than half way through a batch.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError('--limit must be an integer, got %r' % (limit,))
    if limit < 1:
        raise ValueError('--limit must be at least 1, got %d' % limit)
    if limit > MAX_MEDIA_ITEMS_CEILING:
        raise ValueError(
            '--limit %d exceeds the hard per-run ceiling of %d. One run must '
            'never become an unbounded TMDb crawl; run the script again to '
            'continue from where it stopped.'
            % (limit, MAX_MEDIA_ITEMS_CEILING))

    try:
        spacing = float(spacing)
    except (TypeError, ValueError):
        raise ValueError('--spacing must be a number, got %r' % (spacing,))
    if spacing < 0:
        raise ValueError('--spacing must be zero or greater, got %s' % spacing)
    return limit, spacing


def run(limit=MAX_MEDIA_ITEMS, media_type=DEFAULT_MEDIA_TYPE, dry_run=None,
        attempts=DEFAULT_RETRY_ATTEMPTS, base_delay=DEFAULT_RETRY_BASE_DELAY,
        spacing=REQUEST_SPACING_SECONDS):
    """One bounded, resumable enrichment pass. Returns a process exit code."""
    limit, spacing = validate_options(limit, spacing)
    if media_type not in ('movie', 'tv', 'both'):
        raise ValueError("media_type must be 'movie', 'tv' or 'both', got %r"
                         % (media_type,))
    _load()
    app, db = _RUNS['app'], _RUNS['db']
    from datetime import datetime

    if dry_run is None:
        dry_run = DRY_RUN

    print("[START] Cast enrichment (limit=%s scope=%s%s)"
          % (limit, media_type, ' DRY-RUN' if dry_run else ''))
    with app.app_context():
        from models import MediaCast, Person

        batch = _select_pending(db, limit, media_type)
        pending_total = count_pending(db, media_type)
        print("[INFO] titles selected this run: %d (cap %s; %d remain for "
              "later runs)" % (len(batch), limit,
                               max(0, pending_total - len(batch))))
        if dry_run:
            print("[INFO] dry-run — selection preview only, no TMDb calls, "
                  "no writes.")
            for m in batch:
                print("[DRY] id=%s tmdb=%s %s %s"
                      % (m.id, m.tmdb_id, m.media_type, m.title))
            print("[DONE] dry-run complete ok=0 failed=0")
            return 0

        ok = failed = empty = 0
        started = time.time()
        for position, m in enumerate(batch):
            if position and spacing:
                time.sleep(spacing)
            try:
                outcome = enrich_media_item(
                    db, (Person, MediaCast), m, _fetcher_for(m.media_type),
                    datetime.utcnow(), attempts=attempts, base_delay=base_delay)
                if outcome == 'empty':
                    empty += 1
                    print("[OK] id=%s tmdb=%s %s '%s': no cast credited"
                          % (m.id, m.tmdb_id, m.media_type, m.title))
                else:
                    ok += 1
            except Exception as exc:  # noqa: BLE001 — isolation per item
                db.session.rollback()
                failed += 1
                print("[FAIL] id=%s tmdb=%s %s '%s': %s: %s"
                      % (m.id, m.tmdb_id, m.media_type, m.title,
                         type(exc).__name__, exc))

        n_assocs = MediaCast.query.count()
        print("[DONE] enriched=%d no_cast=%d failed=%d associations_total=%d "
              "people_total=%d elapsed=%.1fs"
              % (ok, empty, failed, n_assocs, Person.query.count(),
                 time.time() - started))
        print("[STATUS] " + ("OK" if failed == 0 else "FAILURES"))
        return 0 if failed == 0 else 1


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='enrich_cast.py',
        description='Resumable batch cast enrichment. Runs OFFLINE against '
                    'the local database; never invoked by the deploy workflow.')
    parser.add_argument('--limit', type=int, default=MAX_MEDIA_ITEMS,
                        help='max titles this run (default %d, hard ceiling '
                             '%d; a run is never unbounded)'
                             % (MAX_MEDIA_ITEMS, MAX_MEDIA_ITEMS_CEILING))
    parser.add_argument('--media-type', choices=('movie', 'tv', 'both'),
                        default=DEFAULT_MEDIA_TYPE,
                        help='which titles to enrich (default %s)'
                             % DEFAULT_MEDIA_TYPE)
    parser.add_argument('--dry-run', action='store_true',
                        help='print the selection and exit without any TMDb '
                             'call or write')
    parser.add_argument('--retry-attempts', type=int,
                        default=DEFAULT_RETRY_ATTEMPTS,
                        help='attempts per title (default %d)'
                             % DEFAULT_RETRY_ATTEMPTS)
    parser.add_argument('--retry-base-delay', type=float,
                        default=DEFAULT_RETRY_BASE_DELAY,
                        help='linear backoff base in seconds (default %s)'
                             % DEFAULT_RETRY_BASE_DELAY)
    parser.add_argument('--spacing', type=float,
                        default=REQUEST_SPACING_SECONDS,
                        help='courtesy gap between titles in seconds '
                             '(default %s)' % REQUEST_SPACING_SECONDS)
    return parser.parse_args(argv)


if __name__ == '__main__':
    try:
        _options = _parse_args(sys.argv[1:])
        sys.exit(run(limit=_options.limit,
                     media_type=_options.media_type,
                     dry_run=_options.dry_run or None,
                     attempts=_options.retry_attempts,
                     base_delay=_options.retry_base_delay,
                     spacing=_options.spacing))
    except ValueError as exc:
        # Bad operator input is not a crash: print it and exit cleanly rather
        # than dumping a traceback. Raised by validate_options() before any
        # selection, network call or write happens.
        print('[FATAL] %s' % exc)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 — fatal infrastructure error
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        sys.exit(2)