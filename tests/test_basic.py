"""
Basic smoke tests to ensure core imports work
"""
import pytest
import os


def test_models_import():
    """Test that models can be imported without app initialization"""
    try:
        from models import (
            User, Review, MediaItem, DiaryEntry,
            TVShowProgress, TVEpisodeWatch, UpcomingEpisode
        )
        
        # Check that models have expected attributes
        assert hasattr(User, 'id')
        assert hasattr(User, 'username')
        assert hasattr(MediaItem, 'media_id')
        assert hasattr(Review, 'user_id')
        assert hasattr(DiaryEntry, 'media_id')
        assert hasattr(TVShowProgress, 'show_id')
        assert hasattr(TVEpisodeWatch, 'episode_number')
        assert hasattr(UpcomingEpisode, 'air_date')
    except Exception as e:
        pytest.fail(f"Models import failed: {e}")


@pytest.mark.skipif(
    not os.getenv('GROQ_API_KEY'),
    reason="Requires GROQ_API_KEY to initialize AI agents"
)
def test_app_imports():
    """Test that app can be imported (requires API keys)"""
    try:
        from app import app, db
        assert app is not None
        assert db is not None
        assert app.config.get('SECRET_KEY') is not None
        assert app.config.get('SQLALCHEMY_DATABASE_URI') is not None
    except Exception as e:
        pytest.fail(f"App import failed: {e}")


def test_basic_routes_import():
    """Test that route modules can be imported"""
    try:
        from routes import main, auth, social
        assert main is not None
        assert auth is not None
        assert social is not None
    except Exception as e:
        pytest.fail(f"Routes import failed: {e}")

