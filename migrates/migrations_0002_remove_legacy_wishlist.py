"""Forward migration 0002 — consolidate the legacy ``user_wishlist``.

Supersedes the historical, never-recorded ``migrates/migrate_remove_wishlist.py``.
The same historical script was also a hand-maintained line in
``.github/workflows/deploy.yml``, and it silently did the wrong thing on
conflicting rows: it skipped any (user_id, media_id, media_type) already present
in the target and then dropped the source table, discarding the source row's
``date_added`` and ``priority`` with no record of the loss.

This version refuses to lose data.

Schema (verified read-only against production, 2026-09)
------------------------------------------------------
``user_wishlist`` and ``user_watchlist`` have IDENTICAL columns::

    user_id     integer        NOT NULL
    media_id    integer        NOT NULL
    media_type  varchar(20)    NOT NULL
    date_added  timestamp      NULL
    priority    varchar(10)    NULL  DEFAULT 'medium'

There are no legacy-only and no target-only columns, so no information in a
source row is unrepresentable in the target. Both tables are keyed by::

    PRIMARY KEY (user_id, media_id, media_type)

That key is the uniqueness rule referenced by ``EXACT_DUPLICATE`` below: it is
what makes "the same user already has this title" a well-defined question, and
it is why at most one target row can ever correspond to a source row.

Production state at the time of writing: 1 source row, 4 target rows, **no**
matching target row. The migration would therefore INSERT it, preserving its
``date_added`` (2026-01-27) and ``priority`` ('low').

Row-by-row policy
-----------------
Every source row is classified and counted, and the counts must add up to the
number of source rows before the DROP is allowed:

    INSERTED           no target row existed -> inserted, all source values kept
    EXACT_DUPLICATE    a target row existed and every shared column matched
    REDUNDANT_NULL     a target row existed and the source added nothing the
                       target lacks (its differing columns were NULL), so the
                       source row carried no information at all
    CONFLICT           a target row existed with DIFFERING non-NULL source
                       values -> cannot preserve without overwriting
    UNREPRESENTABLE    a source value cannot exist in the target (e.g. a NULL
                       media_id, which is part of the target's NOT NULL key)

``CONFLICT`` and ``UNREPRESENTABLE`` abort the migration **before** the DROP.
Nothing is deleted while a single source row is unaccounted for.

Transaction
-----------
``run(connection)`` executes inside a transaction owned by the runner: it never
commits and never opens its own transaction. On PostgreSQL, where DDL is
transactional, a failure here rolls back the INSERT and the DROP together, so
the legacy table survives a failed attempt. (SQLite's pysqlite commits before
DDL, so the DROP is not rolled back there — see docs/migration-inventory.md.)

DESTRUCTIVE. Requires an operator-verified snapshot. See
docs/migration-inventory.md.
"""
from sqlalchemy import inspect, text

# Declared to the runner. A destructive migration must say so, which puts it
# behind the backup/approval gate in scripts/migrate.py. Self-describing on
# purpose: if this flag is ever removed, the runner treats the migration as
# ordinary, so removing it is a deliberate act that shows up in review.
#
# The gate exists because no verified, restorable production snapshot has been
# recorded yet. Until an operator confirms one, this migration must not run.
DESTRUCTIVE = True
DESTRUCTIVE_REASON = (
    'drops user_wishlist after merging its rows; irreversible without a '
    'verified restorable snapshot'
)

KEY_COLUMNS = ('user_id', 'media_id', 'media_type')
# Columns compared when deciding whether an existing target row already
# contains everything the source row has to say.
INFORMATIVE = ('date_added', 'priority')


def _table_exists(connection, name):
    """Dialect-agnostic existence check.

    ``to_regclass`` is PostgreSQL-only; hard-coding one dialect would make the
    destructive path the least-tested one.
    """
    return inspect(connection).has_table(name)


def _row_id(row):
    """A stable label for error messages. Never includes user data."""
    return '(user_id=%s, media_id=%s, media_type=%s)' % (
        row.get('user_id'), row.get('media_id'), row.get('media_type'))


def _classify(source, targets):
    """Classify one source row against its target rows.

    Returns ``(verdict, lost_fields, reason)``. ``lost_fields`` names the
    columns whose source values would be destroyed by the DROP, which is what
    an operator needs in order to decide whether to keep, merge or discard.

    ``targets`` is the list of target rows for the same key. At most one is
    possible in practice, but the list form keeps this honest if the target's
    key ever loosens.
    """
    if len(targets) > 1:
        return ('UNREPRESENTABLE', (),
                '%d target rows share one source key, so no single row can be '
                'identified as "the" match' % len(targets))
    if not targets:
        return 'INSERTED', (), None
    target = targets[0]
    if all(source[name] == target[name] for name in INFORMATIVE):
        return 'EXACT_DUPLICATE', (), None
    # A differing column whose source value is NULL carries no information, so
    # dropping the source loses nothing. Anything else is real information that
    # would be destroyed by the DROP.
    lost = tuple(name for name in INFORMATIVE
                 if source[name] != target[name] and source[name] is not None)
    if lost:
        return ('CONFLICT', lost,
                'source has %s that differ from the existing target row and '
                'would be destroyed by the drop (the target already has a row '
                'for this key)' % ', '.join(lost))
    return 'REDUNDANT_NULL', (), None


def run(connection):
    """Merge then drop, refusing to drop anything unaccounted for."""
    print('[0002] legacy wishlist consolidation')

    if not _table_exists(connection, 'user_wishlist'):
        print('      user_wishlist is already absent — nothing to do')
        return {'source_rows': 0, 'inserted': 0, 'exact_duplicates': 0,
                'redundant_nulls': 0, 'dropped': False}

    if not _table_exists(connection, 'user_watchlist'):
        # Dropping the source here would destroy the only copy of the data.
        raise RuntimeError(
            'user_watchlist is absent but user_wishlist still holds data. '
            'Refusing to drop the only remaining copy. Restore user_watchlist '
            'first.')

    sources = [dict(row) for row in connection.execute(
        text('SELECT * FROM user_wishlist')).mappings()]
    print('      legacy wishlist rows : %d' % len(sources))

    counts = {'INSERTED': 0, 'EXACT_DUPLICATE': 0, 'REDUNDANT_NULL': 0}
    blocked = []
    lost_fields = set()

    for source in sources:
        if source.get('media_id') is None:
            blocked.append(('UNREPRESENTABLE', ('media_id',),
                            'media_id is NULL, and media_id is part of the '
                            'target primary key, so this row cannot be stored '
                            'in user_watchlist at all'))
            continue

        targets = [dict(row) for row in connection.execute(
            text('SELECT * FROM user_watchlist '
                 'WHERE user_id = :u AND media_id = :m AND media_type = :t'),
            {'u': source['user_id'], 'm': source['media_id'],
             't': source['media_type']}).mappings()]

        verdict, lost, reason = _classify(source, targets)
        if reason:
            blocked.append((verdict, lost, reason))
            lost_fields.update(lost)
            continue
        counts[verdict] += 1

    # Report the plan before touching anything, so a failure is legible.
    print('      would insert          : %d' % counts['INSERTED'])
    print('      exact duplicates      : %d' % counts['EXACT_DUPLICATE'])
    print('      redundant (NULL only) : %d' % counts['REDUNDANT_NULL'])

    if blocked:
        print('      REFUSING TO DROP — %d source row(s) unaccounted for:'
              % len(blocked))
        for verdict, _lost, reason in blocked:
            print('        - %s: %s' % (verdict, reason))
        # The verdict labels and the at-risk columns go into the RAISED message
        # too, not just stdout: the runner wraps and truncates this text, and an
        # operator reading a deploy failure needs to see WHICH rule blocked and
        # WHICH data is at risk, not only that something did.
        raise RuntimeError(
            'Refusing to drop user_wishlist: %d of %d source rows carry '
            'information that cannot be preserved. verdicts=[%s] '
            'at_risk_columns=[%s]. user_wishlist is left untouched. Resolve '
            'these rows explicitly, or record a product decision about which '
            'side wins, and re-run.'
            % (len(blocked), len(sources),
               ', '.join(sorted({v for v, _l, _r in blocked})),
               ', '.join(sorted(lost_fields)) or '(none)'))

    if counts['INSERTED']:
        _insert_missing(connection, counts['INSERTED'])

    _assert_all_accounted(connection, sources, counts)

    connection.execute(text('DROP TABLE user_wishlist'))
    print('      dropped user_wishlist (all %d source row(s) accounted for)'
          % len(sources))
    return {'source_rows': len(sources), 'inserted': counts['INSERTED'],
            'exact_duplicates': counts['EXACT_DUPLICATE'],
            'redundant_nulls': counts['REDUNDANT_NULL'], 'dropped': True}


def _insert_missing(connection, expected):
    """Insert only source rows that have no target row yet.

    Existing target rows are never touched: the NOT EXISTS guard means an
    INSERT only happens where no row exists, so nothing is overwritten.
    COALESCE guards legacy NULLs; both columns are non-NULL in production, so
    the real values pass through unchanged.

    Raises if the insert count disagrees with the plan — a mismatch means the
    plan and the data disagreed, and the source table must not be dropped.
    """
    merged = connection.execute(text("""
        INSERT INTO user_watchlist
            (user_id, media_id, media_type, date_added, priority)
        SELECT w.user_id, w.media_id, w.media_type,
               COALESCE(w.date_added, CURRENT_TIMESTAMP),
               COALESCE(w.priority, 'medium')
          FROM user_wishlist w
         WHERE NOT EXISTS (
               SELECT 1 FROM user_watchlist wl
                WHERE wl.user_id = w.user_id
                  AND wl.media_id = w.media_id
                  AND wl.media_type = w.media_type)
    """)).rowcount or 0
    if merged != expected:
        raise RuntimeError(
            'expected to insert %d row(s) but inserted %d; refusing to '
            'drop the source table' % (expected, merged))
    print('      inserted into watchlist: %d' % merged)
    return merged


def _assert_all_accounted(connection, sources, counts):
    """The gate immediately before the destructive step.

    Every source key must now exist in the target, and the classification
    counts must add up to the number of source rows. This catches a partial
    merge, a concurrent delete, and any accounting arithmetic error. It runs
    AFTER the merge and BEFORE the DROP, which is the only point where stopping
    still leaves the legacy table intact.
    """
    missing = connection.execute(text("""
        SELECT COUNT(*) FROM user_wishlist w
         WHERE NOT EXISTS (
               SELECT 1 FROM user_watchlist wl
                WHERE wl.user_id = w.user_id
                  AND wl.media_id = w.media_id
                  AND wl.media_type = w.media_type)
    """)).scalar() or 0
    if missing:
        raise RuntimeError(
            'Refusing to drop user_wishlist: %d source row(s) still have no '
            'target row after the merge' % int(missing))

    accounted = (counts['INSERTED'] + counts['EXACT_DUPLICATE']
                 + counts['REDUNDANT_NULL'])
    if accounted != len(sources):
        raise RuntimeError(
            'accounting mismatch: classified %d of %d source rows; refusing to '
            'drop' % (accounted, len(sources)))


def verify(connection):
    """Post-apply assertion: the legacy table must be gone."""
    problems = []
    if _table_exists(connection, 'user_wishlist'):
        problems.append('user_wishlist still exists after the migration')
    return problems
