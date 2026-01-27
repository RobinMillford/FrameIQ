"""
Basic smoke tests to ensure core imports work
"""
import pytest


def test_imports():
    """Test that core modules can be imported"""
    try:
        from app import app, db
        from models import User, Movie, TVShowProgress
        assert app is not None
        assert db is not None
    except Exception as e:
        pytest.fail(f"Import failed: {e}")


def test_app_config():
    """Test that Flask app is configured"""
    from app import app
    assert app.config.get('SECRET_KEY') is not None
    assert app.config.get('SQLALCHEMY_DATABASE_URI') is not None


def test_models_exist():
    """Test that database models are defined"""
    from models import (
        User, Movie, Review, Watchlist, Wishlist,
        TVShowProgress, TVEpisodeWatch, UpcomingEpisode
    )
    
    # Check that models have expected attributes
    assert hasattr(User, 'id')
    assert hasattr(User, 'username')
    assert hasattr(TVShowProgress, 'show_id')
    assert hasattr(TVEpisodeWatch, 'episode_number')
    assert hasattr(UpcomingEpisode, 'air_date')
