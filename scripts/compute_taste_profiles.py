"""Nightly TasteProfile recomputation job (Feature #6, Phase 3).

Orchestration ONLY. Every bit of computation lives in the canonical service
(api.taste_profile.compute_profile) — this script owns batching, failure
isolation, logging and exit codes, and nothing else. See that module for the
signal model; nothing here re-implements weights, decay or normalization.

Behavior contract:

  IDEMPOTENT — safe to run repeatedly/nightly. compute_profile() is
  idempotent (create-or-update, exactly one row per user), and this script
  streams users in ascending user-id order via keyset pagination, so re-runs
  converge to the same persisted profiles.

  PARTIAL-FAILURE SAFE — compute -> persist; profiles are never deleted,
  truncated, or blanked before a successful computation. A failing user's
  exception is caught, its in-flight session state is rolled back (the
  previously persisted profile stays exactly as it was), the failure is
  logged with the user id, and the job continues with the remaining users.
  Per-user rollback keeps the session clean, which is what makes it safe to
  continue after any batch containing failures.

  NO EXTERNAL CALLS — profiles are computed purely from local persisted
  data. This script adds no network surface of its own.

  EXIT CODES — 0 only when every discovered user was processed without a
  failure; 1 when one or more users failed; 2 on fatal infrastructure /
  application errors (e.g. the database is unreachable), including startup.

Invocation (operator / future nightly schedule):

    docker compose exec web python scripts/compute_taste_profiles.py
"""
import logging
import os
import sys
import time

# Add parent directory to path (same convention as sync_upcoming_episodes.py)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import User  # noqa: E402  (requires the sys.path insert above)

# Bounded chunk: ~200 users per pass per the Phase-3 design. run() accepts a
# smaller batch_size so tests can exercise boundaries.
BATCH_SIZE = 200

logger = logging.getLogger("frameiq.taste_profiles")

# Loaded lazily by _load() so an unreachable database at startup surfaces as a
# clean [FATAL] log + exit 2 from main() instead of a raw import traceback.
_app = None
_db = None
compute_profile = None


def _load():
    """Import the Flask app, session and canonical service (once)."""
    global _app, _db, compute_profile
    if _app is not None:
        return
    from app import app as flask_app, db as sqla_db
    from api.taste_profile import compute_profile as _compute_profile
    _app, _db, compute_profile = flask_app, sqla_db, _compute_profile


def _fetch_user_batch(last_id, batch_size):
    """Next bounded page of user ids, stable ascending order (keyset).

    Keyset pagination (WHERE id > last_id ORDER BY id LIMIT n) instead of
    OFFSET: no skipped/duplicated rows when users change mid-run, and no
    growing offset scan on large tables.
    """
    rows = (
        _db.session.query(User.id)
        .filter(User.id > last_id)
        .order_by(User.id)
        .limit(batch_size)
        .all()
    )
    return [row[0] for row in rows]


def process_batch(user_ids, stats):
    """Compute one batch, isolating per-user failures.

    On failure: roll back the in-flight session state (the user's previously
    persisted profile — if any — is untouched), record the failure, and
    continue with the remaining users in the batch. compute_profile() commits
    internally on success, so a successful user never loses its result.
    """
    for user_id in user_ids:
        try:
            compute_profile(user_id)
            stats['success'] += 1
        except Exception as exc:  # isolation is the point of this handler
            _db.session.rollback()
            stats['failed'] += 1
            logger.error("[FAIL] user_id=%s %s: %s",
                         user_id, type(exc).__name__, exc)


def _finish(stats, started):
    elapsed = time.monotonic() - started
    logger.info("[DONE] success=%d failed=%d skipped=%d elapsed=%.1fs",
                stats['success'], stats['failed'], stats['skipped'], elapsed)
    ok = stats['failed'] == 0
    logger.info("[STATUS] %s", "OK" if ok
                else "FAILED (%d user failure(s))" % stats['failed'])


def run(batch_size=BATCH_SIZE):
    """Recompute profiles for every user. Returns the run's stats dict.

    {success, failed, skipped} — main() maps failed>0 to exit 1 and fatal
    errors to exit 2. All work happens inside one app context; each user is
    committed individually by the canonical service, and identity-map state
    is released between batches so memory stays bounded. Batch-level
    infrastructure errors (fetch/session) propagate to the caller, which
    reports them as fatal.
    """
    _load()
    stats = {'success': 0, 'failed': 0, 'skipped': 0}
    started = time.monotonic()
    logger.info("[START] Taste profile recomputation")

    with _app.app_context():
        total = _db.session.query(User.id).count()
        logger.info("[INFO] Users: %d", total)
        if total == 0:
            _finish(stats, started)
            return stats

        total_batches = max(1, (total + batch_size - 1) // batch_size)
        last_id = 0
        batch_no = 0
        while True:
            ids = _fetch_user_batch(last_id, batch_size)
            if not ids:
                break
            batch_no += 1
            last_id = ids[-1]
            logger.info("[BATCH %d/%d] users id>=%d (n=%d)",
                        batch_no, total_batches, ids[0], len(ids))
            process_batch(ids, stats)
            # Release accumulated identity-map references between batches.
            _db.session.expire_all()

    _finish(stats, started)
    return stats


def main():
    """Operator entry point: configure logging, run, exit by result."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(message)s")
    try:
        stats = run()
    except Exception as exc:  # fatal infrastructure/application error
        logger.error("[FATAL] %s: %s", type(exc).__name__, exc)
        sys.exit(2)
    sys.exit(0 if stats['failed'] == 0 else 1)


if __name__ == '__main__':
    main()
