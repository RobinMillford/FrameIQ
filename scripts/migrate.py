"""The authoritative migration runner (Task F8).

One command, one execution path:

    python scripts/migrate.py status                 read-only
    python scripts/migrate.py validate               read-only
    python scripts/migrate.py upgrade                WRITES
    python scripts/migrate.py adopt-legacy-baseline  WRITES (once)

There is deliberately no other way to apply a migration. In particular:

* importing ``app``, this module, ``migrates.registry`` or
  ``utils.schema_guard`` executes nothing. :func:`main` is the only caller of
  :func:`apply_pending`, and it is reached only through ``__main__``.
* the web process never invokes it. Nothing in ``app.py``, ``gunicorn.conf.py``
  or a container entrypoint calls this file.

Fail-closed rules
-----------------
Each of these refuses and changes nothing:

* a malformed registry (duplicate version, unknown dependency, forward
  dependency, cycle);
* an **applied** migration whose file checksum no longer matches — the ledger
  keeps the original checksum and the run stops;
* an already-applied version that is somehow re-registered;
* a pending migration whose declared dependencies are not all applied;
* a migration that fails: it is not recorded, later migrations do not run, and
  the exit code is non-zero.

Concurrency
-----------
A PostgreSQL **transaction-scoped advisory lock** is taken before pending
migrations are determined, and released at the end of the run (including on
failure). Advisory locks vanish automatically when the connection closes, so a
killed runner cannot leave a permanently blocked deployment — there is no lock
row to clean up.

On a backend without PostgreSQL advisory locks (the test suite runs SQLite)
``advisory_lock`` reports that locking is unavailable and continues. That is
reported honestly rather than pretended: SQLite has no session-level advisory
lock, so two concurrent runners on SQLite are serialised by database-level
write locking only, and the PostgreSQL guarantee is asserted in the unit tests
that mock the lock, not on SQLite.

Transactions
------------
One transaction per migration, owned here. The migration body and its ledger row
commit atomically, so a crash can never leave a ledger row for work that did not
happen, nor work that happened without a ledger row. A migration that cannot be
run transactionally must say so by exposing ``NON_TRANSACTIONAL = True``; the
runner then refuses rather than pretending the guarantee holds.

Backend caveat, stated because it is easy to over-claim here. On PostgreSQL the
guarantee is complete: DDL is transactional, so a failing migration leaves no
trace. On SQLite, pysqlite commits before DDL, so a failing migration that had
already run DDL may leave that DDL behind while still correctly NOT recording a
ledger row. The ledger guarantee holds everywhere; the DDL guarantee is
PostgreSQL-only. Production is PostgreSQL; SQLite is exercised in CI and locally,
so the weaker guarantee is the one that is actually tested.
"""
import argparse
import datetime
import hashlib
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrates'))

from sqlalchemy import inspect  # noqa: E402

from migrates import registry  # noqa: E402
from utils.db_target import UnsafeTargetError, \
    require_writable_target  # noqa: E402

# One fixed key for the migration lock. Any value works; a constant is easier to
# recognise in pg_locks and guarantees every runner contends for the same lock.
LOCK_KEY = 0x46515538  # 'FU8'

BASELINE_VERSION = '0000_legacy_baseline'
BASELINE_NOTE = (
    'Pre-F8 legacy baseline. The schema was verified against the declared '
    'models and the schema guard at adoption time. Individual historical '
    'migrates/*.py execution was never recorded and is NOT claimed here; see '
    'docs/migration-inventory.md.'
)


class MigrationError(RuntimeError):
    """A migration problem an operator must resolve. Never auto-recovered."""


class ChecksumMismatch(MigrationError):
    """An applied migration's file changed after it ran."""


class DestructiveApprovalError(MigrationError):
    """A destructive migration was reached without backup/operator evidence."""


# Values that look like evidence but are not. A generic YES, a boolean secret or
# a stale repository variable must never be able to unlock a DROP.
PLACEHOLDER_EVIDENCE = frozenset({
    'yes', 'y', 'true', '1', 'on', 'ok', 'okay', 'sure', 'confirmed', 'done',
    'none', 'null', 'nil', 'n/a', 'na', '-', '--', '_', 'x', 'xx',
    'todo', 'tbd', 'fixme', 'changeme', 'placeholder', 'dummy', 'example',
    'sample', 'foo', 'bar', 'baz', 'asdf', 'backup', 'snap', 'snapshot',
})
MIN_BACKUP_REF_LENGTH = 8


class DestructiveApproval:
    """Operator evidence that a specific destructive migration may run.

    Three separate, non-placeholder facts, all required:

    * ``authorized_version`` — the EXACT migration id being authorized;
    * ``backup_ref``        — a reference to a snapshot that was created;
    * ``verified_by``       — who confirmed it is recoverable.

    Design notes
    ------------
    The authorization names one migration version, so it cannot be reused as a
    blanket pass for a future, different destructive migration: that one would
    have to be authorized by its own id. Nothing here is read from the
    environment, so a deploy cannot widen its own authority by setting a
    variable that happens to be lying around.
    """

    def __init__(self, authorized_version=None, backup_ref=None,
                 verified_by=None):
        self.authorized_version = (authorized_version or '').strip()
        self.backup_ref = (backup_ref or '').strip()
        self.verified_by = (verified_by or '').strip()

    @property
    def empty(self):
        return not (self.authorized_version or self.backup_ref
                    or self.verified_by)

    def problems_for(self, version):
        """Every reason this approval does not cover ``version``."""
        issues = []
        if self.authorized_version != version:
            if self.authorized_version:
                issues.append(
                    'the supplied authorization names %r, not %r. Approval is '
                    'per-migration and cannot carry over.'
                    % (self.authorized_version, version))
            else:
                issues.append('no --authorize-destructive was supplied')

        for label, value in (('--backup-ref', self.backup_ref),
                             ('--backup-verified-by', self.verified_by)):
            if not value:
                issues.append('%s was not supplied' % label)
            elif value.lower() in PLACEHOLDER_EVIDENCE:
                issues.append(
                    '%s is the placeholder %r, which is not evidence that a '
                    'backup exists' % (label, value))

        if self.backup_ref and len(self.backup_ref) < MIN_BACKUP_REF_LENGTH:
            issues.append(
                '--backup-ref is too short to identify a snapshot (minimum %d '
                'characters)' % MIN_BACKUP_REF_LENGTH)
        return issues

    def audit_note(self, version):
        """What gets recorded in the ledger, for a later audit."""
        return ('destructive migration authorized: version=%s backup_ref=%s '
                'verified_by=%s' % (version, self.backup_ref, self.verified_by))


def require_destructive_approval(spec, module, approval):
    """Refuse a destructive migration that lacks real backup evidence.

    Called immediately before the module's ``run``, so no code path can reach a
    destructive migration without passing through here.
    """
    if not getattr(module, 'DESTRUCTIVE', False):
        return None
    version = spec.version
    issues = approval.problems_for(version)
    if issues:
        reason = getattr(module, 'DESTRUCTIVE_REASON', 'unspecified')
        raise DestructiveApprovalError(
            'Refusing to run destructive migration %s.\n'
            '  It %s.\n'
            '  No verified restorable snapshot has been recorded, so it is '
            'blocked. Required, all of them per-deployment:\n'
            '    --authorize-destructive %s   (this exact migration)\n'
            '    --backup-ref <snapshot reference>\n'
            '    --backup-verified-by <operator who confirmed it restores>\n'
            '  Unmet:\n    - %s\n'
            '  Nothing has been applied or dropped. %s has not been touched.'
            % (version, reason, version, '\n    - '.join(issues),
               'user_wishlist' if 'wishlist' in version else 'the legacy table'))
    return approval.audit_note(version)


def _utcnow_naive():
    """UTC now, without a tzinfo.

    The ledger column is a naive DateTime (matching the rest of the schema,
    which uses ``datetime.utcnow`` throughout). Storing an aware value into a
    naive column would silently drop or shift the offset depending on backend.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ── checksums ────────────────────────────────────────────────────────────────


def module_path(spec):
    """Filesystem path of a registered migration module."""
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'migrates',
        spec.module.split('.')[-1] + '.py')


def checksum_for(spec):
    """sha256 over the COMPLETE migration source file.

    The whole file, not a selected portion. A partial checksum would let an
    edit to an unincluded region go undetected, and "did the thing that ran
    change?" is exactly the question this exists to answer. The cost is that a
    comment-only edit is also a mismatch — which is the correct, conservative
    answer: applied migrations are immutable, and a comment change means writing
    a new migration instead.
    """
    path = module_path(spec)
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ── locking ──────────────────────────────────────────────────────────────────


class advisory_lock:
    """Session-scoped PostgreSQL advisory lock, or an honest no-op.

    ``pg_advisory_lock`` (session-scoped, not the ``_xact_`` variant) because the
    lock must be held across *several* transactions: one to read the ledger and
    decide what is pending, then one per applied migration. A transaction-scoped
    lock would be released by the first COMMIT and would not cover the window in
    which two runners could both decide the same migration was pending.

    It is released explicitly, and also automatically when the connection closes
    — so a killed runner cannot leave a deployment permanently blocked. There is
    no lock table and therefore no stale row to clean up.
    """

    def __init__(self, connection):
        self.connection = connection
        self.available = False
        self.key = LOCK_KEY

    def __enter__(self):
        from sqlalchemy import text
        try:
            self.connection.execute(text('SELECT pg_advisory_lock(:k)'),
                                    {'k': self.key})
            self.available = True
            print('  lock: acquired (postgres session advisory, key=%d)'
                  % self.key)
        except Exception as exc:  # noqa: BLE001
            self.available = False
            # On PostgreSQL a failed statement ABORTS the transaction, so every
            # later statement on this connection would fail with
            # "current transaction is aborted" until it is rolled back. Since
            # continuing without the lock is a supported (loud) mode, the
            # connection must be returned to a usable state rather than being
            # left poisoned.
            try:
                self.connection.rollback()
            except Exception:  # noqa: BLE001
                pass
            print('  lock: UNAVAILABLE on this backend (%s)'
                  % str(exc).splitlines()[0][:64])
            print('        Continuing WITHOUT concurrent-runner exclusion. '
                  'Do not rely on this for a shared database.')
        return self

    def __exit__(self, *exc_info):
        if self.available:
            try:
                from sqlalchemy import text
                self.connection.execute(text('SELECT pg_advisory_unlock(:k)'),
                                        {'k': self.key})
                print('  lock: released')
            except Exception:  # noqa: BLE001
                # Closing the connection releases it anyway.
                pass
        return False


# ── ledger access ────────────────────────────────────────────────────────────


def load_applied(connection):
    """``{version: (checksum, kind)}`` currently recorded."""
    from sqlalchemy import text
    rows = connection.execute(text(
        'SELECT version, checksum, kind FROM schema_migrations')).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def ensure_ledger_table(connection):
    """Create the ledger table if absent.

    Not a registered migration: the ledger must exist *before* any versioned
    migration can be recorded. Idempotent.
    """
    import models  # noqa: F401  (populates metadata; see below)
    from models.base import db

    # `import models` is required, not incidental. Importing `models.base`
    # already runs the package __init__ today, so the table happens to be
    # registered -- but relying on that means a future refactor of
    # models/__init__.py could silently turn this into a KeyError on the
    # deploy path only, where no test would see it.
    table = db.metadata.tables['schema_migrations']
    table.create(bind=connection, checkfirst=True)


# ── planning ─────────────────────────────────────────────────────────────────


def plan(applied):
    """Split registered migrations into pending / mismatched / satisfied.

    Pure function over the registry plus the ledger — no database access, so it
    is trivially testable and safe to call from the read-only ``status``.
    """
    specs = registry.ordered_migrations()
    checksums = {spec.version: checksum_for(spec) for spec in specs}

    mismatched, pending = [], []
    for spec in specs:
        recorded = applied.get(spec.version)
        if recorded is None:
            pending.append(spec)
        elif recorded[0] != checksums[spec.version]:
            mismatched.append((spec, recorded[0]))

    satisfied = {spec.version for spec in specs
                 if spec.version in applied
                 and applied[spec.version][0] == checksums[spec.version]}
    # The baseline is deliberately NOT in the registry -- it is a record of
    # adopted history, not a migration. Listing it as "orphaned" would train
    # operators to ignore the very warning that catches a real unknown entry.
    orphaned = sorted(version for version, (_c, kind) in applied.items()
                      if version not in checksums and kind != 'baseline')

    return {'pending': pending, 'mismatched': mismatched,
            'satisfied': satisfied, 'orphaned': orphaned,
            'checksums': checksums}


def missing_dependencies(spec, applied_versions):
    """Dependencies of ``spec`` not recorded as applied."""
    return [dep for dep in spec.depends_on if dep not in applied_versions]


def read_state(connection):
    """Everything ``status``/``validate`` need, without writing."""
    applied = load_applied(connection)
    return applied, plan(applied)


# ── execution ────────────────────────────────────────────────────────────────


def is_destructive(spec):
    """True when the module declares itself destructive.

    Reading the module is the only reliable source: the flag lives with the
    migration, so a new destructive migration cannot skip the gate by forgetting
    to be registered as dangerous somewhere else.
    """
    try:
        return bool(getattr(load_module(spec), 'DESTRUCTIVE', False))
    except MigrationError:
        # An unloadable module is not silently treated as harmless; report it as
        # needing review rather than letting it through as "ordinary".
        return True


def load_module(spec):
    try:
        module = importlib.import_module(spec.module.split('.')[-1])
    except ImportError as exc:
        raise MigrationError(
            '%s names module %r, which cannot be imported: %s'
            % (spec.version, spec.module, exc)) from exc
    if not hasattr(module, 'run') or not callable(module.run):
        raise MigrationError(
            '%s has no run(connection) function' % spec.version)
    if getattr(module, 'NON_TRANSACTIONAL', False):
        raise MigrationError(
            '%s declares NON_TRANSACTIONAL; the runner will not pretend the '
            'ledger row and the work commit together' % spec.version)
    return module


def apply_one(engine, spec, checksum, approval=None):
    """Run one migration and record it in ONE transaction.

    The ledger row is written inside the same transaction as the migration body.
    If the body raises, both roll back: no misleading applied record and no
    half-applied schema. If the process dies mid-flight the transaction aborts
    and the ledger never claims the work happened.

    ``approval`` is checked BEFORE the module's ``run`` is invoked, so a
    destructive migration cannot be reached without backup evidence regardless of
    which caller got here.
    """
    from sqlalchemy import text
    module = load_module(spec)
    note = require_destructive_approval(
        spec, module, approval or DestructiveApproval())
    if note:
        print('      [AUTHORIZED] destructive migration: %s' % note)
    print('  --> applying %s' % spec.version)
    started = time.monotonic()
    connection = engine.connect()
    try:
        with connection.begin():
            result = module.run(connection)
            problems = module.verify(connection) if hasattr(
                module, 'verify') else []
            if problems:
                raise MigrationError('%s did not converge: %s'
                                     % (spec.version, '; '.join(problems)))
            connection.execute(text("""
                INSERT INTO schema_migrations
                    (version, checksum, kind, applied_at, execution_ms, note)
                VALUES (:v, :c, 'migration', :t, :ms, :n)
            """), {
                'v': spec.version, 'c': checksum,
                't': _utcnow_naive(),
                'ms': int((time.monotonic() - started) * 1000),
                'n': note,
            })
    except MigrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MigrationError('%s failed: %s' % (spec.version,
                                                str(exc)[:300])) from exc
    finally:
        connection.close()
    elapsed = int((time.monotonic() - started) * 1000)
    print('      done in %d ms%s' % (
        elapsed, ('  %s' % result) if isinstance(result, dict) else ''))
    return elapsed


def _guard_problems(applied, report):
    """Reasons the run must refuse, as a list of strings."""
    problems = []
    for spec, recorded in report['mismatched']:
        problems.append(
            'applied migration %s was modified after it ran '
            '(recorded %s, file now %s)'
            % (spec.version, recorded[:12], report['checksums'][spec.version][:12]))
    return problems


def decide_pending(applied, only=None):
    """The exact list to apply, or refuse. Pure, so it is easy to test.

    Three fail-closed checks, in the order that would do the most damage if
    skipped: a registry that does not make sense, an applied migration whose
    file changed, then a pending migration whose dependencies are unmet.
    """
    problems = registry.validate()
    if problems:
        raise MigrationError('registry is invalid: %s'
                             % '; '.join(problems))

    report = plan(applied)
    mismatched = _guard_problems(applied, report)
    if mismatched:
        raise ChecksumMismatch(
            'Refusing to run. An applied migration must never be edited; the '
            'remedy is a NEW forward migration:\n  %s'
            % '\n  '.join(mismatched))

    pending = report['pending']
    if only:
        pending = [spec for spec in pending if spec.version in only]

    # A destructive migration is only ever attempted when the operator named it
    # explicitly with --only. Left unnamed it is deferred, not attempted, so an
    # ordinary deploy is never permanently blocked by an unapproved DROP -- and
    # it is reported rather than quietly dropped from the plan.
    destructive, ordinary = [], []
    for spec in pending:
        (destructive if is_destructive(spec) else ordinary).append(spec)

    selected = only or set()
    deferred = [spec for spec in destructive if spec.version not in selected]
    if deferred:
        print('DEFERRED (destructive, needs approval + verified backup):')
        for spec in deferred:
            print('  - %s' % spec.version)
        print('  Apply it in the approval-gated step with:')
        print('    --only %s --authorize-destructive %s'
              % (spec.version, spec.version))
        print('    --backup-ref <snapshot> --backup-verified-by <operator>')

    # Only the explicitly selected destructive ones are attempted. The rest are
    # reported above and left pending, so an ordinary deploy is never blocked by
    # an unapproved DROP and nothing is silently dropped from the plan.
    pending = ordinary + [spec for spec in destructive
                          if spec.version in selected]
    if not pending:
        return []

    # Dependencies are validated against the versions that will exist once the
    # EARLIER migrations in this same run have completed -- not just the ones
    # already recorded. Otherwise the first migration of a fresh database could
    # never be applied, because its dependents would look unmet.
    satisfied = set(applied)
    for spec in pending:
        absent = missing_dependencies(spec, satisfied)
        if absent:
            raise MigrationError(
                '%s depends on %s, which is not applied and does not run '
                'before it' % (spec.version, ', '.join(absent)))
        satisfied.add(spec.version)
    return pending


def apply_pending(engine, only=None, approval=None):
    """Apply every pending migration in order. Stops at the first failure.

    The lock is taken BEFORE pending migrations are determined, so two runners
    cannot both decide the same migration is pending and then both apply it.
    """
    lock_connection = engine.connect()
    try:
        with advisory_lock(lock_connection):
            # Idempotent DDL: the ledger must exist before it can be read.
            # Read-only commands never reach this path.
            ensure_ledger_table(lock_connection)
            lock_connection.commit()

            # Read the ledger and decide what is pending UNDER the lock, so two
            # runners cannot both conclude the same migration is pending.
            applied = load_applied(lock_connection)
            pending = decide_pending(applied, only)
            checksums = plan(applied)['checksums']

            if not pending:
                print('No pending migrations.')
                return []

            applied_now = []
            for spec in pending:
                try:
                    apply_one(engine, spec, checksums[spec.version],
                              approval=approval)
                except MigrationError:
                    print('\nFAILED at %s. Nothing after it will run, and it is '
                          'NOT recorded as applied.' % spec.version)
                    raise
                applied_now.append(spec.version)
            return applied_now
    finally:
        lock_connection.close()


def adopt_legacy_baseline(engine):
    """Write the one-time baseline record. Never automatic.

    Refuses unless the live schema is actually verified:

    * the schema guard must pass;
    * the database must not be empty (an empty database is bootstrap, not an
      adopted legacy database — the two must never be confused);
    * a baseline must not already exist.

    The record asserts that the schema was verified. It does **not** claim that
    any individual historical script ran, because nobody recorded that.
    """
    from sqlalchemy import text
    connection = engine.connect()
    try:
        with connection.begin():
            ensure_ledger_table(connection)
            applied = load_applied(connection)
            existing = [v for v, (_c, kind) in applied.items()
                        if kind == 'baseline']
            if existing:
                print('Baseline already recorded (%s). Nothing to do.'
                      % ', '.join(sorted(existing)))
                return False

            # Dialect-agnostic. The earlier `information_schema` query made
            # baseline adoption untestable outside PostgreSQL -- which is
            # exactly backwards for the one operation that writes a permanent
            # claim about a database.
            live_tables = [name for name
                           in inspect(connection).get_table_names()
                           if name != 'schema_migrations']
            if not live_tables:
                raise MigrationError(
                    'This database has no tables. An empty database is a '
                    'FRESH BOOTSTRAP, not a verified legacy database, and '
                    'adopting it as a baseline would be a false record. Use '
                    'scripts/bootstrap_dev_schema.py, then apply migrations.')

            from utils.schema_guard import check_schema
            verdict = check_schema(connection.engine)
            if not verdict['ok']:
                raise MigrationError(
                    'Schema does not match the declared models, so a baseline '
                    'cannot be recorded. Run the migrations first.\n  %s'
                    % '; '.join(verdict.get('missing_tables', []) or [])[:300])

            connection.execute(text("""
                INSERT INTO schema_migrations
                    (version, checksum, kind, applied_at, execution_ms, note)
                VALUES (:v, :c, 'baseline', :t, 0, :n)
            """), {
                'v': BASELINE_VERSION,
                # The baseline is not a file, so its checksum is the sha256 of
                # this constant: stable, and meaningful ("what was adopted").
                'c': hashlib.sha256(BASELINE_NOTE.encode()).hexdigest(),
                't': _utcnow_naive(),
                'n': BASELINE_NOTE,
            })
        print('Baseline %s recorded.' % BASELINE_VERSION)
        print('  This asserts the schema was VERIFIED at this time.')
        print('  It does NOT assert that each historical migrates/*.py ran.')
        return True
    finally:
        connection.close()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _engine_for(allow_production):
    """Build an engine, after classifying (and possibly refusing) the target."""
    from sqlalchemy import create_engine
    target = require_writable_target('the migration runner',
                                     allow_production=allow_production)
    print('Target: %s' % target.safe_identity())
    return create_engine(target.raw), target


def _engine_for_readonly():
    """Read-only commands still need a target, but never a writable one."""
    from sqlalchemy import create_engine
    target = require_writable_target('the migration status report',
                                     allow_production=True)
    print('Target: %s' % target.safe_identity())
    return create_engine(target.raw), target


def cmd_status(engine):
    """Report migration state without changing anything.

    Distinguishes "schema looks right" from "history is known". Those are
    different claims: a passing schema guard says nothing about provenance, and
    an empty ledger does not mean no historical changes were applied.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)
    if 'schema_migrations' not in inspector.get_table_names():
        print()
        print('No migration ledger on this database.')
        print('  Schema may still be correct, but WHICH migrations produced it')
        print('  is unknown. An empty ledger is not evidence that nothing ran.')
        return 0

    connection = engine.connect()
    try:
        applied, report = read_state(connection)
    finally:
        connection.close()

    print()
    print('Recorded migrations : %d' % len(applied))
    for version in sorted(applied):
        checksum, kind = applied[version]
        print('  %-40s %-10s %s' % (version, kind, checksum[:12]))
    baselines = [v for v, (_c, k) in applied.items() if k == 'baseline']
    baseline_text = ', '.join(sorted(baselines)) if baselines else 'NOT ADOPTED'
    print('Baseline            : %s' % baseline_text)
    print('Pending             : %d' % len(report['pending']))
    for spec in report['pending']:
        print('  %-40s %s' % (spec.version, spec.summary))
    if report['mismatched']:
        print('CHECKSUM MISMATCH  : %d' % len(report['mismatched']))
        for spec, recorded in report['mismatched']:
            print('  %s recorded=%s file=%s'
                  % (spec.version, recorded[:12],
                     report['checksums'][spec.version][:12]))
    else:
        print('Checksum mismatches : none')
    if report['orphaned']:
        print('Recorded but not registered: %s'
              % ', '.join(report['orphaned']))

    print()
    _print_guard(engine)

    if baselines:
        print()
        print('Migration history is known from the baseline onward.')
    else:
        print()
        print('Migration history is NOT known for anything before adoption.')
    return 0


def _print_guard(engine):
    """Schema guard result, kept separate from provenance reporting.

    These are different claims and the output must not blur them: a passing
    guard says the live schema matches the models, nothing about which
    migration produced it.
    """
    try:
        from utils.schema_guard import check_schema
        verdict = check_schema(engine)
    except Exception as exc:  # noqa: BLE001
        print('Schema guard        : could not run (%s)' % str(exc)[:80])
        return
    print('Schema guard        : %s'
          % ('OK' if verdict['ok'] else 'MISMATCH'))
    if not verdict['ok']:
        tables = ', '.join(verdict.get('missing_tables') or []) or '-'
        columns = ', '.join(verdict.get('missing_columns') or []) or '-'
        print('  missing tables  : %s' % tables)
        print('  missing columns : %s' % columns)


def cmd_validate(engine):
    """Validate the registry and every recorded checksum. Writes nothing."""
    connection = engine.connect()
    try:
        try:
            applied = load_applied(connection)
        except Exception:  # noqa: BLE001 - ledger may not exist yet
            applied = {}
        report = plan(applied)
    finally:
        connection.close()

    problems = registry.validate()
    print('Registry problems   : %d' % len(problems))
    for problem in problems:
        print('  - %s' % problem)
    print('Registered          : %d'
          % len(registry.ordered_migrations()))
    print('Checksum mismatches : %d' % len(report['mismatched']))
    for spec, recorded in report['mismatched']:
        print('  - %s recorded=%s file=%s'
              % (spec.version, recorded[:12],
                 report['checksums'][spec.version][:12]))
    if problems or report['mismatched']:
        print('\nFAILED: the registry or the ledger is not trustworthy.')
        return 1
    print('\nOK')
    return 0


def cmd_upgrade(engine, only, approval=None):
    print()
    applied = apply_pending(engine, only=only, approval=approval)
    if applied:
        print('\nApplied %d migration(s):' % len(applied))
        for version in applied:
            print('  + %s' % version)
    return 0


def cmd_adopt_baseline(engine):
    print()
    if adopt_legacy_baseline(engine):
        print('\nLegacy baseline adopted. Future deploys use `upgrade`.')
    return 0


def _approval_from(args):
    """Build the approval object from whatever the operator supplied."""
    return DestructiveApproval(
        authorized_version=getattr(args, 'authorize_destructive', None),
        backup_ref=getattr(args, 'backup_ref', None),
        verified_by=getattr(args, 'backup_verified_by', None))


def _dispatch(args, engine):
    """Map the command word to its handler."""
    handlers = {
        'status': lambda: cmd_status(engine),
        'validate': lambda: cmd_validate(engine),
        'upgrade': lambda: cmd_upgrade(engine, set(args.only or ()),
                                       approval=_approval_from(args)),
        'adopt-legacy-baseline': lambda: cmd_adopt_baseline(engine),
    }
    return handlers[args.command]()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='migrate.py',
        description='FrameIQ versioned migration runner. The authoritative '
                    'path for applying schema changes.')
    parser.add_argument('command', choices=(
        'status', 'validate', 'upgrade', 'adopt-legacy-baseline'))
    parser.add_argument('--allow-production', action='store_true',
                        help='permit a remote target. There is no environment '
                             'variable that does this: it must be chosen.')
    parser.add_argument('--only', action='append', default=None,
                        help='apply only these version(s); repeatable')
    group = parser.add_argument_group(
        'destructive-migration approval',
        'Required per deployment, and only for a migration that declares '
        'DESTRUCTIVE. Names one exact migration id, so it cannot become a '
        'blanket pass for a different destructive migration later.')
    group.add_argument('--authorize-destructive', default=None,
                       metavar='VERSION',
                       help='exact migration id being authorized to run')
    group.add_argument('--backup-ref', default=None, metavar='REF',
                       help='reference to a snapshot that was actually created')
    group.add_argument('--backup-verified-by', default=None, metavar='WHO',
                       help='operator who confirmed the snapshot restores')
    args = parser.parse_args(argv)

    os.environ.setdefault('SKIP_SCHEMA_GUARD', '1')
    writing = args.command in ('upgrade', 'adopt-legacy-baseline')
    try:
        if writing:
            engine, _target = _engine_for(args.allow_production)
        else:
            engine, _target = _engine_for_readonly()
    except UnsafeTargetError as exc:
        print(str(exc), file=sys.stderr)
        return 97

    try:
        return _dispatch(args, engine)
    except MigrationError as exc:
        print('\nFAILED: %s' % exc, file=sys.stderr)
        return 1
    except UnsafeTargetError as exc:
        print(str(exc), file=sys.stderr)
        return 97
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
