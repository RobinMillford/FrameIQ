"""Persistent import source mappings (Task F7).

A mapping is the user's own durable answer to the question F6 could only ask
every time:

    "this external title is that FrameIQ title"

F6 deliberately refused to guess, so an unresolved import had exactly one
recovery: open the title in FrameIQ and re-run. That works, but it makes the
user repeat the same judgement for every file, forever. F7 stores the answer.

    Letterboxd  /film/the-matrix/            -> Movie   tmdb 600000000
    TV Time      show 600100001              -> Show    tmdb 600100001

Once saved, the resolver checks this table FIRST, above any automatic title
match. A manually chosen mapping is always stronger than an inferred one — the
user looked at two candidates and picked one, which is information the
heuristics do not have.

Design notes
------------
**One focused table.** Import bookkeeping is deliberately NOT bolted onto
``DiaryEntry``, ``TVEpisodeWatch``, ``MediaItem`` or ``TVShowProgress``. Those
models are canonical history and TV correctness depends on their shape (F1–F4);
adding source columns to them would put portability metadata inside the
records that statistics and progress maths read.

**Keyed by resolution identity, not record identity.** A TV Time show has ONE
mapping, not one per episode, so mapping a show resolves all of its episodes
(bulk resolution for free). That is why the unique key uses the show identity
for TV and the film identity for movies — see
:func:`api.imports.resolve.resolution_key`.

**User-owned.** Scoped by ``user_id`` throughout. Two users who import the
same Letterboxd export get independent mappings, because "the film I meant" is
an opinion, and one user's answer must never silently apply to another's.

**No secrets.** This stores public catalogue identifiers only: a source key,
a source title, and a TMDb id.
"""
from datetime import datetime

from models.base import db


class ImportSourceMapping(db.Model):
    """One user's durable external-title -> FrameIQ-title mapping."""

    __tablename__ = 'import_source_mapping'

    id = db.Column(db.Integer, primary_key=True)
    # Owner. Scoped on every read and write; never client-selectable.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False,
                        index=True)

    # Which adapter produced the identity, e.g. 'letterboxd' | 'tvtime'.
    source = db.Column(db.String(32), nullable=False)
    # 'movie' | 'tv' — part of the key because the same external id can name a
    # film in one service and a show in another.
    media_type = db.Column(db.String(20), nullable=False)
    # The adapter's stable identity for the thing being mapped, e.g.
    # 'the-matrix' or '600100001'. NOT a watch-history record id and NOT a
    # FrameIQ id — see docs/import-format.md §Source identity.
    source_key = db.Column(db.String(255), nullable=False)
    # Denormalised so the mappings UI can show what was mapped without a join
    # into media_item (and so the row stays readable if that row is deleted).
    source_title = db.Column(db.String(300))

    # The FrameIQ title the user chose. A plain integer rather than a
    # relationship: a mapping to a deleted media row must still be readable so
    # it can be reported stale and cleaned up, which a relationship with a
    # lazy load cannot promise.
    media_id = db.Column(db.Integer, db.ForeignKey('media_item.id'),
                         nullable=False, index=True)
    media_tmdb_id = db.Column(db.Integer, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    user = db.relationship(
        'User',
        backref=db.backref('import_source_mappings',
                           lazy='dynamic',
                           cascade='all, delete-orphan'),
    )

    __table_args__ = (
        # At most ONE active mapping per (user, source, media_type, source_key).
        # Two contradictory rows for the same external title would make
        # resolution order-dependent, which is the one thing this table exists
        # to prevent.
        db.UniqueConstraint('user_id', 'source', 'media_type', 'source_key',
                            name='unique_user_source_mapping'),
        # The resolver's hot path: "all my mappings for this source".
        db.Index('idx_import_mapping_user_source', 'user_id', 'source'),
    )

    def __repr__(self):
        return ('<ImportSourceMapping %s %s:%s -> media %s>'
                % (self.user_id, self.source, self.source_key, self.media_id))