# File: routes/auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, Review
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import re
import logging

# Added for Cloudinary
import cloudinary
import cloudinary.uploader
import cloudinary.api

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

auth = Blueprint('auth', __name__)

# Configuration for file uploads
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@auth.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        # Validate email format
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            flash('Please enter a valid email address')
            return redirect(url_for('auth.register'))
        
        # Validate password strength
        if len(password) < 8:
            flash('Password must be at least 8 characters long')
            return redirect(url_for('auth.register'))
        
        # Check for password complexity (at least one uppercase, one lowercase, one digit)
        if not re.search(r'[A-Z]', password):
            flash('Password must contain at least one uppercase letter')
            return redirect(url_for('auth.register'))
        
        if not re.search(r'[a-z]', password):
            flash('Password must contain at least one lowercase letter')
            return redirect(url_for('auth.register'))
        
        if not re.search(r'\d', password):
            flash('Password must contain at least one digit')
            return redirect(url_for('auth.register'))
        
        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('auth.register'))
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered')
            return redirect(url_for('auth.register'))
        
        # Check if passwords match
        if password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('auth.register'))
        
        # Create new user
        new_user = User(
            username=username,
            email=email,
            first_name=request.form.get('first_name', ''),
            last_name=request.form.get('last_name', '')
        )
        new_user.set_password(password)
        
        try:
            db.session.add(new_user)
            db.session.commit()
            flash('Registration successful')
            return redirect(url_for('auth.login'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error during user registration: {e}")
            flash('An error occurred during registration. Please try again.')
            return redirect(url_for('auth.register'))
    
    return render_template('register.html')

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember_me = 'remember_me' in request.form  # Check if "Remember Me" is checked
        
        try:
            user = User.query.filter_by(username=username).first()
            
            if user and user.check_password(password):
                # Pass the remember parameter to login_user
                login_user(user, remember=remember_me)
                flash('Logged in successfully')
                logger.info(f"User {username} logged in successfully")
                return redirect(url_for('main.index'))
            else:
                flash('Invalid username or password')
                logger.warning(f"Failed login attempt for username: {username}")
        except Exception as e:
            logger.error(f"Database error during login: {e}")
            flash('An error occurred during login. Please try again.')
    
    return render_template('login.html')

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out')
    return redirect(url_for('main.index'))

@auth.route('/profile')
@login_required
def profile():
    # Fetch recent reviews for the authenticated user
    recent_reviews = current_user.user_reviews.order_by(db.desc(Review.created_at)).all()
    return render_template('profile.html', reviews=recent_reviews)

@auth.route('/profile/recommendations')
@login_required
def profile_recommendations():
    from api.tmdb_client import fetch_poster, fetch_tmdb_recommendations
    from routes.main import TMDB_API_KEY
    import requests
    import random
    
    # Get user's lists
    user_watchlist = current_user.watchlist
    user_wishlist = current_user.wishlist
    user_viewed = current_user.viewed_media
    
    # Combine all user items
    user_items = list(user_watchlist) + list(user_wishlist) + list(user_viewed)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_user_items = []
    for item in user_items:
        if item.tmdb_id not in seen:
            seen.add(item.tmdb_id)
            unique_user_items.append(item)
    
    # Get recommendations based on user's items
    recommendations = []
    processed_ids = set()
    
    # Shuffle the items to get a more diverse selection
    random.shuffle(unique_user_items)
    
    # Consider more items to get diverse recommendations (up to 15 items)
    items_to_consider = min(15, len(unique_user_items))
    
    # Get recommendations from TMDB for each item
    for item in unique_user_items[:items_to_consider]:
        if len(recommendations) >= 18:  # Limit total recommendations
            break
            
        try:
            # Get recommendations from TMDB
            tmdb_recs = fetch_tmdb_recommendations(item.tmdb_id, item.media_type == 'movie')
            
            # Limit the number of recommendations we take from each item to avoid over-representation
            max_recs_per_item = 3
            recs_added_for_this_item = 0
            
            for rec in tmdb_recs:
                # Skip if we already have this recommendation
                if rec['id'] in processed_ids:
                    continue
                    
                # Skip if user already has this item in any list
                user_has_item = any(user_item.tmdb_id == rec['id'] for user_item in unique_user_items)
                if user_has_item:
                    continue
                
                # Add recommendation
                poster = fetch_poster(rec['id'], item.media_type == 'movie')
                recommendations.append({
                    'id': rec['id'],
                    'title': rec['title'] if item.media_type == 'movie' else rec['name'],
                    'poster': poster,
                    'media_type': item.media_type,
                    'release_date': rec.get('release_date') if item.media_type == 'movie' else rec.get('first_air_date', 'N/A'),
                    'based_on': item.title  # What this recommendation is based on
                })
                
                processed_ids.add(rec['id'])
                recs_added_for_this_item += 1
                
                # Limit recommendations per source item
                if recs_added_for_this_item >= max_recs_per_item:
                    break
                
                # Limit total recommendations
                if len(recommendations) >= 18:
                    break
                    
        except Exception as e:
            print(f"Error fetching recommendations for {item.title}: {e}")
            continue
    
    # Remove duplicates from recommendations (in case any slipped through)
    final_recommendations = []
    seen_rec_ids = set()
    for rec in recommendations:
        if rec['id'] not in seen_rec_ids:
            seen_rec_ids.add(rec['id'])
            final_recommendations.append(rec)
    
    # Get user's lists for status indicators
    user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
    user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
    user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
    
    return render_template('profile_recommendations.html', 
                          recommendations=final_recommendations,
                          user_watchlist_ids=user_watchlist_ids,
                          user_wishlist_ids=user_wishlist_ids,
                          user_viewed_ids=user_viewed_ids)

@auth.route('/profile/recommendations-preview')
@login_required
def profile_recommendations_preview():
    from api.tmdb_client import fetch_poster, fetch_tmdb_recommendations
    import requests
    import random
    
    # Get user's lists
    user_watchlist = current_user.watchlist
    user_wishlist = current_user.wishlist
    user_viewed = current_user.viewed_media
    
    # Combine all user items
    user_items = list(user_watchlist) + list(user_wishlist) + list(user_viewed)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_user_items = []
    for item in user_items:
        if item.tmdb_id not in seen:
            seen.add(item.tmdb_id)
            unique_user_items.append(item)
    
    # Shuffle for diversity
    random.shuffle(unique_user_items)
    
    # Get recommendations based on user's items
    recommendations = []
    processed_ids = set()
    
    # Consider more items for preview (up to 8 items)
    items_to_consider = min(8, len(unique_user_items))
    
    # Get recommendations from TMDB for each item
    for item in unique_user_items[:items_to_consider]:
        if len(recommendations) >= 6:  # Limit total recommendations for preview
            break
            
        try:
            # Get recommendations from TMDB
            tmdb_recs = fetch_tmdb_recommendations(item.tmdb_id, item.media_type == 'movie')
            
            # Limit recommendations per item for preview
            recs_added_for_this_item = 0
            max_recs_per_item = 2
            
            for rec in tmdb_recs:
                # Skip if we already have this recommendation
                if rec['id'] in processed_ids:
                    continue
                    
                # Skip if user already has this item in any list
                user_has_item = any(user_item.tmdb_id == rec['id'] for user_item in unique_user_items)
                if user_has_item:
                    continue
                
                # Add recommendation
                poster = fetch_poster(rec['id'], item.media_type == 'movie')
                recommendations.append({
                    'id': rec['id'],
                    'title': rec['title'] if item.media_type == 'movie' else rec['name'],
                    'poster': poster,
                    'media_type': 'movie' if item.media_type == 'movie' else 'tv',
                    'release_date': rec.get('release_date') if item.media_type == 'movie' else rec.get('first_air_date', 'N/A'),
                    'based_on': item.title  # What this recommendation is based on
                })
                
                processed_ids.add(rec['id'])
                recs_added_for_this_item += 1
                
                # Limit recommendations per source item
                if recs_added_for_this_item >= max_recs_per_item:
                    break
                
                # Limit total recommendations
                if len(recommendations) >= 6:
                    break
                    
        except Exception as e:
            print(f"Error fetching recommendations for {item.title}: {e}")
            continue
    
    # Remove duplicates (in case any slipped through)
    final_recommendations = []
    seen_rec_ids = set()
    for rec in recommendations:
        if rec['id'] not in seen_rec_ids:
            seen_rec_ids.add(rec['id'])
            final_recommendations.append(rec)
    
    return jsonify({'recommendations': final_recommendations})

@auth.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        current_user.first_name = request.form.get('first_name', '')
        current_user.last_name = request.form.get('last_name', '')
        current_user.bio = request.form.get('bio', '')
        
        # Handle profile picture upload
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename != '' and allowed_file(file.filename):
                try:
                    # Upload to Cloudinary
                    logger.info(f"Uploading profile picture for user {current_user.id}")
                    upload_result = cloudinary.uploader.upload(
                        file,
                        public_id=f"user_{current_user.id}_profile_{int(datetime.now().timestamp())}",
                        overwrite=True,
                        transformation=[
                            {'width': 300, 'height': 300, 'crop': 'fill', 'gravity': 'face'},
                            {'quality': 'auto'},
                            {'fetch_format': 'auto'}
                        ]
                    )
                    
                    # Log the upload result
                    logger.info(f"Upload successful for user {current_user.id}: {upload_result['secure_url']}")
                    
                    # Store the Cloudinary URL in the database
                    current_user.profile_picture = upload_result['secure_url']
                    
                    flash('Profile picture updated successfully')
                except Exception as e:
                    logger.error(f"Error uploading to Cloudinary for user {current_user.id}: {e}")
                    flash('Error uploading profile picture. Please try again.')
        
        try:
            db.session.commit()
            flash('Profile updated successfully')
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error updating profile: {e}")
            flash('An error occurred while updating your profile. Please try again.')
        
        return redirect(url_for('auth.profile'))
    
    return render_template('edit_profile.html')