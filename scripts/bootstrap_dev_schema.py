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
* Refuses to run against a production-looking target unless
  ``--allow-production`` is passed. The refusal is based on the URL, so it
  works before any connection is opened.
* Prints the resolved target with the password hidden, so a mistyped
  ``DATABASE_URL`` is visible before DDL is attempted.
* Requires ``SKIP_SCHEMA_GUARD=1``: on a not-yet-created database the startup
  guard would otherwise refuse to import.

Usage:
    python scripts/bootstrap_dev_schema.py
    python scripts/bootstrap_dev_schema.py --database-url sqlite:///dev.db

Equivalent to ``make dev-schema``.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _looks_production(url):
    """True for anything that is not obviously a local/scratch target."""
    lowered = (url or '').lower()
    if lowered.startswith('sqlite:'):
        return False
    if not lowered:
        return True
    for marker in ('neon.tech', 'neon.build', 'amazonaws.com', 'rds.amazonaws',
                   'supabase.co', 'azure.com', 'cloudsql'):
        if marker in lowered:
            return True
    # Any remote network host is treated as production-ish by default.
    return '@' in lowered or '://' in lowered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=None,
                        help='override DATABASE_URL for this run')
    parser.add_argument('--allow-production', action='store_true',
                        help='proceed even if the target looks like production')
    args = parser.parse_args()

    url = args.database_url or os.environ.get('DATABASE_URL') or ''
    if not url:
        sys.exit("DATABASE_URL is not set; pass --database-url instead.")

    if _looks_production(url) and not args.allow_production:
        sys.exit(
            "REFUSING to create tables on a production-looking target.\n"
            "  The application does not create its own schema; use the "
            "migrations in migrates/.\n"
            "  For a local scratch database, pass --database-url, or "
            "--allow-production if you truly mean it.")

    # Printed before anything is opened, with the password removed.
    from sqlalchemy.engine import make_url
    try:
        shown = make_url(url).render_as_string(hide_password=True)
    except Exception:
        shown = url.split('@')[0] + '@***'
    print("Bootstrap schema")
    print("  target : %s" % shown)
    print("  action : create declared tables that do not exist yet")
    print()

    # The guard would refuse to import against a database that does not yet
    # have the declared schema — which is exactly the case being repaired.
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