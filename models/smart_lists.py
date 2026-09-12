"""Smart Lists (Feature 05) — saved dynamic queries over the user's data.

A Smart List is NOT a manually maintained list of items. It stores only the
RULES (scope + filters + sort); results are evaluated on every request by
api/smart_lists.py. Nothing about the matching titles is persisted here.

The filter configuration is a single validated JSON document — never one
column per future filter. Validation lives in api/smart_lists.py
(validate_config) and runs on every write path; unknown filter names and
out-of-range values are rejected server-side.
"""
from datetime import datetime

from models.base import db


class SmartList(db.Model):
    """A saved, dynamic filter (query) over the owner's FrameIQ data."""
    __tablename__ = 'smart_list'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False,
                        index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    # 'watchlist' | 'diary' | 'tracked_tv' | 'all' (validated in the engine)
    scope = db.Column(db.String(20), nullable=False)

    # JSON document with the active filters; validated before every write.
    filters_json = db.Column(db.Text, nullable=False, default='{}')

    # One of the engine's supported sort keys.
    sort = db.Column(db.String(20), nullable=False, default='date_added')

    # Private by default. A read-only public mode may come later; nothing in
    # v1 exposes Smart Lists outside the owner's session.
    is_public = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    user = db.relationship('User',
                           backref=db.backref('smart_lists', lazy='dynamic',
                                              cascade='all, delete-orphan'))

    __table_args__ = (
        db.Index('idx_smart_list_user_created', 'user_id', 'created_at'),
    )

    def __init__(self, **kwargs):
        super(SmartList, self).__init__(**kwargs)

    @property
    def filters(self):
        """Parsed filter dict (empty dict when unparseable)."""
        import json
        try:
            data = json.loads(self.filters_json or '{}')
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @filters.setter
    def filters(self, value):
        import json
        self.filters_json = json.dumps(value or {}, sort_keys=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'scope': self.scope,
            'filters': self.filters,
            'sort': self.sort,
            'is_public': bool(self.is_public),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<SmartList {self.id}: {self.name}>'
