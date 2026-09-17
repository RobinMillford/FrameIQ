"""Year-in-Review share authorization records (Feature #8, Phase 9).

A share is EXPLICITLY created by an authenticated user (never automatic)
and grants public, revocable access to exactly one (user, year) recap.

Design notes (§3/§4/§14/§15):
  - The public URL carries an opaque, cryptographically random token
    (secrets.token_urlsafe, 256-bit entropy). Nothing — no user ID, no
    year, no statistics — is encoded into the token.
  - Only a SHA-256 HASH of the token is persisted (token_hash, unique).
    The raw token exists once: in the creation response. A database leak
    therefore cannot expose working share links.
  - At most ONE ACTIVE share per (user_id, year): a partial unique index
    on unrevoked rows backs the documented regeneration semantics
    (creating a share for a year that already has one revokes the old
    token and issues a fresh one — old links die immediately).
  - No statistics payload is stored: the public page rebuilds the recap
    through the canonical deterministic builder at view time (live
    semantics, documented in the share route). Revocation is instant.
"""
from datetime import datetime

from sqlalchemy import text

from models.base import db


class YearInReviewShare(db.Model):
    __tablename__ = "year_in_review_share"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False)
    # SHA-256 hex digest of the opaque URL token (64 chars). The raw
    # token is never stored, logged, or derivable from this value.
    token_hash = db.Column(db.String(64), nullable=False,
                           unique=True, index=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        # One ACTIVE share per (user, year); revoked rows free the slot
        # for regeneration. Partial indexes work on SQLite and Postgres.
        db.Index(
            "uq_yir_share_active_user_year",
            "user_id", "year",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
