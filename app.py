# ============================================================================
# FrameIQ - Movie & TV Show Tracking Platform
# ============================================================================

# Standard Library Imports
import os
import urllib.parse
from datetime import timedelta

# Third-Party Imports
from flask import Flask
from flask_login import LoginManager
from dotenv import load_dotenv

# Local Imports - Models
from models import db, User

# Local Imports - Core Routes
from routes.auth import auth
from routes.main import main
from routes.details import details

# Local Imports - Feature Routes
from routes.chat import chat
from routes.oauth import oauth

# Local Imports - Content Management
from routes.reviews import reviews
from routes.lists import lists
from routes.diary import diary

# Local Imports - Social Features
from routes.social import social
from routes.analytics import analytics
from routes.trending import trending
from routes.activity_feed import activity_feed
from routes.friends_activity import friends_activity
from routes.profile_enhancements import profile_enhancements

# Local Imports - Letterboxd-Style Features
from routes.tags import tags_bp
from routes.likes import likes_bp
from routes.media_comments import media_comments_bp
from routes.watchlist_priorities import priorities_bp
from routes.lists_advanced import lists_advanced  # Week 2b

# Local Imports - AI Agent
from src.api.flask_integration import agent_chat
from routes.reviews_enhanced import reviews_enhanced_bp

# Week 3: Film Stats & Analytics
from routes.stats import stats_bp


# ============================================================================
# Environment & Configuration
# ============================================================================

load_dotenv()


# ============================================================================
# Flask Application Setup
# ============================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")


# ============================================================================
# OAuth Configuration
# ============================================================================

app.config['GOOGLE_CLIENT_ID'] = os.getenv("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv("GOOGLE_CLIENT_SECRET")


# ============================================================================
# Database Configuration
# ============================================================================

database_url = os.getenv("DATABASE_URL")

if database_url:
    if database_url.startswith("postgresql://"):
        is_local = 'RENDER' not in os.environ
        parsed = urllib.parse.urlparse(database_url)
        
        if not is_local:
            if not parsed.query:
                database_url += "?sslmode=require"
            elif "sslmode" not in parsed.query:
                database_url += "&sslmode=require"
        else:
            if "sslmode=require" in database_url:
                database_url = database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

print(f"Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")


# ============================================================================
# Database Initialization
# ============================================================================

db.init_app(app)


# ============================================================================
# Authentication Setup
# ============================================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'  # type: ignore
login_manager.login_message = 'Please log in to access this page.'  # type: ignore
login_manager.remember_cookie_duration = timedelta(days=30)  # type: ignore



@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login"""
    return User.query.get(int(user_id))


# ============================================================================
# Blueprint Registration
# ============================================================================

# Core
app.register_blueprint(auth)
app.register_blueprint(main)
app.register_blueprint(details)

# Features
app.register_blueprint(chat)
app.register_blueprint(oauth)

# Content
app.register_blueprint(reviews)
app.register_blueprint(lists)
app.register_blueprint(diary)

# Social
app.register_blueprint(social)
app.register_blueprint(analytics)
app.register_blueprint(trending)
app.register_blueprint(activity_feed)
app.register_blueprint(friends_activity)
app.register_blueprint(profile_enhancements)

# Letterboxd-Style
app.register_blueprint(tags_bp)
app.register_blueprint(likes_bp)
app.register_blueprint(media_comments_bp)
app.register_blueprint(priorities_bp)
app.register_blueprint(lists_advanced)
app.register_blueprint(reviews_enhanced_bp)

# Week 4: User Discovery
from routes.user_discovery import user_discovery
app.register_blueprint(user_discovery)

# Week 3: Stats & Analytics
app.register_blueprint(stats_bp)

# Enhanced Features: Popular with Friends
from routes.popular_with_friends import popular_bp
app.register_blueprint(popular_bp)

# Enhanced Features: More Like This Recommendations
from routes.recommendations import recommendations_bp
app.register_blueprint(recommendations_bp)

# AI
app.register_blueprint(agent_chat)


# ============================================================================
# Route Handlers
# ============================================================================

@app.route('/health')
def health_check():
    """Health check endpoint for deployment monitoring"""
    return {'status': 'ok'}, 200


# ============================================================================
# Database Table Creation
# ============================================================================

with app.app_context():
    print(f"Database engine: {db.engine}")
    try:
        db.create_all()
        print("✅ Database tables created successfully")
    except Exception as e:
        print(f"❌ Error creating database tables: {e}")


# ============================================================================
# Application Entry Point
# ============================================================================

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)

