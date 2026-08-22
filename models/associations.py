"""Association (many-to-many) tables: watchlist, wishlist, viewed."""
from datetime import datetime

from models.base import db

user_watchlist = db.Table(
    'user_watchlist',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_added', db.DateTime, default=datetime.utcnow),
    db.Column('priority', db.String(10), default='medium'),  # 'high', 'medium', 'low'
)

user_wishlist = db.Table(
    'user_wishlist',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_added', db.DateTime, default=datetime.utcnow),
    db.Column('priority', db.String(10), default='medium'),  # 'high', 'medium', 'low'
)

user_viewed = db.Table(
    'user_viewed',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_viewed', db.DateTime, default=datetime.utcnow),
    db.Column('rating', db.Integer)  # Optional rating from 1-10
)
