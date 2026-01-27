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
    db.Column('date_added', db.DateTime, default=datetime.utcnow),
    db.Column('priority', db.String(10), default='medium')  # 'high', 'medium', 'low'
)

# Association table for many-to-many relationship between users and movies in wishlist
user_wishlist = db.Table('user_wishlist',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_added', db.DateTime, default=datetime.utcnow),
    db.Column('priority', db.String(10), default='medium')  # 'high', 'medium', 'low'
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
    
    def __init__(self, **kwargs):
        super(User, self).__init__(**kwargs)
    
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
    
    def __init__(self, **kwargs):
        super(MediaItem, self).__init__(**kwargs)
    
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
    title = db.Column(db.String(200))  # Optional review title (Week 3)
    contains_spoilers = db.Column(db.Boolean, default=False)  # Week 3: spoiler flag
    rewatch = db.Column(db.Boolean, default=False)  # Week 3: is this a rewatch?
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False)  # Soft delete for statistics
    
    # Social metrics (denormalized for performance)
    likes_count = db.Column(db.Integer, default=0)  # Week 3: ReviewLikes count
    helpful_count = db.Column(db.Integer, default=0)  # Week 3: helpful votes
    not_helpful_count = db.Column(db.Integer, default=0)  # Week 3: not helpful votes
    comments_count = db.Column(db.Integer, default=0)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('user_reviews', lazy='dynamic'))
    media = db.relationship('MediaItem', backref=db.backref('user_reviews', lazy='dynamic'))
    likes = db.relationship('ReviewLike', backref='review', cascade='all, delete-orphan', lazy='dynamic')
    comments = db.relationship('ReviewComment', backref='review', cascade='all, delete-orphan', lazy='dynamic')
    
    def __init__(self, **kwargs):
        super(Review, self).__init__(**kwargs)
    
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
    
    def __init__(self, **kwargs):
        super(ReviewLike, self).__init__(**kwargs)
    
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
    
    def __init__(self, **kwargs):
        super(ReviewComment, self).__init__(**kwargs)
    
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


class ReviewHelpful(db.Model):
    """'Was this review helpful?' votes"""
    __tablename__ = 'review_helpful'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=False, index=True)
    is_helpful = db.Column(db.Boolean, nullable=False)  # True = helpful, False = not helpful
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('review_helpful_votes', lazy='dynamic'))
    review = db.relationship('Review', backref=db.backref('helpful_votes', lazy='dynamic'))
    
    def __init__(self, **kwargs):
        super(ReviewHelpful, self).__init__(**kwargs)
    
    # Constraints
    __table_args__ = (
        db.UniqueConstraint('user_id', 'review_id', name='unique_user_review_helpful'),
    )
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'review_id': self.review_id,
            'is_helpful': self.is_helpful,
            'created_at': self.created_at.isoformat()
        }
    
    def __repr__(self):
        return f'<ReviewHelpful User:{self.user_id} Review:{self.review_id} Helpful:{self.is_helpful}>'


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


class UserList(db.Model):
    """User-created custom lists of movies/TV shows"""
    __tablename__ = 'user_list'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    is_public = db.Column(db.Boolean, default=True)
    cover_image = db.Column(db.String(500))  # Week 2: Cover image URL
    slug = db.Column(db.String(250), unique=True, index=True)  # Week 2: Shareable URL slug
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('lists', lazy='dynamic'))
    items = db.relationship('UserListItem', backref='list', cascade='all, delete-orphan', lazy='dynamic')
    
    def to_dict(self):
        """Convert list to dictionary for JSON responses"""
        # Week 2b: Get collaborators and categories
        collaborators = []
        if hasattr(self, 'collaborators'):
            collaborators = [c.to_dict() for c in self.collaborators.all()]
        
        categories = []
        if hasattr(self, 'list_categories'):
            categories = [lc.category.to_dict() for lc in self.list_categories.all()]
        
        analytics_data = None
        if hasattr(self, 'analytics') and self.analytics:
            analytics_data = self.analytics.to_dict()
        
        return {
            'id': self.id,
            'user': {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture
            },
            'title': self.title,
            'description': self.description,
            'is_public': self.is_public,
            'cover_image': self.cover_image,  # Week 2
            'slug': self.slug,  # Week 2
            'collaborators': collaborators,  # Week 2b
            'categories': categories,  # Week 2b
            'analytics': analytics_data,  # Week 2b
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'item_count': self.items.count(),
            'is_owner': current_user.is_authenticated and self.user_id == current_user.id
        }
    
    def __repr__(self):
        return f'<UserList {self.id}: {self.title}>'


class ListCollaborator(db.Model):
    """Week 2b: Collaborators on a list (multiple people can edit)"""
    __tablename__ = 'list_collaborator'
    
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    role = db.Column(db.String(20), default='editor')  # 'owner', 'editor', 'viewer'
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'))  # Who added this collaborator
    
    # Relationships
    list = db.relationship('UserList', backref=db.backref('collaborators', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('collaborated_lists', lazy='dynamic'))
    inviter = db.relationship('User', foreign_keys=[added_by])
    
    def __init__(self, **kwargs):
        super(ListCollaborator, self).__init__(**kwargs)
    
    # Constraints
    __table_args__ = (
        db.UniqueConstraint('list_id', 'user_id', name='unique_list_collaborator'),
    )
    
    def to_dict(self):
        return {
            'id': self.id,
            'list_id': self.list_id,
            'user': {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture
            },
            'role': self.role,
            'added_at': self.added_at.isoformat()
        }
    
    def __repr__(self):
        return f'<ListCollaborator {self.user_id} on List {self.list_id}>'


class ListCategory(db.Model):
    """Week 2b: Categories/themes for lists (e.g., 'Best of 2024', 'Horror', 'Oscar Winners')"""
    __tablename__ = 'list_category'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    description = db.Column(db.Text)
    icon = db.Column(db.String(50))  # Font Awesome icon class
    color = db.Column(db.String(20))  # Hex color code
    usage_count = db.Column(db.Integer, default=0)  # How many lists use this category
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'icon': self.icon,
            'color': self.color,
            'usage_count': self.usage_count
        }
    
    def __repr__(self):
        return f'<ListCategory {self.id}: {self.name}>'


class UserListCategory(db.Model):
    """Week 2b: Junction table for lists and categories (many-to-many)"""
    __tablename__ = 'user_list_category'
    
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('list_category.id'), primary_key=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    list = db.relationship('UserList', backref=db.backref('list_categories', lazy='dynamic', cascade='all, delete-orphan'))
    category = db.relationship('ListCategory', backref=db.backref('categorized_lists', lazy='dynamic'))
    
    def __init__(self, **kwargs):
        super(UserListCategory, self).__init__(**kwargs)
    
    def __repr__(self):
        return f'<UserListCategory list={self.list_id} category={self.category_id}>'


class ListAnalytics(db.Model):
    """Week 2b: Analytics for lists (views, likes, shares)"""
    __tablename__ = 'list_analytics'
    
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), nullable=False, unique=True, index=True)
    view_count = db.Column(db.Integer, default=0)
    unique_viewers = db.Column(db.Integer, default=0)
    share_count = db.Column(db.Integer, default=0)
    fork_count = db.Column(db.Integer, default=0)  # How many times list was cloned
    last_viewed = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    list = db.relationship('UserList', backref=db.backref('analytics', uselist=False, cascade='all, delete-orphan'))
    
    def __init__(self, **kwargs):
        super(ListAnalytics, self).__init__(**kwargs)
    
    def to_dict(self):
        return {
            'list_id': self.list_id,
            'view_count': self.view_count,
            'unique_viewers': self.unique_viewers,
            'share_count': self.share_count,
            'fork_count': self.fork_count,
            'last_viewed': self.last_viewed.isoformat() if self.last_viewed else None
        }
    
    def __repr__(self):
        return f'<ListAnalytics list={self.list_id} views={self.view_count}>'


class ListView(db.Model):
    """Week 2b: Track individual list views for unique viewer counting"""
    __tablename__ = 'list_view'
    
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)  # Null for anonymous
    ip_address = db.Column(db.String(45))  # IPv4 or IPv6
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    list = db.relationship('UserList', backref=db.backref('views', lazy='dynamic'))
    user = db.relationship('User', backref=db.backref('list_views', lazy='dynamic'))
    
    def __init__(self, **kwargs):
        super(ListView, self).__init__(**kwargs)
    
    def __repr__(self):
        return f'<ListView list={self.list_id} user={self.user_id}>'


class UserListItem(db.Model):
    """Items in a user's custom list"""
    __tablename__ = 'user_list_item'
    
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, db.ForeignKey('media_item.id'), nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    position = db.Column(db.Integer)  # For ordering items in the list
    note = db.Column(db.Text)  # Optional note about why this item is in the list
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    media = db.relationship('MediaItem', backref=db.backref('list_appearances', lazy='dynamic'))
    
    # Constraints
    __table_args__ = (
        db.UniqueConstraint('list_id', 'media_id', 'media_type', name='unique_list_media'),
    )
    
    def to_dict(self):
        """Convert list item to dictionary for JSON responses"""
        return {
            'id': self.id,
            'list_id': self.list_id,
            'media': {
                'id': self.media.tmdb_id,
                'title': self.media.title,
                'poster_path': self.media.poster_path,
                'media_type': self.media_type
            },
            'position': self.position,
            'note': self.note,
            'added_at': self.added_at.isoformat()
        }
    
    def __repr__(self):
        return f'<UserListItem {self.id} in List {self.list_id}>'


class DiaryEntry(db.Model):
    """User's diary of watched movies/shows (supports re-watches)"""
    __tablename__ = 'diary_entry'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    media_id = db.Column(db.Integer, db.ForeignKey('media_item.id'), nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False)  # 'movie' or 'tv'
    watched_date = db.Column(db.Date, nullable=False, index=True)
    rating = db.Column(db.Float)  # Optional rating 0.5 to 5.0
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=True)  # Optional linked review
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


class TVShowProgress(db.Model):
    """Track user's progress through TV shows - overall show tracking"""
    __tablename__ = 'tv_show_progress'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID
    
    # Progress tracking
    total_seasons = db.Column(db.Integer, default=0)  # Total seasons in show
    watched_seasons = db.Column(db.Integer, default=0)  # Completed seasons
    total_episodes = db.Column(db.Integer, default=0)  # Total episodes in show
    watched_episodes = db.Column(db.Integer, default=0)  # Episodes watched
    
    # Status
    status = db.Column(db.String(20), default='watching')  # 'watching', 'completed', 'plan_to_watch', 'dropped'
    is_favorite = db.Column(db.Boolean, default=False)
    
    # Timestamps
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_watched = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('tv_progress', lazy='dynamic'))
    episodes = db.relationship('TVEpisodeWatch', backref='show_progress', cascade='all, delete-orphan', lazy='dynamic')
    
    # Constraints - one progress entry per user per show
    __table_args__ = (
        db.UniqueConstraint('user_id', 'show_id', name='unique_user_show_progress'),
    )
    
    def __init__(self, **kwargs):
        super(TVShowProgress, self).__init__(**kwargs)
    
    def calculate_progress_percentage(self):
        """Calculate completion percentage"""
        if self.total_episodes == 0:
            return 0
        return round((self.watched_episodes / self.total_episodes) * 100, 1)
    
    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'show_id': self.show_id,
            'total_seasons': self.total_seasons,
            'watched_seasons': self.watched_seasons,
            'total_episodes': self.total_episodes,
            'watched_episodes': self.watched_episodes,
            'progress_percentage': self.calculate_progress_percentage(),
            'status': self.status,
            'is_favorite': self.is_favorite,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'last_watched': self.last_watched.isoformat() if self.last_watched else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'created_at': self.created_at.isoformat()
        }
    
    def __repr__(self):
        return f'<TVShowProgress User:{self.user_id} Show:{self.show_id} {self.watched_episodes}/{self.total_episodes} episodes>'


class TVEpisodeWatch(db.Model):
    """Track individual episode watches"""
    __tablename__ = 'tv_episode_watch'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID
    progress_id = db.Column(db.Integer, db.ForeignKey('tv_show_progress.id'), nullable=True, index=True)
    
    # Episode identification
    season_number = db.Column(db.Integer, nullable=False)
    episode_number = db.Column(db.Integer, nullable=False)
    episode_name = db.Column(db.String(200))  # Episode title
    
    # Watch details
    watched_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    rating = db.Column(db.Float)  # Optional rating 0.5 to 5.0
    notes = db.Column(db.Text)  # User notes about episode
    is_rewatch = db.Column(db.Boolean, default=False)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref=db.backref('episode_watches', lazy='dynamic'))
    
    # Constraints
    __table_args__ = (
        db.CheckConstraint('rating IS NULL OR (rating >= 0.5 AND rating <= 5.0)', name='valid_episode_rating'),
        db.Index('idx_user_show_season_episode', 'user_id', 'show_id', 'season_number', 'episode_number'),
    )
    
    def __init__(self, **kwargs):
        super(TVEpisodeWatch, self).__init__(**kwargs)
    
    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'show_id': self.show_id,
            'season_number': self.season_number,
            'episode_number': self.episode_number,
            'episode_name': self.episode_name,
            'watched_date': self.watched_date.isoformat() if self.watched_date else None,
            'rating': self.rating,
            'notes': self.notes,
            'is_rewatch': self.is_rewatch,
            'created_at': self.created_at.isoformat()
        }
    
    def __repr__(self):
        return f'<TVEpisodeWatch User:{self.user_id} S{self.season_number}E{self.episode_number}>'


class UpcomingEpisode(db.Model):
    """Calendar of upcoming episodes for shows user is tracking"""
    __tablename__ = 'upcoming_episode'
    
    id = db.Column(db.Integer, primary_key=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID
    show_name = db.Column(db.String(200), nullable=False)
    poster_path = db.Column(db.String(200))
    
    # Episode details
    season_number = db.Column(db.Integer, nullable=False)
    episode_number = db.Column(db.Integer, nullable=False)
    episode_name = db.Column(db.String(200))
    episode_overview = db.Column(db.Text)
    
    # Air date
    air_date = db.Column(db.Date, nullable=False, index=True)
    air_time = db.Column(db.String(10))  # e.g., "21:00"
    
    # Metadata
    runtime = db.Column(db.Integer)  # Episode runtime in minutes
    still_path = db.Column(db.String(200))  # Episode screenshot
    
    # Tracking
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Constraints - one entry per episode
    __table_args__ = (
        db.UniqueConstraint('show_id', 'season_number', 'episode_number', name='unique_episode'),
        db.Index('idx_air_date', 'air_date'),
    )
    
    def __init__(self, **kwargs):
        super(UpcomingEpisode, self).__init__(**kwargs)
    
    def days_until_air(self):
        """Calculate days until episode airs"""
        if self.air_date:
            delta = self.air_date - datetime.utcnow().date()
            return delta.days
        return None
    
    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'show_id': self.show_id,
            'show_name': self.show_name,
            'poster_path': self.poster_path,
            'season_number': self.season_number,
            'episode_number': self.episode_number,
            'episode_name': self.episode_name,
            'episode_overview': self.episode_overview,
            'air_date': self.air_date.isoformat() if self.air_date else None,
            'air_time': self.air_time,
            'runtime': self.runtime,
            'still_path': self.still_path,
            'days_until_air': self.days_until_air(),
            'created_at': self.created_at.isoformat()
        }
    
    def __repr__(self):
        return f'<UpcomingEpisode {self.show_name} S{self.season_number}E{self.episode_number} on {self.air_date}>'
