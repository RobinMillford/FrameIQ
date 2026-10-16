# File: routes/auth.py
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app, session)
from flask_login import login_user, logout_user, login_required, current_user

from utils.session_auth import bump_session_version
from models import TVShowProgress, db, User, Review
from utils import username_policy
from werkzeug.security import generate_password_hash, check_password_hash
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
        
        # F35: reject usernames that could break out of the HTML/attribute
        # contexts they are rendered into. Defense in depth — the render paths
        # are already safe — but refusing to STORE such a value is the tighter
        # control. Existing accounts are untouched; this runs only on creation.
        username_problems = username_policy.username_problems(username)
        if username_problems:
            for problem in username_problems:
                flash(problem)
            return redirect(url_for('auth.register'))

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
                # F55: the versioned identity is stamped by the
                # `user_logged_in` handler registered in app.py, so every
                # login path is covered rather than just this one.
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


@auth.route('/logout', methods=['POST'])
@login_required
def logout():
    """Log out.

    F55: this used to accept GET. A state change reachable by GET can be
    triggered cross-origin — an ``<img src="/logout">`` on any page a signed-in
    user visits logs them out — and the global CSRFProtect never sees a GET, so
    POST-only is what actually brings logout under CSRF protection.

    POST is the right verb for a state change that is not idempotent in the
    user's interest, and ``CSRFProtect(app)`` (app.py) already covers it.
    """
    logout_user()
    flash('You have been logged out')
    return redirect(url_for('main.index'))


@auth.route('/profile')
@login_required
def profile():
    # Fetch recent reviews for the authenticated user
    recent_reviews = current_user.user_reviews.order_by(db.desc(Review.created_at)).all()

    # Taste DNA — canonical persisted TasteProfile presentation model
    # (Feature #6 Phase 14). Replaces the old duplicated genre-Counter over
    # recent reviews with the single canonical taste source. The client JS
    # fetches GET /api/taste-profile for the full presentation; the server
    # only passes the availability gate so anonymous-style cold start can
    # render instantly without a flash of empty section.
    from api.taste_profile import get_profile, taste_dna
    try:
        taste_dna_data = taste_dna(get_profile(current_user.id))
    except Exception:
        current_app.logger.debug("Taste DNA load failed for user %s",
                                 current_user.id, exc_info=True)
        taste_dna_data = None

    # Quick stats strip — Phase 1–3 semantics:
    #   WATCH EVENTS = movie DiaryEntry rows + TVEpisodeWatch rows
    #   TITLES       = distinct watched movie titles (diary) + distinct
    #                  TV shows with >=1 watched episode (NOT per-episode)
    #   TRACKING     = TVShowProgress rows (tracking state, never watch
    #                  events) — reported separately from watching
    from api.statistics import canonical_movies_watched
    from models import DiaryEntry, user_watchlist
    from models.tv import TVEpisodeWatch
    from sqlalchemy import func as _func

    uid = current_user.id
    diary_count = DiaryEntry.query.filter_by(user_id=uid).count()
    movie_events = DiaryEntry.query.filter_by(
        user_id=uid, media_type="movie").count()
    tv_watch_events = TVEpisodeWatch.query.filter_by(user_id=uid).count()
    movie_titles = db.session.query(_func.count(_func.distinct(
        DiaryEntry.media_id))).filter(
        DiaryEntry.user_id == uid,
        DiaryEntry.media_type == "movie").scalar() or 0
    tv_titles = db.session.query(_func.count(_func.distinct(
        TVEpisodeWatch.show_id))).filter(
        TVEpisodeWatch.user_id == uid).scalar() or 0
    tracking_count = TVShowProgress.query.filter_by(user_id=uid).count()
    watching_count = TVShowProgress.query.filter_by(
        user_id=uid, status="watching").count()
    wl_count = db.session.execute(
        user_watchlist.select().where(user_watchlist.c.user_id == uid)
    ).rowcount
    # Canonical movie watch history, counted from DiaryEntry.
    #
    # This previously counted the `user_viewed` mirror with `.rowcount` on a
    # bare SELECT, which does not return a row count: it rendered as -1 in the
    # profile stat strip. It was also wrong under the canonical-viewed policy —
    # `user_viewed` is a compatibility mirror, and unmatched legacy markers in
    # it would inflate a figure presented beside diary-derived DIARY/WATCH
    # EVENTS counters.
    viewed_count = canonical_movies_watched(uid)

    # TV progress surface (Phase 6): bounded batch reads over existing
    # TV tracking data — progress rows, titles, and last-watched
    # positions in three grouped statements. No per-show queries, no
    # TMDb calls, no duplicated progress system.
    progress_rows = (TVShowProgress.query
                     .filter_by(user_id=uid)
                     .order_by(TVShowProgress.last_watched.desc().nullslast(),
                               TVShowProgress.id.desc())
                     .limit(8)
                     .all())
    tv_progress = _build_tv_progress_rows(progress_rows)

    return render_template(
        "profile.html", reviews=recent_reviews,
        taste_dna=taste_dna_data,
        tv_progress=tv_progress,
        stats={
            "diary": diary_count,
            "watch_events": movie_events + tv_watch_events,
            "movie_titles": movie_titles,
            "tv_titles": tv_titles,
            "total_titles": movie_titles + tv_titles,
            "tv_watch_events": tv_watch_events,
            "tracking": tracking_count,
            "watching": watching_count,
            "watchlist": wl_count,
            "viewed": viewed_count,
        })


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


def _build_tv_progress_rows(progress_rows):
    """Compact TV-progress rows for the profile surface (Phase 6).

    Bounded: one batched title scan, one batched canonical-progress read and
    one batched canonical next-episode read — none of them grows with the
    episode count of the shows involved. Percentages come from the canonical
    aired-reality payload (Task F2) — the same source as /api/view-state,
    /api/tv/<id>/aired-progress and the TV detail hero, so the profile can
    never display a competing denominator. Tracking rows without canonical
    progress render a neutral note instead of a fabricated percentage.

    The "up next" label shares the canonical next-episode resolver with the
    TV-tracking API and Continue Watching (Task F3): the earlier independent
    ``max(season_number)`` / ``max(episode_number) + 1`` arithmetic could
    emit an episode that does not exist (S2E11 when the real next episode is
    S3E1, the first episode of the following season).
    """
    if not progress_rows:
        return []
    from models.media import MediaItem

    show_ids = [p.show_id for p in progress_rows]
    titles = dict(
        db.session.query(MediaItem.tmdb_id, MediaItem.title)
        .filter(MediaItem.tmdb_id.in_(show_ids)).all())

    # Task F2: percentages come from the canonical aired-reality payload
    # (one batched read — never the stored counters, which a pre-F1
    # legacy row can still carry). A tracked-but-not-started show has no
    # canonical entry and renders the neutral "no episode progress" note.
    from api.user_view_state import canonical_progress_map
    canonical_by_show = canonical_progress_map(current_user, show_ids)

    # Task F3: the next-episode label is resolved by the ONE canonical rule
    # (api.continue_watching) in a single batched call for the whole page.
    from api.continue_watching import next_episode_map
    next_by_show = next_episode_map(current_user.id, show_ids)

    rows = []
    for progress in progress_rows:
        canonical = canonical_by_show.get(progress.show_id)
        if canonical:
            percent = min(100, int(canonical['percent'] + 0.5))
        else:
            percent = None
        next_episode = next_by_show.get(progress.show_id)
        rows.append({
            "show_id": progress.show_id,
            "title": titles.get(progress.show_id, "Untitled show"),
            "status_label": (progress.status or "tracking")
            .replace("_", " ").capitalize(),
            "percent": percent,
            "next_label": (
                f"S{next_episode['season']}E{next_episode['episode']} up next"
                if next_episode else None),
        })
    return rows


@auth.route('/profile/recommendations')
@login_required
@limiter.limit("5 per minute")
def profile_recommendations():
    user_items = list(current_user.watchlist) + list(current_user.viewed_media)
    seen: set = set()
    unique_user_items = [i for i in user_items
                         if not (i.tmdb_id in seen or seen.add(i.tmdb_id))]

    final_recommendations = _build_recommendations(unique_user_items, max_total=18, max_per_item=3)

    user_watchlist_ids = {(i.tmdb_id, i.media_type) for i in current_user.watchlist}
    user_viewed_ids = {(i.tmdb_id, i.media_type) for i in current_user.viewed_media}

    return render_template('profile_recommendations.html',
                           recommendations=final_recommendations,
                           user_watchlist_ids=user_watchlist_ids,
                           user_viewed_ids=user_viewed_ids)


@auth.route('/profile/recommendations-preview')
@login_required
@limiter.limit("10 per minute")
def profile_recommendations_preview():
    user_items = list(current_user.watchlist) + list(current_user.viewed_media)
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


def _password_problem(password, confirm):
    """The first password-policy violation, or None when the pair is acceptable.

    Returning the message rather than flashing here keeps the rule in one
    place and keeps it out of the route's control flow.

    A rejected submission must NOT consume the reset token, so this runs before
    the binding re-check and before any write.
    """
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if not re.search(r'[A-Z]', password):
        return 'Password must contain at least one uppercase letter.'
    if not re.search(r'[a-z]', password):
        return 'Password must contain at least one lowercase letter.'
    if not re.search(r'\d', password):
        return 'Password must contain at least one digit.'
    if password != confirm:
        return 'Passwords do not match.'
    return None


def _token_still_matches(payload, user):
    """Whether a decoded reset token still matches the user's credential.

    F55. The token carries a fingerprint of the password hash it was minted
    against, so this is the whole one-time-use guarantee: the moment the
    password changes the fingerprint stops matching and every outstanding
    token is dead.

    Extracted so the same check is used by the pre-flight GET and by the
    pre-commit row-locked re-read — they must not drift apart, because only
    the second one is atomic.
    """
    from utils.email import password_fingerprint
    return payload['fingerprint'] == password_fingerprint(user.password_hash)


@auth.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def reset_password(token):
    """Consume a password-reset token exactly once.

    F55. Previously the token was a signed email plus a one-hour expiry with no
    record of use, so a successful reset left the link replayable for the rest
    of that hour, and nothing revoked the attacker's existing sessions. Now:

      * the token is bound to a fingerprint of the password hash it was issued
        against, so any password change invalidates every outstanding token;
      * the bound is re-checked under a row lock immediately before the write,
        so two concurrent submissions cannot both succeed;
      * a successful reset raises ``session_version``, which invalidates every
        session cookie and remember-me cookie already issued to that user;
      * this browser's own credentials are cleared as well.
    """
    from utils.email import verify_reset_token

    payload = verify_reset_token(token)
    if not payload:
        flash('Reset link is invalid or has expired.')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=payload['email']).first()
    if not user:
        flash('User not found.')
        return redirect(url_for('auth.login'))

    # The token must still match the credential it was minted against. This
    # runs on GET too, so opening an already-used link shows the same message
    # as a forged one and discloses nothing.
    if not _token_still_matches(payload, user):
        flash('Reset link is invalid or has expired.')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        problem = _password_problem(password, confirm)
        if problem:
            flash(problem)
            return render_template('reset_password.html', token=token)

        # Re-read under a row lock and re-check the binding. Without this the
        # check above is a TOCTOU window: two parallel POSTs both pass it, and
        # the second silently overwrites the first.
        #
        # `populate_existing()` is REQUIRED, not decorative. A re-query of an
        # already-loaded primary key returns the object from the identity map,
        # and `with_for_update()` on its own does NOT refresh its attributes —
        # the column keeps whatever this session loaded before the lock. That
        # was verified directly: with a second connection committing a change
        # in between, `with_for_update().one()` returned the pre-change value
        # while `with_for_update().populate_existing().one()` returned the
        # committed one. Without it the "re-check" would compare the token
        # against the same stale hash it had just passed, and two concurrent
        # resets carrying one token would both be accepted, the second
        # silently overwriting the first. SQLAlchemy's own docs recommend
        # pairing the two for exactly this reason.
        #
        # `with_for_update` is a real lock on PostgreSQL and a harmless no-op on
        # SQLite, where the single-writer test database cannot interleave anyway.
        locked = (User.query.filter_by(id=user.id)
                  .with_for_update()
                  .populate_existing()
                  .one())
        if not _token_still_matches(payload, locked):
            db.session.rollback()
            flash('Reset link is invalid or has expired.')
            return redirect(url_for('auth.forgot_password'))

        locked.set_password(password)
        # Revoke every credential previously issued to this user. The version
        # lives inside both the session cookie and the remember-me cookie, so
        # raising it logs out any other browser or device holding either.
        bump_session_version(locked)
        db.session.commit()

        # And clear this browser's copies. `logout_user` pops the session keys
        # and sets `_remember='clear'`, which makes Flask-Login delete the
        # remember cookie from the response.
        logout_user()
        session.clear()

        flash('Password reset successful. You can now log in.')
        return redirect(url_for('auth.login'))

    return render_template('reset_password.html', token=token)
