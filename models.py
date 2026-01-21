# File: models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, current_user
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Association table for many-to-many relationship between users and movies in watchlist
user_watchlist = db.Table('user_watchlist',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_added', db.DateTime, default=datetime.utcnow)
)

# Association table for many-to-many relationship between users and movies in wishlist
user_wishlist = db.Table('user_wishlist',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_added', db.DateTime, default=datetime.utcnow)
)

# Table for user viewing history
user_viewed = db.Table('user_viewed',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_viewed', db.DateTime, default=datetime.utcnow),
    db.Column('rating', db.Integer)  # Optional rating from 1-10
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)  # Increased length to accommodate longer hashes
    date_joined = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Profile information
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    bio = db.Column(db.Text)
    profile_picture = db.Column(db.String(200))
    
    # Relationships
    watchlist = db.relationship('MediaItem', secondary=user_watchlist, lazy='subquery',
        backref=db.backref('watchlisted_by', lazy=True))
    wishlist = db.relationship('MediaItem', secondary=user_wishlist, lazy='subquery',
        backref=db.backref('wishlisted_by', lazy=True))
    viewed_media = db.relationship('MediaItem', secondary=user_viewed, lazy='subquery',
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
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def __repr__(self):
        return f'<User {self.username}>'

class MediaItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tmdb_id = db.Column(db.Integer, unique=True, nullable=False)
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    title = db.Column(db.String(200), nullable=False)
    release_date = db.Column(db.Date)
    poster_path = db.Column(db.String(200))
    genres = db.Column(db.String(200))  # Comma-separated genre labels
    overview = db.Column(db.Text)
    rating = db.Column(db.Float)
    
    def __repr__(self):
        return f'<MediaItem {self.title}>'


class Review(db.Model):
    """User-generated movie/TV show reviews"""
    __tablename__ = 'review'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, db.ForeignKey('media_item.id'), nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    
    # Review content
    content = db.Column(db.Text)  # Optional review text
    rating = db.Column(db.Float, nullable=False)  # 0.5 to 5.0 stars
    watched_date = db.Column(db.Date)  # When they watched it
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    contains_spoilers = db.Column(db.Boolean, default=False)
    is_deleted = db.Column(db.Boolean, default=False)  # Soft delete for statistics
    
    # Social metrics (denormalized for performance)
    likes_count = db.Column(db.Integer, default=0)
    comments_count = db.Column(db.Integer, default=0)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('user_reviews', lazy='dynamic'))
    media = db.relationship('MediaItem', backref=db.backref('user_reviews', lazy='dynamic'))
    likes = db.relationship('ReviewLike', backref='review', cascade='all, delete-orphan', lazy='dynamic')
    comments = db.relationship('ReviewComment', backref='review', cascade='all, delete-orphan', lazy='dynamic')
    
    # Constraints
    __table_args__ = (
        db.UniqueConstraint('user_id', 'media_id', 'media_type', name='unique_user_media_review'),
        db.CheckConstraint('rating >= 0.5 AND rating <= 5.0', name='valid_rating'),
    )
    
    def to_dict(self):
        """Convert review to dictionary for JSON responses"""
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
            'media_id': self.media_id,
            'media_type': self.media_type,
            'content': self.content,
            'rating': self.rating,
            'watched_date': self.watched_date.isoformat() if self.watched_date else None,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'contains_spoilers': self.contains_spoilers,
            'likes_count': self.likes_count,
            'comments_count': self.comments_count,
            'is_author_self': current_user.is_authenticated and self.user_id == current_user.id
        }
    
    def __repr__(self):
        return f'<Review {self.id} by {self.user.username} for {self.media.title}>'


class ReviewLike(db.Model):
    """Likes on reviews"""
    __tablename__ = 'review_like'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('review_likes', lazy='dynamic'))
    
    # Ensure one like per user per review
    __table_args__ = (
        db.UniqueConstraint('user_id', 'review_id', name='unique_user_review_like'),
    )
    
    def __repr__(self):
        return f'<ReviewLike {self.user.username} on Review {self.review_id}>'


class ReviewComment(db.Model):
    """Comments on reviews"""
    __tablename__ = 'review_comment'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('review_comment.id'), nullable=True, index=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_deleted = db.Column(db.Boolean, default=False)  # Soft delete
    
    # Relationships
    user = db.relationship('User', backref=db.backref('review_comments', lazy='dynamic'))
    replies = db.relationship('ReviewComment', backref=db.backref('parent_comment', remote_side=[id]), lazy='dynamic', cascade='all, delete-orphan')
    
    def to_dict(self):
        """Convert comment to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user': {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture
            },
            'review_id': self.review_id,
            'parent_id': self.parent_id,
            'content': self.content,
            'created_at': self.created_at.isoformat(),
            'reply_count': self.replies.filter_by(is_deleted=False).count()
        }
    
    def __repr__(self):
        return f'<ReviewComment {self.id} by {self.user.username}>'

class UserFollow(db.Model):
    """User following relationships"""
    __tablename__ = 'user_follow'
    
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_active = db.Column(db.Boolean, default=True)  # For soft delete
    
    # Constraints
    __table_args__ = (
        db.UniqueConstraint('follower_id', 'following_id', name='unique_follower_following'),
        db.CheckConstraint('follower_id != following_id', name='no_self_follow'),
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
