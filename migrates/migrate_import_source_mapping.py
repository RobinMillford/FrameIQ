"""Migration: create the ``import_source_mapping`` table (Task F7).

Idempotent — safe to run multiple times. On the first run it creates the one
table F7's persistent source mappings need; on every later run (or on a fresh
database where ``db.create_all()`` already made it) it is a no-op.

Why a new table at all
----------------------
F6 refused to guess at an ambiguous or unknown external title, and its only
recovery was "open the title in FrameIQ, then re-run". That makes the user
repeat the same judgement for every file, forever. F7 stores the answer.

The bookkeeping deliberately does NOT go into ``DiaryEntry``,
``TVEpisodeWatch``, ``MediaItem`` or ``TVShowProgress``. Those are canonical
history models that F1–F4 correctness and every statistics statement depend
on; adding source-import columns to them would put portability metadata
inside the records the product reads for truth.

What it stores: public catalogue identifiers only — the adapter's source key,
the source title the user saw, and the FrameIQ ``MediaItem`` it maps to. No
credentials, no uploaded file contents, no other user's data.

Run with SKIP_SCHEMA_GUARD=1 (the guard blocks app import while the model is
declared but the table is absent):

    SKIP_SCHEMA_GUARD=1 python migrates/migrate_import_source_mapping.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

TABLE = 'import_source_mapping'


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)

        if TABLE in inspector.get_table_names():
            print("[OK] %s already exists — nothing to do." % TABLE)
            _report(inspector)
            return

        # Single source of truth for the DDL: the model's own metadata. A
        # hand-written CREATE TABLE would drift from models/import_mapping.py
        # the first time a column is added there.
        from models.import_mapping import ImportSourceMapping
        ImportSourceMapping.__table__.create(bind=db.engine, checkfirst=True)
        db.session.commit()

        print("[OK] created %s." % TABLE)
        _report(inspect(db.engine))


def _report(inspector):
    """Print the shape that now exists, so a run is self-verifying."""
    columns = [c['name'] for c in inspector.get_columns(TABLE)]
    indexes = sorted(i['name'] for i in inspector.get_indexes(TABLE)
                     if i.get('name'))
    uniques = sorted(u['name'] for u in inspector.get_unique_constraints(TABLE)
                     if u.get('name'))
    print("     columns : %s" % ", ".join(columns))
    print("     indexes : %s" % ", ".join(indexes))
    print("     unique  : %s" % (", ".join(uniques) or "(none)"))

    expected_unique = 'unique_user_source_mapping'
    if uniques and expected_unique not in uniques:
        print("[WARN] expected unique constraint %s is missing."
              % expected_unique)
    else:
        print("[OK] %s present — one mapping per "
              "(user, source, media_type, source_key)." % expected_unique)


def verify_no_data():
    """Report row count; F7 ships the table empty, never back-filled."""
    with app.app_context():
        if TABLE not in inspect(db.engine).get_table_names():
            print("[OK] %s absent — nothing to verify." % TABLE)
            return
        count = db.session.execute(
            text("SELECT COUNT(*) FROM %s" % TABLE)).scalar()
        print("[OK] %s holds %d row(s)." % (TABLE, count))


if __name__ == '__main__':
    migrate()
    verify_no_data()