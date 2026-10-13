#!/usr/bin/env python3
"""Explicit local/test schema bootstrap. Prints before it writes.

The application no longer creates tables at import time (see ``app.py``), so a
developer or a scratch database needs an explicit way to create the declared
schema. This script is that way — and it is deliberately *loud*, because the
whole point of the change was that creating tables stopped being a side effect
of importing a module.

What it does
------------
Creates every table the models declare, and nothing else. It never ALTERs an
existing table and never deletes anything, so running it against a database
that is already current is a no-op.

Safety
------
* Classifies the target with :mod:`utils.db_target` — the repository's single
  source of truth for "which database may this command touch" — and refuses
  anything remote unless ``--allow-production`` is passed. Classification
  happens from the URL alone, so the refusal works before any connection is
  opened.
* **Fails closed.** A missing, empty, whitespace-only, malformed or otherwise
  unparseable target is refused. A parse failure is never treated as evidence
  that a target is safe: this script used to allow anything containing neither
  ``@`` nor ``://``, which meant ``garbage`` and ``""   ""`` were classified
  "local" and only failed later, at the driver.
* **SQLite only, by default.** ``db_target`` classifies a loopback PostgreSQL
  host as local, which is right for the migration runner but wrong *here*: this
  deployment publishes Postgres on ``127.0.0.1:5432`` on the application host
  (see ``docker-compose.yml``), so on that machine ``localhost:5432`` *is* the
  production database. A classifier sees a URL, not a machine, and cannot tell
  a laptop from a VPS. This tool therefore narrows the shared verdict rather
  than inheriting it — see :func:`_require_development_target`.
* **``--allow-production`` does not reach the production database.** It lifts
  the SQLite-only default for a genuinely *remote* database, matching how
  ``scripts/migrate.py`` uses it. It cannot unlock a loopback host, because a
  loopback URL carries nothing that distinguishes a laptop's scratch server
  from the application's own database, and it cannot unlock an unusable URL.
  Both of those refusals are unconditional.
* Prints ``DbTarget.safe_identity()``, which strips credentials, so a mistyped
  ``DATABASE_URL`` is visible before DDL is attempted and the output is safe
  to paste into an issue.
* Requires ``SKIP_SCHEMA_GUARD=1``: on a not-yet-created database the startup
  guard would otherwise refuse to import. That bypass is a *different* concern
  from write authorization — it is set only after the target is authorized, and
  it never substitutes for it.

Behaviour note (F22.2)
----------------------
Two changes, neither of which widens the accepted-target policy:

* **Fail-closed.** Every malformed or ambiguous target that used to be allowed
  is now refused.
* **The override is narrowed.** ``--allow-production`` previously
  short-circuited the entire authorization, so it also permitted malformed URLs
  and loopback PostgreSQL. It now lifts only the SQLite-only default.

Loopback PostgreSQL was refused before this change and is still refused, both
with and without the flag. An earlier draft delegated straight to
``require_writable_target``, which would have silently *widened* the policy by
admitting loopback PostgreSQL; that widening was reverted because it conflicts
with the published loopback port, and ``.env.example`` documents a loopback
``DATABASE_URL`` as the local-development shape — so a developer's habit of
pasting that URL would otherwise have become a production write on the VPS.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Imported after the sys.path bootstrap above, hence E402. This must stay
# above the application import: db_target is pure URL classification and opens
# nothing, which is exactly why the guard can run before `app` is imported.
from utils.db_target import (  # noqa: E402
    UnsafeTargetError, classify, require_writable_target,
)

#: Only this scheme is accepted without an explicit override. A scratch file or
#: in-memory database cannot be a production database by construction.
DEVELOPMENT_SCHEME = 'sqlite'


def _looks_production(url):
    """True when ``url`` is not an obviously local/scratch target.

    Kept as a thin delegation rather than a second implementation. It used to
    carry its own hostname denylist plus an ``'@' in url or '://' in url``
    heuristic, which duplicated the authoritative classifier and — because
    anything matching neither pattern fell through to "not production" —
    classified malformed input as safe.

    NOTE: this is a *classification* predicate, not this tool's authorization
    decision. It answers "does this look remote?". The bootstrap refuses some
    targets it does not consider production — notably loopback PostgreSQL — so
    never use this to decide whether a write may proceed. Use
    :func:`_require_development_target` for that.
    """
    return classify(url or '').is_production_like


def _is_usable_database_url(target):
    """True when ``target`` names a database well enough to connect to.

    This is a *structural* check, not a local/remote classification — that
    belongs to :func:`utils.db_target.classify` and is not repeated here. It
    asks only whether the parsed URL carries the components a connection needs,
    so that ``--allow-production`` cannot be spent on a string that could never
    have named a database.
    """
    if not target.scheme or not target.database:
        return False
    if not target.is_sqlite and not target.host:
        return False
    return True


def _require_development_target(url, allow_production=False):
    """Authorize ``url`` for schema creation, or raise.

    :func:`utils.db_target.require_writable_target` makes the authoritative
    remote/local call and owns the ``allow_production`` contract; nothing here
    re-implements it. This function then applies two refusals that no flag can
    lift, and one default that ``--allow-production`` can:

    1. **Unusable URL.** Refused even with ``--allow-production``. An operator
       cannot knowingly authorise a database they have not named.
    2. **Loopback PostgreSQL.** Refused even with ``--allow-production``. A
       loopback address is ambiguous in a way a flag cannot resolve: on a
       laptop it is a scratch server, and on this deployment's application host
       it is the production database (``docker-compose.yml`` publishes
       PostgreSQL on ``127.0.0.1:5432``). ``utils.db_target`` calls it local
       because it classifies a URL, not a machine. There is no signal in a
       loopback URL that distinguishes the two cases, so this tool declines the
       whole class rather than guess.
    3. **Non-SQLite.** The default is SQLite only. ``--allow-production`` lifts
       this for a genuinely remote database, which is the same deliberate
       operator opt-in ``scripts/migrate.py`` offers.

    None of these refusals consults the environment.
    """
    target = require_writable_target(
        'the development schema bootstrap',
        explicit_url=url or None,
        allow_production=allow_production)

    if not _is_usable_database_url(target):
        raise UnsafeTargetError(
            '%s is not a usable database URL. A schema bootstrap needs a URL '
            'that names a scheme and a database, and for a network scheme a '
            'host as well. Refusing rather than passing it to the driver, '
            'which would only fail after the application had been imported.'
            % target.safe_identity())

    if target.is_local and not target.is_sqlite:
        raise UnsafeTargetError(
            'the development schema bootstrap does not create schema in a '
            'loopback PostgreSQL server, and %s is one. This is refused even '
            'with --allow-production: a loopback address is ambiguous in a way '
            'a flag cannot resolve, because this deployment publishes '
            'PostgreSQL on 127.0.0.1:5432 on the application host, so the same '
            'URL that is a scratch server on a laptop is the production '
            'database on the server.\n'
            '  For local work, use a scratch file:  '
            '--database-url sqlite:///dev.db\n'
            '  For any real database, use the migration runner: '
            'scripts/migrate.py upgrade'
            % target.safe_identity())

    if not target.is_sqlite and not allow_production:
        raise UnsafeTargetError(
            'the development schema bootstrap only creates schema in a SQLite '
            'development database, and %s is not one.\n'
            '  For local work, use a scratch file:  '
            '--database-url sqlite:///dev.db\n'
            '  A real database is migrated, not bootstrapped: use '
            'scripts/migrate.py.'
            % target.safe_identity())

    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=None,
                        help='override DATABASE_URL for this run')
    parser.add_argument('--allow-production', action='store_true',
                        help='permit a genuinely REMOTE non-SQLite database. '
                             'Does NOT unlock a loopback PostgreSQL host '
                             '(refused unconditionally), nor an unusable '
                             'URL; use scripts/migrate.py for real databases')
    args = parser.parse_args()

    url = args.database_url or os.environ.get('DATABASE_URL') or ''

    # ── Authorization ────────────────────────────────────────────────────────
    # This is the first thing the script does with the target, and it happens
    # before the schema-guard bypass below, before `app` is imported, and
    # therefore before any engine, connection or DDL. An UnsafeTargetError
    # here terminates the command; it is not caught and downgraded.
    try:
        target = _require_development_target(
            url, allow_production=args.allow_production)
    except UnsafeTargetError as exc:
        sys.exit(
            "REFUSING to create tables: %s\n"
            "  The application does not create its own schema; use the "
            "migrations in migrates/.\n"
            "  For a local scratch database, pass --database-url sqlite:///"
            "<path>." % exc)

    # Printed before anything is opened. safe_identity() drops the password and
    # any query string, so this line is safe to paste into a bug report.
    print("Bootstrap schema")
    print("  target : %s" % target.safe_identity())
    print("  action : create declared tables that do not exist yet")
    print()

    # The guard would refuse to import against a database that does not yet
    # have the declared schema — which is exactly the case being repaired.
    # This is a startup-schema-parity bypass, not write authorization: the
    # target was already authorized above, and this setting grants nothing.
    os.environ.setdefault('SKIP_SCHEMA_GUARD', '1')
    os.environ['DATABASE_URL'] = url

    from app import app
    from models import db
    from sqlalchemy import inspect

    with app.app_context():
        # A FRESH inspector after create_all. SQLAlchemy's Inspector caches
        # its reflection, so reusing the "before" instance would report zero
        # created tables and hide what the run actually did.
        before = set(inspect(db.engine).get_table_names())
        db.create_all()
        after = set(inspect(db.engine).get_table_names())

    created = sorted(after - before)
    print("  created : %d table(s)" % len(created))
    for name in created:
        print("      + %s" % name)
    if not created:
        print("      (schema already current — nothing to do)")
    print()
    print("Done. Verify with:  python -m utils.schema_guard")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())