# File: app.py
from flask import Flask
from flask_login import LoginManager
from dotenv import load_dotenv
import os
from datetime import timedelta
import urllib.parse

# Load environment variables
load_dotenv()

# Create Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")

# Google OAuth Configuration
app.config['GOOGLE_CLIENT_ID'] = os.getenv("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv("GOOGLE_CLIENT_SECRET")

# Configure database with environment-aware SSL support
database_url = os.getenv("DATABASE_URL")
if database_url:
    # Handle PostgreSQL SSL requirement based on environment
    if database_url.startswith("postgresql://"):
        # Check if we're running locally or on Render
        is_local = 'RENDER' not in os.environ
        
        # Parse the URL
        parsed = urllib.parse.urlparse(database_url)
        
        if not is_local:
            # On Render, we need SSL
            if not parsed.query:
                # Add SSL mode requirement for Render PostgreSQL
                database_url += "?sslmode=require"
            elif "sslmode" not in parsed.query:
                database_url += "&sslmode=require"
        else:
            # For local development, remove SSL requirement if present
            if "sslmode=require" in database_url:
                database_url = database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
    
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Add additional database configuration for better connection handling
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

# Debug: Print the database URI to verify it's correct
print(f"Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'

# Configure remember me cookie duration (30 days)
login_manager.remember_cookie_duration = timedelta(days=30)

# Import and register blueprints
from routes.main import main
from routes.chat import chat
from routes.details import details
from routes.auth import auth
from routes.oauth import oauth
from src.api.flask_integration import agent_chat  # NEW: LangGraph agent system

app.register_blueprint(auth)
app.register_blueprint(main)
app.register_blueprint(chat)
app.register_blueprint(details)
app.register_blueprint(oauth)
app.register_blueprint(agent_chat)  # NEW: Register agent endpoints

# Initialize database after blueprints to avoid circular imports
from models import db, User
db.init_app(app)

# Health check endpoint for Render
@app.route('/health')
def health_check():
    """Simple health check endpoint that responds immediately"""
    return {'status': 'ok'}, 200

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Create tables only if they don't exist
with app.app_context():
    # Debug: Print the actual database engine being used
    print(f"Database engine: {db.engine}")
    try:
        db.create_all()
        print("Database tables created successfully")
    except Exception as e:
        print(f"Error creating database tables: {e}")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)