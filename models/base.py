"""Shared SQLAlchemy instance — single source of truth for the db object."""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
