"""Migration ledger — the record of which controlled migrations have run (Task F8).

Why this table exists
---------------------
FrameIQ shipped roughly thirty migration scripts with **no version table**. Every
script was individually idempotent and inspected the live catalog, which made
each one safe to re-run — but it meant nothing could answer:

* which migrations had been applied?
* when did each one run, and for how long?
* had an applied migration's file changed since it ran?
* what should run next?
* had a migration failed halfway?

The historical answer was "we cannot know". That remains true for the past; this
table makes it true going forward.

What it deliberately does NOT do
--------------------------------
It does not reconstruct history. Production's pre-F8 state is adopted as a single
distinguished **baseline record** (``kind='baseline'``), which asserts only that
the schema was verified at adoption time. It does not claim that each historical
script ran, because nobody recorded that. Fabricating a row per script with a
plausible timestamp would be worse than an honest blank: it would make the ledger
actively lie about provenance.

What is stored
--------------
Migration provenance only. No credentials, no connection strings, no environment
variables, no user or request data. The columns below are the minimum that
makes a deployment auditable and safe to repeat.
"""
from datetime import datetime, timezone

from models.base import db

# A row describing a normal forward migration.
KIND_MIGRATION = 'migration'
# A row asserting "the schema was verified on this date; prior history is not
# individually recorded". Written exactly once by explicit adoption.
KIND_BASELINE = 'baseline'

CHECKSUM_LENGTH = 64  # hex sha256


class SchemaMigration(db.Model):
    """One controlled migration version and the fact it was applied."""

    __tablename__ = 'schema_migrations'

    id = db.Column(db.Integer, primary_key=True)

    # Stable, human-readable identifier: "0001_canonical_watched_reconcile".
    # Assigned by the registry, never derived from the filesystem, so renaming a
    # file cannot silently renumber history.
    version = db.Column(db.String(128), nullable=False)

    # sha256 of the migration source file, hex. Recorded so that editing an
    # already-applied migration is detected instead of silently diverging from
    # what actually ran.
    checksum = db.Column(db.String(CHECKSUM_LENGTH), nullable=False)

    # 'migration' | 'baseline' — distinguishes real forward migrations from the
    # one-time legacy adoption record.
    kind = db.Column(db.String(16), nullable=False, default=KIND_MIGRATION)

    # UTC, set by the runner. Never a database DEFAULT, because the ledger's
    # timestamps are an operational fact rather than part of the schema.
    applied_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc))

    # Wall-clock duration in milliseconds, so a slow migration is visible in the
    # ledger rather than only in logs.
    execution_ms = db.Column(db.Integer, nullable=False, default=0)

    # Free text for the baseline row: what was verified, and by which procedure.
    # Null for ordinary migrations.
    note = db.Column(db.Text, nullable=True)

    __table_args__ = (
        # One row per version. The unique constraint is the enforcement point:
        # the runner checks this too, but a race between two runners must fail
        # in the database rather than in application code.
        db.UniqueConstraint('version', name='uq_schema_migrations_version'),
    )

    def __repr__(self):
        return '<SchemaMigration %s %s @%s>' % (
            self.version, self.checksum[:12] if self.checksum else '-',
            self.applied_at)
