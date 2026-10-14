"""RETIRED — use the registered migration instead (Feature F22).

This script used to merge the ``user_wishlist`` rows into ``user_watchlist``
and then drop the legacy table outright. It is retired and now does nothing.

Why it was retired
------------------
It was a SECOND, INDEPENDENT way to run that destructive operation, and a
strictly less safe one:

* It dropped the legacy table with **no target authorization** —
  no :func:`utils.db_target.require_writable_target` call, so running it with a
  production ``DATABASE_URL`` reached a live database through a plain module
  import.
* It carried **none** of the controls the registered migration has: no
  ``--allow-production``, no operator-verified backup, no checksum in the
  ledger, no dependency ordering.
* It merged rows **silently discarding** ``priority``/``date_added`` whenever a
  conflicting Watchlist row already existed. The registered migration refuses
  to drop the table at all when any source row cannot be preserved
  (``migrations_0002_remove_legacy_wishlist.py``), and raises with the verdict
  and the at-risk columns so an operator sees WHICH rows are in danger.

So the legacy path was not merely redundant — running it could lose data that
the supported path protects.

What to use instead
-------------------
The registered, ledger-tracked, backup-gated migration:

    python scripts/migrate.py upgrade --allow-production

It applies ``0002_remove_legacy_wishlist``. Because that migration is marked
``DESTRUCTIVE``, the runner additionally requires ``--authorize-destructive``
together with ``--backup-ref`` and ``--backup-verified-by``. See
``docs/migration-inventory.md`` and ``scripts/migrate.py --help``.

Running this file deliberately does NOT invoke the replacement. Translating a
legacy invocation into a production-authorized one would be exactly the
unsafe shortcut this retirement exists to remove: the authorization, the
backup verification, and the destructive approval all have to be chosen
deliberately by whoever performs them.
"""

# Deliberately no imports at all — not `app`, not `models`, not SQLAlchemy.
# This module must be inert on import: no application initialisation, no engine,
# no connection, no DDL/DML. tests/test_legacy_migration_guard_f22.py asserts
# this structurally with an AST check rather than by trusting this comment.

#: Exit status used for "this operation has been retired".
EXIT_RETIRED = 2

RETIREMENT_MESSAGE = """\
RETIRED: migrates/migrate_remove_wishlist.py no longer performs this migration.

It used to DROP the user_wishlist table without target authorization and
without a verified backup, and it could silently discard priority/date_added.
That operation is now owned by the registered migration
'0002_remove_legacy_wishlist', which refuses to drop the table when any source
row cannot be preserved.

Use the supported runner instead (it will NOT run automatically):

    python scripts/migrate.py upgrade --allow-production

That migration is DESTRUCTIVE and additionally requires:
    --authorize-destructive --backup-ref <snapshot> --backup-verified-by <who>

Docs: docs/migration-inventory.md ; runner help: scripts/migrate.py --help
"""


def migrate(*args, **kwargs):
    """Present so old imports fail loudly instead of silently doing nothing.

    Returning quietly would let a caller believe the consolidation had happened
    when it had not, so this raises rather than returning a falsy value.
    """
    raise NotImplementedError(
        'migrate_remove_wishlist.migrate() was removed in F22. Use '
        'scripts/migrate.py upgrade, which applies the registered '
        '0002_remove_legacy_wishlist migration under full authorization.')


if __name__ == '__main__':
    import sys
    sys.stderr.write(RETIREMENT_MESSAGE)
    raise SystemExit(EXIT_RETIRED)