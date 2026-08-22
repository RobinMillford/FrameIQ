"""User account and follow-relationship models."""
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from models.base import db
from models.associations import user_watchlist, user_wishlist, user_viewed


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)  # Increased length to accommodate longer hashes
    date_joined = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)

    # Profile information
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    bio = db.Column(db.Text)
    profile_picture = db.Column(db.String(200))

    # Relationships
    watchlist = db.relationship(
        'MediaItem', secondary=user_watchlist, lazy='select',
        backref=db.backref('watchlisted_by', lazy=True))
    wishlist = db.relationship(
        'MediaItem', secondary=user_wishlist, lazy='select',
        backref=db.backref('wishlisted_by', lazy=True))
    viewed_media = db.relationship(
        'MediaItem', secondary=user_viewed, lazy='select',
        backref=db.backref('viewed_by', lazy=True))

    # Following relationships
    followers = db.relationship(
        'UserFollow',
        foreign_keys='UserFollow.following_id',
        backref=db.backref('following_user', lazy='joined'),
        lazy='dynamic',
        cascade='all, delete-orphan'
    )
    following = db.relationship(
        'UserFollow',
        foreign_keys='UserFollow.follower_id',
        backref=db.backref('follower_user', lazy='joined'),
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    # Social statistics (cached for performance)
    total_reviews = db.Column(db.Integer, default=0)
    total_movies_watched = db.Column(db.Integer, default=0)
    followers_count = db.Column(db.Integer, default=0)
    following_count = db.Column(db.Integer, default=0)

    def __init__(self, **kwargs):
        super(User, self).__init__(**kwargs)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


class UserFollow(db.Model):
    """User following relationships"""
    __tablename__ = 'user_follow'

    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_active = db.Column(db.Boolean, default=True)  # For soft delete

    __table_args__ = (
        db.UniqueConstraint('follower_id', 'following_id', name='unique_follower_following'),
        db.CheckConstraint('follower_id != following_id', name='no_self_follow'),
        # Composite indexes for the two hot query patterns in social.py
        db.Index('idx_uf_following_active', 'following_id', 'is_active'),
        db.Index('idx_uf_follower_active',  'follower_id',  'is_active'),
    )

    def to_dict(self):
        """Convert follow relationship to dictionary for JSON responses"""
        return {
            'id': self.id,
            'follower': {
                'id': self.follower_user.id,
                'username': self.follower_user.username,
                'profile_picture': self.follower_user.profile_picture
            },
            'following': {
                'id': self.following_user.id,
                'username': self.following_user.username,
                'profile_picture': self.following_user.profile_picture
            },
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<UserFollow {self.follower_id} -> {self.following_id}>'
