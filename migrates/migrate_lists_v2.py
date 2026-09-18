"""
Migration (Feature 9 — Lists V2): ordering, ranking mode, social engagement.

Changes (all idempotent, non-destructive):
  1. user_list.list_type        — VARCHAR(20) NOT NULL DEFAULT 'unranked'
                                  (existing lists default to unranked)
  2. user_list_item.position    — backfilled to deterministic 1..N positions
                                  (current canonical order: position ASC,
                                  added_at ASC, id ASC) and set NOT NULL
  3. idx_user_list_item_list_position — composite index on (list_id, position)
  4. list_like                  — new table (unique user_id+list_id)
  5. list_comment               — new table

Operational sequence (see README / Makefile):
  1. build image
  2. run migration container:  python migrates/migrate_lists_v2.py
  3. verify migration (re-run prints status without altering anything)
  4. bring web up
  5. schema guard / smoke checks

Re-running is safe: every step checks current catalog state first.
"""
import os

from sqlalchemy import (MetaData, Table, Column, Integer, Text, Boolean,
                        DateTime, create_engine, func as sa_func, text)
from sqlalchemy import inspect as sa_inspect
from dotenv import load_dotenv

load_dotenv()

# Must match models/lists.py.
LIST_LIKE_SQL = """
CREATE TABLE IF NOT EXISTS list_like (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    list_id INTEGER NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL
)
"""

LIST_COMMENT_SQL = """
CREATE TABLE IF NOT EXISTS list_comment (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    list_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    is_deleted BOOLEAN DEFAULT FALSE NOT NULL
)
"""


def _column_exists(conn, table, column):
    return any(c["name"] == column for c in sa_inspect(conn).get_columns(table))


def _table_exists(conn, table):
    return table in sa_inspect(conn).get_table_names()


def _index_exists(conn, name, table=None):
    tables = [table] if table else sa_inspect(conn).get_table_names()
    for t in tables:
        try:
            for idx in sa_inspect(conn).get_indexes(t):
                if idx["name"] == name:
                    return True
        except Exception:
            continue
    return False


def _column_nullable(conn, table, column):
    for c in sa_inspect(conn).get_columns(table):
        if c["name"] == column:
            return bool(c.get("nullable", True))
    return True


def _create_social_tables_generic(conn):
    """Dialect-portable DDL for list_like/list_comment (non-Postgres)."""
    md = MetaData()
    Table('list_like', md,
          Column('id', Integer, primary_key=True),
          Column('user_id', Integer, nullable=False),
          Column('list_id', Integer, nullable=False),
          Column('created_at', DateTime, server_default=sa_func.now(),
                 nullable=False))
    Table('list_comment', md,
          Column('id', Integer, primary_key=True),
          Column('user_id', Integer, nullable=False),
          Column('list_id', Integer, nullable=False),
          Column('content', Text, nullable=False),
          Column('created_at', DateTime, server_default=sa_func.now(),
                 nullable=False),
          Column('updated_at', DateTime, server_default=sa_func.now(),
                 nullable=False),
          Column('is_deleted', Boolean, nullable=False,
                 server_default=text('FALSE')))
    md.create_all(conn, checkfirst=True)


def _migrate_like_comment_tables(conn, engine):
    """Create list_like + list_comment and their indexes (idempotent)."""
    # ── list_like ──────────────────────────────────────────────────────
    if not _table_exists(conn, "list_like"):
        if engine.dialect.name == "postgresql":
            conn.execute(text(LIST_LIKE_SQL))
        else:
            _create_social_tables_generic(conn)
        print("  list_like created")
    else:
        print("  list_like already exists — nothing to do")

    if not _index_exists(conn, "uq_user_list_like"):
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_list_like "
            "ON list_like (user_id, list_id)"
        ))
        print("  uq_user_list_like created")
    else:
        print("  uq_user_list_like already exists — nothing to do")

    if not _index_exists(conn, "idx_list_like_list"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_list_like_list "
            "ON list_like (list_id)"
        ))
        print("  idx_list_like_list created")
    else:
        print("  idx_list_like_list already exists — nothing to do")

    # ── list_comment ─────────────────────────────────────────────────
    if not _table_exists(conn, "list_comment"):
        if engine.dialect.name == "postgresql":
            conn.execute(text(LIST_COMMENT_SQL))
        else:
            _create_social_tables_generic(conn)
        print("  list_comment created")
    else:
        print("  list_comment already exists — nothing to do")

    if not _index_exists(conn, "idx_list_comment_list"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_list_comment_list "
            "ON list_comment (list_id, created_at)"
        ))
        print("  idx_list_comment_list created")
    else:
        print("  idx_list_comment_list already exists — nothing to do")


def run_migration(database_url=None):
    """Run the migration. Pass database_url explicitly in tests; production
    falls back to DATABASE_URL from the environment."""
    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL not set; pass database_url explicitly or export it")

    engine = create_engine(database_url)
    with engine.connect() as conn:
        # Migrations must fail loudly — no try/except framing here.
        with conn.begin():
            # ── 1. user_list.list_type ────────────────────────────────
            if _table_exists(conn, "user_list"):
                if not _column_exists(conn, "user_list", "list_type"):
                    conn.execute(text(
                        "ALTER TABLE user_list "
                        "ADD COLUMN list_type VARCHAR(20) NOT NULL "
                        "DEFAULT 'unranked'"
                    ))
                    print("  user_list.list_type added (default 'unranked')")
                else:
                    print("  user_list.list_type already exists — "
                          "nothing to do")
            else:
                print("  user_list table missing — list_type skipped")

            # ── 2. user_list_item.position backfill + NOT NULL ────────
            # Deterministic backfill: rows with NULL (or colliding)
            # positions get 1..N in canonical order (position, added_at,
            # id). Existing relative order is preserved; no arbitrary
            # reordering.
            if _table_exists(conn, "user_list_item"):
                if _column_exists(conn, "user_list_item", "position"):
                    conn.execute(text(
                        "WITH ranked AS ("
                        "  SELECT id, ROW_NUMBER() OVER ("
                        "    PARTITION BY list_id "
                        "    ORDER BY position ASC NULLS LAST, "
                        "             added_at ASC, id ASC"
                        "  ) AS rn "
                        "  FROM user_list_item"
                        ") "
                        "UPDATE user_list_item SET position = ranked.rn "
                        "FROM ranked WHERE user_list_item.id = ranked.id"
                    ))
                    print("  user_list_item.position backfilled "
                          "(deterministic 1..N per list)")

                    if _column_nullable(conn, "user_list_item", "position"):
                        if engine.dialect.name == "postgresql":
                            conn.execute(text(
                                "ALTER TABLE user_list_item "
                                "ALTER COLUMN position SET NOT NULL"
                            ))
                            print("  user_list_item.position set NOT NULL")
                        else:
                            print("  NOTE: dialect %s cannot alter "
                                  "nullability; position stays nullable on "
                                  "this database (create_all enforces "
                                  "NOT NULL for fresh tables)"
                                  % engine.dialect.name)
                    else:
                        print("  user_list_item.position already NOT NULL — "
                              "nothing to do")
                else:
                    print("  WARNING: user_list_item.position column "
                          "missing — skipped")
            else:
                print("  user_list_item table missing — position skipped")

            # ── 3. composite ordering index ───────────────────────────
            if _table_exists(conn, "user_list_item"):
                if not _index_exists(conn, "idx_user_list_item_list_position"):
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS "
                        "idx_user_list_item_list_position "
                        "ON user_list_item (list_id, position)"
                    ))
                    print("  idx_user_list_item_list_position created")
                else:
                    print("  idx_user_list_item_list_position already exists "
                          "— nothing to do")
            else:
                print("  user_list_item table missing — index skipped")

            # ── 4/5. list_like + list_comment ─────────────────────────
            _migrate_like_comment_tables(conn, engine)

            print("Migration complete.")


if __name__ == '__main__':
    run_migration()
