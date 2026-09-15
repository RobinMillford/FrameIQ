"""Director capture models (Feature #6, Phase 10).

Durable local director evidence so TasteProfile.director_affinity can be
computed offline (api/taste_profile.py) and For You can tag candidates
(api/for_you.py) without any request-time TMDb credits call.

Representation (smallest durable form that supports the roadmap):
  - Director: one row per person, keyed by the STABLE TMDb person id
    (tmdb_person_id, unique) so display-name changes never fork a person.
  - MediaDirector: unique (media_item_id, director_id) association —
    a title may have several directors; re-running enrichment must never
    duplicate a pair.
  - MediaItem.directors_enriched_at (declared in models/media.py):
    distinguishes "not yet enriched" (NULL) from "enriched, no director
    found" (set, zero associations) — never an ambiguous empty list.

TV semantics: TMDb's TV credits aggregate EPISODE-level directing, which
is not a series-level director concept. Enrichment therefore captures
MOVIES only; TV director evidence is deliberately absent (documented in
scripts/enrich_directors.py) and must not be fabricated from episode data.

This data is derived cache state, NOT user data: deleting it is always
safe and recomputable from the external source.
"""
from datetime import datetime

from models.base import db


class Director(db.Model):
    """A director identity, stable across display-name changes."""

    __tablename__ = 'director'

    id = db.Column(db.Integer, primary_key=True)
    # Stable external identity — TMDb person id. Unique so a renamed
    # person updates this row instead of creating a duplicate.
    tmdb_person_id = db.Column(db.Integer, unique=True, nullable=False,
                               index=True)
    name = db.Column(db.String(200), nullable=False)  # display name
    source = db.Column(db.String(20), nullable=False, default='tmdb')
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    media_items = db.relationship(
        'MediaDirector', back_populates='director', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Director {self.tmdb_person_id}:{self.name}>'


class MediaDirector(db.Model):
    """Which directors directed which local MediaItems (unique per pair)."""

    __tablename__ = 'media_director'
    __table_args__ = (
        db.UniqueConstraint('media_item_id', 'director_id',
                            name='uq_media_director_pair'),
    )

    id = db.Column(db.Integer, primary_key=True)
    media_item_id = db.Column(
        db.Integer, db.ForeignKey('media_item.id', ondelete='CASCADE'),
        nullable=False, index=True)
    director_id = db.Column(
        db.Integer, db.ForeignKey('director.id', ondelete='CASCADE'),
        nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    media_item = db.relationship('MediaItem', back_populates='directors')
    director = db.relationship('Director', back_populates='media_items')

    def __repr__(self):
        return (f'<MediaDirector media={self.media_item_id} '
                f'director={self.director_id}>')
