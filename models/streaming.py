"""User streaming-service preferences (Feature 03 — My Services).

A user's selected subscription services, scoped to the region they were
chosen in. Provider identity uses TMDb's stable integer provider_id — never
the mutable display name.
"""
from datetime import datetime

from models.base import db


class UserStreamingService(db.Model):
    """A streaming service the user subscribes to (per region)."""
    __tablename__ = 'user_streaming_services'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    provider_id = db.Column(db.Integer, nullable=False)
    region = db.Column(db.String(2), nullable=False, default='US')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'provider_id', 'region',
            name='unique_user_provider_region'
        ),
        db.Index('idx_uss_user_region', 'user_id', 'region'),
    )

    def to_dict(self):
        return {
            'provider_id': self.provider_id,
            'region': self.region,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<UserStreamingService u={self.user_id} p={self.provider_id} r={self.region}>'
