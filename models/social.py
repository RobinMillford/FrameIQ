"""Social engagement models: diary, tags, likes, comments."""
from datetime import datetime

from flask_login import current_user

from models.base import db


class DiaryEntry(db.Model):
    """User's diary of watched movies/shows (supports re-watches)"""
    __tablename__ = 'diary_entry'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, db.ForeignKey('media_item.id'), nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    watched_date = db.Column(db.Date, nullable=False, index=True)
    rating = db.Column(db.Float)  # Optional rating 0.5 to 5.0
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=True, index=True)
    is_rewatch = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('diary_entries', lazy='dynamic'))
    media = db.relationship('MediaItem', backref=db.backref('diary_entries', lazy='dynamic'))
    review = db.relationship('Review', backref=db.backref('diary_entry', uselist=False))

    # Constraints
    __table_args__ = (
        db.CheckConstraint('rating IS NULL OR (rating >= 0.5 AND rating <= 5.0)', name='valid_diary_rating'),
    )

    def __init__(self, **kwargs):
        super(DiaryEntry, self).__init__(**kwargs)

    def to_dict(self):
        """Convert diary entry to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user': {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture
            },
            'media': {
                'id': self.media.tmdb_id,
                'title': self.media.title,
                'poster_path': self.media.poster_path,
                'media_type': self.media_type
            },
            'watched_date': self.watched_date.isoformat(),
            'rating': self.rating,
            'review_id': self.review_id,
            'is_rewatch': self.is_rewatch,
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<DiaryEntry {self.id}: {self.user.username} watched {self.media.title}>'


class Tag(db.Model):
    """Tags that can be applied to movies/TV shows (like Letterboxd)"""
    __tablename__ = 'tag'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), unique=True, nullable=False, index=True)  # lowercase, max 30 chars
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    usage_count = db.Column(db.Integer, default=0)  # For tracking popular tags

    # Relationships
    user_media_tags = db.relationship('UserMediaTag', backref='tag', cascade='all, delete-orphan', lazy='dynamic')

    def __init__(self, **kwargs):
        """Initialize Tag with keyword arguments"""
        super(Tag, self).__init__(**kwargs)

    def to_dict(self):
        """Convert tag to dictionary for JSON responses"""
        return {
            'id': self.id,
            'name': self.name,
            'usage_count': self.usage_count,
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<Tag {self.name}>'


class UserMediaTag(db.Model):
    """Junction table for user-applied tags to media (supports same tag by multiple users)"""
    __tablename__ = 'user_media_tag'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB ID, not foreign key
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    tag_id = db.Column(db.Integer, db.ForeignKey('tag.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    user = db.relationship('User', backref=db.backref('user_tags', lazy='dynamic'))

    # Constraints - each user can only tag a media with the same tag once
    __table_args__ = (
        db.UniqueConstraint('user_id', 'media_id', 'media_type', 'tag_id', name='unique_user_media_tag'),
    )

    def __init__(self, **kwargs):
        """Initialize UserMediaTag with keyword arguments"""
        super(UserMediaTag, self).__init__(**kwargs)

    def to_dict(self):
        """Convert user media tag to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'media_id': self.media_id,
            'media_type': self.media_type,
            'tag': self.tag.to_dict(),
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<UserMediaTag User:{self.user_id} Media:{self.media_id} Tag:{self.tag.name}>'


class MediaLike(db.Model):
    """User likes (hearts) on movies/TV shows - quick appreciation without review"""
    __tablename__ = 'media_like'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB ID
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    user = db.relationship('User', backref=db.backref('media_likes', lazy='dynamic'))

    # Constraints - one like per user per media
    __table_args__ = (
        db.UniqueConstraint('user_id', 'media_id', 'media_type', name='unique_user_media_like'),
    )

    def __init__(self, **kwargs):
        """Initialize MediaLike with keyword arguments"""
        super(MediaLike, self).__init__(**kwargs)

    def to_dict(self):
        """Convert like to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'media_id': self.media_id,
            'media_type': self.media_type,
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<MediaLike User:{self.user_id} Media:{self.media_id}>'


class MediaComment(db.Model):
    """General comments/discussion on movie/TV pages (not tied to reviews)"""
    __tablename__ = 'media_comment'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB ID
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False)

    # Relationships
    user = db.relationship('User', backref=db.backref('media_comments', lazy='dynamic'))

    def __init__(self, **kwargs):
        """Initialize MediaComment with keyword arguments"""
        super(MediaComment, self).__init__(**kwargs)

    def to_dict(self):
        """Convert comment to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user': {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture
            },
            'media_id': self.media_id,
            'media_type': self.media_type,
            'content': self.content,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'is_author': current_user.is_authenticated and self.user_id == current_user.id
        }

    def __repr__(self):
        return f'<MediaComment {self.id} by User:{self.user_id} on {self.media_type}:{self.media_id}>'
