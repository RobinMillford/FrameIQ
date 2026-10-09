"""Cast capture models (Feature F9).

Durable local cast evidence so statistics and TasteProfile can be computed
offline (api/statistics.py, api/taste_profile.py) without any request-time
TMDb credits call.

Representation (mirrors models/director.py exactly — same durable shapes, same
stable-identity rule, same "enriched and empty is not the same as unenriched"
marker):
  - Person: one row per person, keyed by the STABLE TMDb person id
    (tmdb_person_id, unique) so display-name changes never fork a person.
  - MediaCast: unique (media_item_id, person_id) association — a title credits
    many people; re-running enrichment must never duplicate a pair. It also
    carries the two credit attributes a title-level credits payload actually
    has: the credited `character` and the `credit_order` position.
  - MediaItem.cast_enriched_at (declared in models/media.py): distinguishes
    "not yet enriched" (NULL) from "enriched, no cast member found" (set,
    zero associations) — never an ambiguous empty list.

Credit order
------------
`credit_order` is the position in the title-level `credits.cast` array, which
TMDb returns in billing order. It is stored so a consumer can present
"top billed" without re-fetching, and it is updated in place on re-enrichment
rather than being treated as immutable.

Dual roles
----------
Uniqueness on (media_item_id, person_id) means a person credited in two roles
in one title occupies ONE row, holding the highest-billed of the two
characters. That is deliberate: making character part of the key would put a
nullable column in a unique constraint, and on PostgreSQL NULLs are distinct
inside a unique index, so the constraint would silently stop deduplicating
(precisely the trap models/notification.py documents for
uq_notification_user_episode). The top-30 billing list is the evidence this
milestone persists, and the higher-billed character is the honest choice.

Title-level credits only — the TV distinction
---------------------------------------------
For MOVIES, `credits.cast` is unambiguously the film's cast.

For TV it is different from TV *crew*, and that difference is the whole reason
the two features are shaped differently. TMDb's `/tv/{id}?append_to_response=
credits` exposes a SERIES-LEVEL `cast` array — the show's billed cast — which
is the canonical "who is in this show" answer. The same endpoint's `crew`
array aggregates EPISODE-level jobs, which is why models/director.py
deliberately refuses to persist TV directors: an aggregate of per-episode
directing is not a series-level director, and recording it would fabricate
evidence.

So F9 enriches TV CAST and continues to refuse TV CREW. The rule enforced
mechanically by scripts/enrich_cast.py is that it reads only the single
title-level `cast` array from a title-level details call, and never requests
or reads per-episode credits at all. There is no code path by which an
episode guest star could become a series credit.

Deliberately NOT persisted (F9 scope): biography, birthday, death date,
gender, known-for department. Those require one extra TMDb request per person,
which would turn a one-call-per-title batch into an N+1 fanout. When richer
person metadata is wanted it must be its own budgeted batch, not a side effect
of cast capture. `profile_url` is kept because the title-level credits payload
already carries it, so it costs nothing.

This data is derived cache state, NOT user data: deleting it is always safe and
recomputable from the external source.
"""
from datetime import datetime

from models.base import db


class Person(db.Model):
    """A cast-member identity, stable across display-name changes."""

    __tablename__ = 'person'

    id = db.Column(db.Integer, primary_key=True)
    # Stable external identity — TMDb person id. Unique so a renamed
    # person updates this row instead of creating a duplicate.
    tmdb_person_id = db.Column(db.Integer, unique=True, nullable=False,
                               index=True)
    name = db.Column(db.String(200), nullable=False)  # display name
    # Already an absolute image URL as delivered by the title-level credits
    # payload (see module docstring: no extra request is made for it).
    profile_url = db.Column(db.String(500))
    source = db.Column(db.String(20), nullable=False, default='tmdb')
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    media_items = db.relationship(
        'MediaCast', back_populates='person', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Person {self.tmdb_person_id}:{self.name}>'


class MediaCast(db.Model):
    """Which people are cast in which local MediaItems (unique per pair)."""

    __tablename__ = 'media_cast'
    __table_args__ = (
        db.UniqueConstraint('media_item_id', 'person_id',
                            name='uq_media_cast_pair'),
        # Ordered cast reads (top-billed) are always scoped to one title, so
        # the index leads with media_item_id rather than person_id (which is
        # already covered by its own index for the reverse "who is this person
        # in" direction).
        db.Index('ix_media_cast_ordered', 'media_item_id', 'credit_order'),
    )

    id = db.Column(db.Integer, primary_key=True)
    media_item_id = db.Column(
        db.Integer, db.ForeignKey('media_item.id', ondelete='CASCADE'),
        nullable=False, index=True)
    person_id = db.Column(
        db.Integer, db.ForeignKey('person.id', ondelete='CASCADE'),
        nullable=False, index=True)
    # Credit attributes carried by the title-level credits payload.
    character = db.Column(db.String(200))
    credit_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    media_item = db.relationship('MediaItem', back_populates='cast_members')
    person = db.relationship('Person', back_populates='media_items')

    def __repr__(self):
        return (f'<MediaCast media={self.media_item_id} '
                f'person={self.person_id} order={self.credit_order}>')