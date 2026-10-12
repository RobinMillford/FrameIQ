#!/usr/bin/env python3
"""Batch director enrichment for existing MediaItems (Feature #6, Phase 10).

The ONLY component allowed to contact TMDb for director data. It moves
remote credits into durable local state (models/director.py) so that:

  - api/taste_profile.py computes director_affinity from LOCAL rows only
  - api/for_you.py tags candidates with director names locally
  - no request path ever performs a credits lookup

Bounded by design (spec §7): at most MAX_MEDIA_ITEMS titles per run, one
credits-bearing details call each, through the existing cached/retrying
TMDb layer (api.tmdb.cache). Restartable: enriched titles are skipped, so
re-running continues where the last run stopped.

Idempotency (spec §12): a re-run issues NO external call for an already-
enriched title, never duplicates Director rows (stable tmdb_person_id
key), and never duplicates (media, director) associations. A FAILED title
is NOT marked enriched — it remains selected on the next run.

No-director semantics (spec §13): a title processed successfully but with
zero director credits sets media_item.directors_enriched_at with zero
associations — "enriched and empty" stays distinguishable from "not yet
enriched" (NULL).

TV semantics (spec §10): TMDb's TV crew credits aggregate EPISODE-level
directing; TMDb has no reliable series-level director concept. Enriching
TV would fabricate evidence, so only MOVIES are processed and TV is
left absent (documented, deliberate).

Exits: 0 = all selected items enriched · 1 = one or more item failures ·
2 = fatal startup/infrastructure error. Operator logging uses the
[START]/[INFO]/[OK]/[SKIP]/[FAIL]/[DONE] style of the other scripts.

Run: python scripts/enrich_directors.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_MEDIA_ITEMS = 100        # hard per-run bound (spec §7) — documented
DIRECTOR_JOB = 'Director'    # TMDb credits.crew job value for directors
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


def _select_pending(db, limit):
    """Bounded, stable-order selection of movies not yet enriched."""
    from models import MediaItem
    return (
        MediaItem.query.filter(
            MediaItem.media_type == 'movie',
            MediaItem.directors_enriched_at.is_(None),
        )
        .order_by(MediaItem.id)
        .limit(limit)
        .all()
    )


class DirectorContractError(RuntimeError):
    """The details response did not carry usable director credits.

    Feature F16. Raised instead of silently reporting "no director credited",
    because an unreadable response and a genuinely uncredited film are very
    different facts and only one of them may mark an item enriched.
    """


def _extract_directors(details_payload):
    """details payload → [(tmdb_person_id, name), ...] for DIRECTORS only.

    Multiple directors supported (spec §9). Actors/writers/producers and
    every other crew role are filtered out here — nothing else is ever
    persisted (spec §8). Deterministic order: by person id.

    TWO shapes are accepted, in priority order:

    1. **Normalized** (what ``api.tmdb.movies.fetch_movie_details`` actually
       returns): ``{'directors': [{'id': .., 'name': ..}, ...]}``. This is the
       contract the enrichment pipeline really consumes.
    2. **Raw TMDb** (kept for compatibility with existing callers and test
       fixtures): ``{'credits': {'crew': [{'id', 'name', 'job'}, ...]}}``.

    Shape 2 used to be the ONLY thing understood, while the function under
    test never returned it — so every movie was marked enriched with zero
    directors persisted. Accepting both keeps the existing fixtures meaningful
    and fixes the real path.

    Raises :class:`DirectorContractError` when NEITHER key is present: that is
    an unreadable response, not an uncredited film, and the caller must not
    mark it enriched.
    """
    if not isinstance(details_payload, dict):
        raise DirectorContractError(
            'movie details response was %s, not an object'
            % type(details_payload).__name__)

    if 'directors' in details_payload:
        entries = details_payload.get('directors')
        if entries is None:
            raise DirectorContractError(
                'movie details carried directors=None (unreadable credits)')
        return _normalized_directors(entries)

    # Legacy raw TMDb shape. Only taken when `credits` is genuinely PRESENT:
    # an absent key means the response could not be read, not that the film
    # is uncredited, so it must not be allowed to mark the item enriched.
    if 'credits' in details_payload:
        credits = details_payload.get('credits')
        if not isinstance(credits, dict):
            raise DirectorContractError(
                'movie details carried credits=%s (unreadable)'
                % type(credits).__name__)
        return _raw_crew_directors(credits.get('crew') or [])

    raise DirectorContractError(
        'movie details carried neither "directors" nor "credits" '
        '(present keys: %s)' % sorted(details_payload))


def _normalized_directors(entries):
    """``[{'id': .., 'name': ..}]`` → sorted [(person_id, name)].

    Anything without a usable identity or a non-blank name is DROPPED — the
    enrichment never invents a person.
    """
    found = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            person_id = int(entry.get('id'))
        except (TypeError, ValueError):
            continue            # no usable identity -> never fabricate one
        name = (entry.get('name') or '').strip()
        if name:
            found[person_id] = name
    return sorted(found.items())          # deterministic: by person id


def _raw_crew_directors(crew):
    """Legacy raw TMDb crew list → sorted [(person_id, name)].

    Coerces ids exactly like the normalized path and skips entries it cannot
    read, rather than letting a bare ``int()`` raise: a malformed legacy
    payload is bad input, not a database fault, and the two shapes must not
    diverge in how they behave.
    """
    found = {}
    for entry in crew:
        if not isinstance(entry, dict):
            continue
        if entry.get('job') != DIRECTOR_JOB:
            continue
        try:
            person_id = int(entry.get('id'))
        except (TypeError, ValueError):
            continue            # no usable identity -> never fabricate one
        name = (entry.get('name') or '').strip()
        if name:
            found[person_id] = name
    return sorted(found.items())  # [(person_id, name)] sorted by id


def _upsert_director(db, Director, person_id, name):
    """Get-or-create by STABLE person id; display-name changes UPDATE the
    existing row instead of forking a duplicate person (spec §3)."""
    director = Director.query.filter_by(tmdb_person_id=person_id).first()
    if director is None:
        director = Director(tmdb_person_id=person_id, name=name, source='tmdb')
        db.session.add(director)
        db.session.flush()  # assign PK before association insert
    elif director.name != name:
        director.name = name
    return director


def _associate(db, MediaDirector, media_item, director):
    """Unique (media, director) pair — never duplicated on re-runs."""
    existing = MediaDirector.query.filter_by(
        media_item_id=media_item.id, director_id=director.id).first()
    if existing is not None:
        return existing, False
    assoc = MediaDirector(media_item_id=media_item.id, director_id=director.id)
    db.session.add(assoc)
    return assoc, True


def enrich_media_item(db, models, media_item, fetch_details, now):
    """Enrich ONE MediaItem. Returns 'ok' | 'empty' | raises.

    Atomic per item: either the associations + enriched marker commit
    together, or nothing is written and the item stays retryable.

    Feature F16: an unreadable details response raises
    :class:`DirectorContractError` BEFORE the marker is written, so a
    contract failure can never be recorded as a successful enrichment (which
    is what silently produced zero-director rows before). A genuinely
    uncredited film still returns 'empty' and IS marked — "enriched and
    empty" stays distinguishable from "not yet enriched" (spec §13).
    """
    Director, MediaDirector = models
    data = fetch_details(media_item.tmdb_id)
    directors = _extract_directors(data)
    for person_id, name in directors:
        director = _upsert_director(db, Director, person_id, name)
        _associate(db, MediaDirector, media_item, director)
    media_item.directors_enriched_at = now
    db.session.commit()
    return 'empty' if not directors else 'ok'


def run():
    _load()
    app, db = _RUNS['app'], _RUNS['db']
    from datetime import datetime
    from models import Director, MediaDirector, MediaItem
    from api.tmdb.movies import fetch_movie_details

    print("[START] Director enrichment")
    with app.app_context():
        batch = _select_pending(db, MAX_MEDIA_ITEMS)
        pending_total = MediaItem.query.filter(
            MediaItem.media_type == 'movie',
            MediaItem.directors_enriched_at.is_(None),
        ).count()
        print(f"[INFO] Movies selected this run: {len(batch)} "
              f"(cap {MAX_MEDIA_ITEMS}; {max(0, pending_total - len(batch))} "
              f"remain for later runs)")
        if DRY_RUN:
            print("[INFO] ENRICH_DRY_RUN=1 — selection preview only, "
                  "no TMDb calls, no writes.")
            for m in batch:
                print(f"[DRY] id={m.id} tmdb={m.tmdb_id} {m.title}")
            print("[DONE] dry-run complete ok=0 failed=0")
            return 0

        ok = failed = empty = 0
        started = time.time()
        for m in batch:
            try:
                outcome = enrich_media_item(
                    db, (Director, MediaDirector), m, fetch_movie_details,
                    datetime.utcnow())
                if outcome == 'empty':
                    empty += 1
                    print(f"[OK] id={m.id} tmdb={m.tmdb_id} "
                          f"'{m.title}': no director credited")
                else:
                    ok += 1
            except Exception as exc:  # noqa: BLE001 — isolation per item
                db.session.rollback()
                failed += 1
                print(f"[FAIL] id={m.id} tmdb={m.tmdb_id} '{m.title}': "
                      f"{type(exc).__name__}: {exc}")

        n_assocs = MediaDirector.query.count()
        print(f"[DONE] enriched={ok} no_director={empty} failed={failed} "
              f"associations_total={n_assocs} "
              f"elapsed={time.time() - started:.1f}s")
        print("[STATUS] " + ("OK" if failed == 0 else "FAILURES"))
        return 0 if failed == 0 else 1


if __name__ == '__main__':
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001 — fatal infrastructure error
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        sys.exit(2)
