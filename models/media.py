"""Core media item model (movies/TV shows cached from TMDb)."""
from models.base import db


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
    # Runtime in minutes (movies: main runtime; TV: first episode_run_time).
    # Nullable — only populated for titles hydrated after this column existed.
    runtime = db.Column(db.Integer)

    def __init__(self, **kwargs):
        super(MediaItem, self).__init__(**kwargs)

    def __repr__(self):
        return f'<MediaItem {self.title}>'
