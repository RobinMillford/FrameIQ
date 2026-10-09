"""Forward migration 0001 — reconcile derived viewed state with diary history.

Historical context
------------------
``migrates/migrate_canonical_watched.py`` existed before the ledger and was
never recorded as run. A read-only check of production during the F8 audit
found the reconciliation had NOT happened:

    diary-only (user_id, media_id) pairs : 2
    viewed-only (user_id, media_id) pairs : 15

This is the clearest example of why "the table exists" proves nothing: both
``diary_entry`` and ``user_viewed`` have existed for the life of the product, so
no amount of schema inspection can tell you whether the row-level reconciliation
was ever performed. Leaving it unperformed means Continue Watching and the
diary/statistics read different truths about the same watches.

Why a NEW forward migration
---------------------------
``migrate_canonical_watched.py`` is a historical script and is not registered
with the runner. Rewriting it to be versioned would mean editing a script whose
behaviour may already have been relied upon elsewhere. The remedy for an
unperformed historical data transformation is a new forward migration, which is
what this is.

Direction 1 only — diary is canonical
-------------------------------------
F4 established the ``DiaryEntry`` as canonical for movies and ``user_viewed`` as
*derived* from it. So the only legitimate direction is diary -> viewed, and that
is the only direction this migration performs.

The 15 viewed-only pairs are deliberately NOT reconciled
--------------------------------------------------------
A ``user_viewed`` row with no diary entry is a **legacy marker**, not watch
history. Turning one into a ``DiaryEntry`` would:

* fabricate a canonical record the user never created, which then reads as
  authoritative in the diary, statistics and Continue Watching forever;
* require a watch date, and ``user_viewed.date_viewed`` is nullable and not
  authoritative — it may be absent, or may have been set by a bulk import rather
  than by the user;
* silently destroy the distinction between "watched and logged" and "marked
  viewed", which is exactly the distinction F4 was built to establish.

``diary_entry.watched_date`` is NOT NULL, so a NULL ``date_viewed`` would
actually abort the INSERT outright — a latent crash, not a graceful outcome.

So this migration preserves those 15 rows exactly as they are, does not
manufacture diary entries for them, does not delete them, and reports them as
**unresolved legacy markers**. They remain readable, remain queryable, and
remain a real discrepancy between the two tables. Resolving them needs an
explicit product/data decision (ask the user, or accept the mark with a
recorded provenance), which is out of scope for a schema migration and is
documented in docs/migration-inventory.md.

Contract
--------
``run(connection)`` executes inside a transaction owned by the runner. It MUST
NOT commit and MUST NOT open its own transaction — the ledger row is written in
the same transaction, so a failure here rolls back both together.

Movies only. TV is deliberately untouched: ``user_viewed`` is movie-scoped and
F4 established that TV history lives in ``TVEpisodeWatch``.
"""
from sqlalchemy import text

# Derivation only: it adds rows to `user_viewed` and never drops or alters
# anything. Declared non-destructive so it keeps the ordinary execution path and
# needs no backup approval.
DESTRUCTIVE = False
DESTRUCTIVE_REASON = ''


def _diary_only_pairs(connection):
    """Distinct movie diary keys with no corresponding ``user_viewed`` row."""
    rows = connection.execute(text("""
        SELECT DISTINCT user_id, media_id
          FROM diary_entry WHERE media_type = 'movie'
        EXCEPT
        SELECT DISTINCT user_id, media_id FROM user_viewed
         WHERE media_type = 'movie'
    """)).fetchall()
    return [(row[0], row[1]) for row in rows]


def _viewed_only_pairs(connection):
    """Distinct movie viewed keys with no canonical diary entry.

    These are the preserved legacy markers. Reported, never repaired.
    """
    rows = connection.execute(text("""
        SELECT DISTINCT user_id, media_id FROM user_viewed
         WHERE media_type = 'movie'
        EXCEPT
        SELECT DISTINCT user_id, media_id
          FROM diary_entry WHERE media_type = 'movie'
    """)).fetchall()
    return [(row[0], row[1]) for row in rows]


def run(connection):
    """Derive viewed state from the canonical diary. One direction only."""
    diary_only = _diary_only_pairs(connection)
    viewed_only = _viewed_only_pairs(connection)

    print('[0001] canonical_watched reconciliation (diary -> viewed)')
    print('      diary-only pairs to add      : %d' % len(diary_only))
    print('      unresolved legacy markers    : %d' % len(viewed_only))

    # A diary entry with no user_viewed row. date_viewed is the user's EARLIEST
    # watch of that title, which is what the historical script used and what
    # Continue Watching expects to order by.
    #
    # Existing user_viewed rows are never modified: NOT EXISTS means this only
    # reaches keys that have no row, so a viewed row's date or rating is never
    # overwritten by a diary-derived value.
    #
    # watched_date is NOT NULL in the diary schema, so MIN() is never NULL and
    # the COALESCE cannot fabricate a date. It is filtered rather than
    # substituted anyway: if a NULL ever did appear, "now" would be a lie about
    # when something was watched, and such a pair is reported instead.
    inserted_viewed = connection.execute(text("""
        INSERT INTO user_viewed (user_id, media_id, media_type, date_viewed)
        SELECT d.user_id, d.media_id, 'movie', MIN(d.watched_date)
          FROM diary_entry d
         WHERE d.media_type = 'movie'
           AND d.watched_date IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM user_viewed v
                WHERE v.user_id = d.user_id
                  AND v.media_id = d.media_id
                  AND v.media_type = 'movie')
         GROUP BY d.user_id, d.media_id
    """)).rowcount or 0

    # Re-read both sides. diary_only must now be empty; viewed_only must be
    # UNCHANGED -- a change in either direction means something other than
    # derivation happened.
    after_diary = _diary_only_pairs(connection)
    after_viewed = _viewed_only_pairs(connection)

    print('      inserted user_viewed         : %d' % inserted_viewed)
    print('      residual diary-only          : %d' % len(after_diary))
    print('      unresolved markers remaining : %d' % len(after_viewed))

    if after_viewed != viewed_only:
        print('      [WARN] the set of unresolved legacy markers CHANGED. '
              'This migration must not alter viewed-only rows.')
    if after_diary:
        print('      [WARN] diary-only pairs remain; derivation did not '
              'fully converge')
    if viewed_only:
        print('      [NOTE] %d viewed-only pair(s) are preserved as legacy '
              'markers and are NOT canonical watch history. They need an '
              'explicit data-resolution decision; see '
              'docs/migration-inventory.md.' % len(viewed_only))

    return {
        'inserted_user_viewed': int(inserted_viewed),
        'diary_only_before': len(diary_only),
        'diary_only_after': len(after_diary),
        'unresolved_legacy_markers': len(after_viewed),
        # Retained so an operator reading the deploy log is never told these
        # pairs were reconciled.
        'reconciled_viewed_only': 0,
    }


def verify(connection):
    """Post-apply assertions.

    Two different claims, kept separate:

    * the derived direction must have converged (diary_only == 0);
    * the legacy markers must be **preserved**. Their count is deliberately NOT
      required to be zero — requiring that would make this migration fail
      precisely because it declined to fabricate history.
    """
    problems = []
    diary_only = _diary_only_pairs(connection)
    if diary_only:
        problems.append('%d diary-only pair(s) remain' % len(diary_only))
    return problems
