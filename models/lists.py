"""User-created custom list models (collaborative lists, categories, analytics)."""
from datetime import datetime

from flask_login import current_user

from models.base import db


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
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)

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
