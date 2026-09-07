"""User-created custom list models (collaborative lists, categories, analytics)."""
from datetime import datetime

from flask_login import current_user

from models.base import db


def prefetch_list_data(user_lists):
    """Batch-load related data for a collection of UserList rows.

    Attaches private _prefetched_* attributes that UserList.to_dict() reads,
    replacing per-list lazy queries (items.count(), collaborators.all(),
    list_categories.all(), user, analytics) with a fixed number of grouped
    queries. Safe no-op behavior for empty input.
    """
    lists = [user_list for user_list in user_lists if user_list is not None]
    if not lists:
        return

    list_ids = [user_list.id for user_list in lists]

    # 1. Item counts (one grouped query).
    count_rows = db.session.query(
        UserListItem.list_id,
        db.func.count(UserListItem.id).label('cnt'),
    ).filter(
        UserListItem.list_id.in_(list_ids)
    ).group_by(UserListItem.list_id).all()
    counts = {row.list_id: row.cnt for row in count_rows}

    # 2. Collaborators (one query).
    collab_rows = ListCollaborator.query.filter(
        ListCollaborator.list_id.in_(list_ids)
    ).all()
    collabs_by_list = {}
    for collab in collab_rows:
        collabs_by_list.setdefault(collab.list_id, []).append(collab)

    # 3. Categories via the junction table (two queries).
    lc_rows = UserListCategory.query.filter(
        UserListCategory.list_id.in_(list_ids)
    ).all()
    category_ids = {lc.category_id for lc in lc_rows}
    categories_by_id = {}
    if category_ids:
        for cat in ListCategory.query.filter(ListCategory.id.in_(category_ids)).all():
            categories_by_id[cat.id] = cat
    cats_by_list = {}
    for lc in lc_rows:
        cat = categories_by_id.get(lc.category_id)
        if cat is not None:
            cats_by_list.setdefault(lc.list_id, []).append(lc)
            # Stash resolved categories under the junction row for _category_dicts().
            lc._resolved_category = cat
        else:
            # If the category row was deleted under us, skip it rather than
            # crashing in _category_dicts().
            pass

    # 4. Owners (one query).
    user_ids = {user_list.user_id for user_list in lists}
    users_by_id = {}
    if user_ids:
        from models.user import User  # local import avoids import cycles
        for user in User.query.filter(User.id.in_(user_ids)).all():
            users_by_id[user.id] = user

    # 5. Analytics (one query).
    analytics_rows = ListAnalytics.query.filter(
        ListAnalytics.list_id.in_(list_ids)
    ).all()
    analytics_by_list = {a.list_id: a for a in analytics_rows}

    for user_list in lists:
        user_list._item_count = counts.get(user_list.id, 0)
        user_list._prefetched_collaborators = collabs_by_list.get(user_list.id, [])
        user_list._prefetched_categories = cats_by_list.get(user_list.id, [])
        user_list._prefetched_user = users_by_id.get(user_list.user_id)
        user_list._prefetched_analytics = analytics_by_list.get(user_list.id)


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

    def _collaborator_dicts(self):
        """Collaborator dicts, using batch-prefetched rows when available."""
        objs = getattr(self, '_prefetched_collaborators', None)
        if objs is None and hasattr(self, 'collaborators'):
            objs = self.collaborators.all()
        out = []
        for c in (objs or []):
            if c is None:
                continue
            u = getattr(c, 'user', None)
            if u is None:
                continue
            out.append({
                'id': c.id,
                'list_id': c.list_id,
                'user': {
                    'id': u.id,
                    'username': u.username,
                    'profile_picture': getattr(u, 'profile_picture', None)
                },
                'role': c.role,
                'added_at': c.added_at.isoformat()
            })
        return out

    def _category_dicts(self):
        """Category dicts, using batch-prefetched rows when available."""
        objs = getattr(self, '_prefetched_categories', None)
        if objs is None and hasattr(self, 'list_categories'):
            objs = self.list_categories.all()
        dicts = []
        for lc in (objs or []):
            cat = getattr(lc, '_resolved_category', None)
            if cat is None:
                cat = lc.category
            dicts.append(cat.to_dict())
        return dicts

    def _analytics_dict(self):
        """Analytics dict, using batch-prefetched row when available."""
        if not hasattr(self, 'analytics'):
            return None
        analytics = getattr(self, '_prefetched_analytics', None)
        if analytics is None:
            analytics = self.analytics
        return analytics.to_dict() if analytics else None

    def to_dict(self):
        """Convert list to dictionary for JSON responses"""
        user = getattr(self, '_prefetched_user', None)
        if user is None:
            user = self.user

        item_count = getattr(self, '_item_count', None)
        if item_count is None:
            item_count = self.items.count()

        user_dict = None
        if user is not None:
            user_dict = {
                'id': user.id,
                'username': user.username,
                'profile_picture': user.profile_picture
            }

        return {
            'id': self.id,
            'user': user_dict,
            'title': self.title,
            'description': self.description,
            'is_public': self.is_public,
            'cover_image': self.cover_image,  # Week 2
            'slug': self.slug,  # Week 2
            'collaborators': self._collaborator_dicts(),  # Week 2b
            'categories': self._category_dicts(),  # Week 2b
            'analytics': self._analytics_dict(),  # Week 2b
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'item_count': item_count,
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
