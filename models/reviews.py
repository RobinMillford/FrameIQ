"""Review models: reviews, likes, comments, helpful votes."""
from datetime import datetime

from flask_login import current_user

from models.base import db


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

    def to_dict(self, include_user=True):
        d = {
            'id': self.id,
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
            'is_author_self': current_user.is_authenticated and self.user_id == current_user.id,
        }
        if include_user:
            d['user'] = {
                'id': self.user.id,
                'username': self.user.username,
                'profile_picture': self.user.profile_picture,
            }
        return d

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
    replies = db.relationship(
        'ReviewComment',
        backref=db.backref('parent_comment', remote_side=[id]),
        lazy='dynamic', cascade='all, delete-orphan')

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
