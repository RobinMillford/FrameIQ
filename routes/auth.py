# File: routes/auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, Review
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from extensions import limiter
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
_DUMMY_HASH = generate_password_hash('_timing_dummy_')

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _valid_image_bytes(stream):
    """Verify magic bytes match a real image — extension alone is not enough."""
    header = stream.read(12)
    stream.seek(0)
    if header[:4] == b'\x89PNG':
        return True
    if header[:3] == b'\xff\xd8\xff':
        return True
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return True
    return False

@auth.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 20 per hour")
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
            last_name=request.form.get('last_name', ''),
            email_verified=False,
        )
        new_user.set_password(password)

        try:
            db.session.add(new_user)
            db.session.commit()
            # Send verification email (non-blocking — failure doesn't break registration)
            from utils.email import send_verification_email
            sent = send_verification_email(new_user)
            if sent:
                flash('Registration successful! Check your email to verify your account.')
            else:
                flash('Registration successful! (Email verification unavailable — contact support if needed.)')
            return redirect(url_for('auth.login'))
        except Exception as e:
            db.session.rollback()
            logger.error("Error during user registration: %s", e)
            flash('An error occurred during registration. Please try again.')
            return redirect(url_for('auth.register'))
    
    return render_template('register.html')

@auth.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 50 per hour")
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember_me = 'remember_me' in request.form  # Check if "Remember Me" is checked
        
        try:
            user = User.query.filter_by(username=username).first()
            candidate_hash = user.password_hash if user else _DUMMY_HASH
            password_ok = check_password_hash(candidate_hash, password)

            if user and password_ok:
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

def _build_recommendations(unique_user_items, max_total, max_per_item):
    """
    Build recommendation list from TMDB without calling fetch_poster.
    TMDB recommendation results already include poster_path — use it directly,
    eliminating one extra API call per recommendation (was 54 calls per page load).
    """
    from api.tmdb_client import fetch_tmdb_recommendations
    import random
    random.shuffle(unique_user_items)

    user_tmdb_ids = {item.tmdb_id for item in unique_user_items}
    recommendations = []
    processed_ids: set = set()

    for item in unique_user_items[:15]:
        if len(recommendations) >= max_total:
            break
        try:
            tmdb_recs = fetch_tmdb_recommendations(item.tmdb_id, item.media_type == 'movie')
            added = 0
            for rec in tmdb_recs:
                if rec['id'] in processed_ids or rec['id'] in user_tmdb_ids:
                    continue
                poster_path = rec.get('poster_path')
                poster = (f"https://image.tmdb.org/t/p/w500{poster_path}"
                          if poster_path else "https://via.placeholder.com/500x750?text=No+Image")
                recommendations.append({
                    'id': rec['id'],
                    'title': rec.get('title') or rec.get('name', 'Unknown'),
                    'poster': poster,
                    'media_type': item.media_type,
                    'release_date': rec.get('release_date') or rec.get('first_air_date', 'N/A'),
                    'based_on': item.title,
                })
                processed_ids.add(rec['id'])
                added += 1
                if added >= max_per_item or len(recommendations) >= max_total:
                    break
        except Exception as e:
            logger.warning("Recommendations fetch failed for %s: %s", item.title, e)

    return recommendations


@auth.route('/profile/recommendations')
@login_required
@limiter.limit("5 per minute")
def profile_recommendations():
    user_items = list(current_user.watchlist) + list(current_user.wishlist) + list(current_user.viewed_media)
    seen: set = set()
    unique_user_items = [i for i in user_items
                         if not (i.tmdb_id in seen or seen.add(i.tmdb_id))]

    final_recommendations = _build_recommendations(unique_user_items, max_total=18, max_per_item=3)

    user_watchlist_ids = {(i.tmdb_id, i.media_type) for i in current_user.watchlist}
    user_wishlist_ids = {(i.tmdb_id, i.media_type) for i in current_user.wishlist}
    user_viewed_ids = {(i.tmdb_id, i.media_type) for i in current_user.viewed_media}

    return render_template('profile_recommendations.html',
                           recommendations=final_recommendations,
                           user_watchlist_ids=user_watchlist_ids,
                           user_wishlist_ids=user_wishlist_ids,
                           user_viewed_ids=user_viewed_ids)

@auth.route('/profile/recommendations-preview')
@login_required
@limiter.limit("10 per minute")
def profile_recommendations_preview():
    user_items = list(current_user.watchlist) + list(current_user.wishlist) + list(current_user.viewed_media)
    seen: set = set()
    unique_user_items = [i for i in user_items
                         if not (i.tmdb_id in seen or seen.add(i.tmdb_id))]

    final_recommendations = _build_recommendations(unique_user_items, max_total=6, max_per_item=2)
    return jsonify({'recommendations': final_recommendations})

@auth.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        current_user.first_name = request.form.get('first_name', '').strip()[:50]
        current_user.last_name = request.form.get('last_name', '').strip()[:50]
        current_user.bio = request.form.get('bio', '').strip()[:500]
        
        # Handle profile picture upload
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename != '' and allowed_file(file.filename) and _valid_image_bytes(file.stream):
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


# ── Email verification ────────────────────────────────────────────────────────

@auth.route('/verify-email/<token>')
def verify_email(token):
    logger.info("verify_email: start")
    from utils.email import verify_email_token
    logger.info("verify_email: decoding token")
    email = verify_email_token(token)
    logger.info("verify_email: token decoded → %s", bool(email))
    if not email:
        flash('Verification link is invalid or has expired.')
        return redirect(url_for('auth.login'))
    try:
        logger.info("verify_email: querying user")
        user = User.query.filter_by(email=email).first()
        logger.info("verify_email: user found → %s", bool(user))
        if not user:
            flash('User not found.')
            return redirect(url_for('auth.login'))
        if user.email_verified:
            flash('Email already verified. You can log in.')
        else:
            logger.info("verify_email: committing")
            user.email_verified = True
            db.session.commit()
            logger.info("verify_email: commit done")
            flash('Email verified! You can now log in.')
    except Exception as exc:
        db.session.rollback()
        logger.error("verify_email: error — %s", exc)
        flash('An error occurred. Please try again.')
    return redirect(url_for('auth.login'))


@auth.route('/resend-verification')
@login_required
def resend_verification():
    if current_user.email_verified:
        flash('Your email is already verified.')
        return redirect(url_for('auth.profile'))
    from utils.email import send_verification_email
    send_verification_email(current_user)
    flash('Verification email sent. Check your inbox.')
    return redirect(url_for('auth.profile'))


# ── Password reset ────────────────────────────────────────────────────────────

@auth.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 10 per hour")
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        # Always show success message — prevents user enumeration
        if user:
            from utils.email import send_password_reset_email
            send_password_reset_email(user)
        flash('If that email is registered you will receive a reset link shortly.')
        return redirect(url_for('auth.login'))
    return render_template('forgot_password.html')


@auth.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def reset_password(token):
    from utils.email import verify_reset_token
    email = verify_reset_token(token)
    if not email:
        flash('Reset link is invalid or has expired.')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('User not found.')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')

        if len(password) < 8:
            flash('Password must be at least 8 characters.')
            return render_template('reset_password.html', token=token)
        if not re.search(r'[A-Z]', password):
            flash('Password must contain at least one uppercase letter.')
            return render_template('reset_password.html', token=token)
        if not re.search(r'[a-z]', password):
            flash('Password must contain at least one lowercase letter.')
            return render_template('reset_password.html', token=token)
        if not re.search(r'\d', password):
            flash('Password must contain at least one digit.')
            return render_template('reset_password.html', token=token)
        if password != confirm:
            flash('Passwords do not match.')
            return render_template('reset_password.html', token=token)

        user.set_password(password)
        db.session.commit()
        flash('Password reset successful. You can now log in.')
        return redirect(url_for('auth.login'))

    return render_template('reset_password.html', token=token)