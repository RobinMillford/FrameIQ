"""Association (many-to-many) tables: watchlist, viewed.

Historical note: a parallel ``user_wishlist`` table existed until the
Wishlist→Watchlist consolidation (migrates/migrate_remove_wishlist.py
merged its rows into user_watchlist and dropped the table).
"""
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

user_viewed = db.Table(
    'user_viewed',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('media_id', db.Integer, db.ForeignKey('media_item.id'), primary_key=True),
    db.Column('media_type', db.String(20), primary_key=True),  # 'movie' or 'tv'
    db.Column('date_viewed', db.DateTime, default=datetime.utcnow),
    db.Column('rating', db.Integer)  # Optional rating from 1-10
)
