from flask import Blueprint, request, redirect, url_for, session, jsonify, current_app, request
from flask_login import login_user
from models import db, User
from authlib.integrations.flask_client import OAuth
import os

oauth = Blueprint('oauth', __name__)

# Initialize OAuth
oauth_app = OAuth()

@oauth.record_once
def record_oauth(setup_state):
    """Initialize OAuth with the app configuration"""
    app = setup_state.app
    oauth_app.init_app(app)
    # Register Google OAuth
    oauth_app.register(
        name='google',
        client_id=app.config.get('GOOGLE_CLIENT_ID'),
        client_secret=app.config.get('GOOGLE_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={
            'scope': 'openid email profile'
        }
    )

@oauth.route('/google/login')
def google_login():
    """Initiate Google OAuth login"""
    google = oauth_app.create_client('google')
    if not google:
        return redirect(url_for('auth.login'))
    # Generate redirect URI manually to ensure it matches Google Console configuration
    redirect_uri = request.url_root.rstrip('/') + url_for('oauth.google_callback')
    return google.authorize_redirect(redirect_uri)

@oauth.route('/google/callback')
def google_callback():
    """Handle Google OAuth callback"""
    try:
        google = oauth_app.create_client('google')
        if not google:
            return redirect(url_for('auth.login'))
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        
        if not user_info:
            return redirect(url_for('auth.login'))
        
        # Extract user information
        email = user_info.get('email')
        name = user_info.get('name', '')
        first_name = user_info.get('given_name', '')
        last_name = user_info.get('family_name', '')
        picture = user_info.get('picture', '')
        
        # Check if user already exists
        user = User.query.filter_by(email=email).first()
        
        if not user:
            # Create new user
            # Generate a unique username from email
            username = email.split('@')[0]
            # Ensure username is unique
            counter = 1
            original_username = username
            while User.query.filter_by(username=username).first():
                username = f"{original_username}_{counter}"
                counter += 1
            
            user = User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
                profile_picture=picture
            )
            # Set a random password for OAuth users (they won't use it)
            user.set_password(os.urandom(24).hex())
            db.session.add(user)
            db.session.commit()
        
        # Log in the user
        login_user(user, remember=True)
        
        # Redirect to home page
        return redirect(url_for('main.index'))
        
    except Exception as e:
        print(f"Error during Google OAuth: {e}")
        return redirect(url_for('auth.login'))