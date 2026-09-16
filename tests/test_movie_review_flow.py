"""Movie detail / review flow hardening (production bug fix).

Regression coverage for the audited movie-detail review flow:

- the fatal ``mobileMenuButton is null`` crash: the inline script must be
  null-safe around the optional mobile-menu button so the review form
  initializes even when the element is absent (base-chrome pages)
- the "Write Review" control must have an in-page anchor target
- the duplicate dead review-render block (targeting a non-existent
  ``#user-reviews-grid`` container with raw innerHTML) must stay deleted;
  review loading belongs to review-manager.js / ``#reviews-list``
- backend contract: CSRF enforcement, duplicate-review 409, transaction
  rollback, ownership checks, media identity resolution (tmdb_id →
  MediaItem.id), XSS-safe rendering

No database schema is created or altered by the frontend fix; backend
tests run against the disposable test DB.
"""
import uuid

from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
# Constants / helpers
# ─────────────────────────────────────────────────────────────────────────────

MOVIE_STUB = {
    'id': 603, 'title': 'T', 'poster_path': None, 'overview': '',
    'release_date': '', 'genres': [], 'vote_average': 0,
    'recommendations': [], 'budget': 0, 'revenue': 0,
    'cast': [], 'crew': [], 'videos': {'results': []},
    'images': {}, 'runtime': 100, 'reviews': [],
    'tagline': '', 'vote_count': 0, 'status': 'Released',
    'original_language': 'en', 'trailer_url': None,
    'certification': None, 'director': '', 'writer': '',
    'backdrop_path': None,
}

TV_STUB = {
    'id': 1399, 'name': 'S', 'poster_path': None, 'overview': '',
    'first_air_date': '', 'genres': [], 'vote_average': 0,
    'number_of_seasons': 1, 'seasons': [], 'status': 'Ended',
    'origin_country': [], 'created_by': [], 'cast': [],
    'videos': {'results': []}, 'episode_run_time': [45],
}


def _render_movie(client, monkeypatch):
    import routes.details as details
    monkeypatch.setattr(details, 'fetch_movie_details', lambda _id: dict(MOVIE_STUB, id=_id))
    return client.get('/movie/603')


def _render_tv(client, monkeypatch):
    import routes.details as details
    monkeypatch.setattr(details, 'fetch_tv_show_details', lambda _id: dict(TV_STUB, id=_id))
    return client.get('/tv/1399')


# ─────────────────────────────────────────────────────────────────────────────
# Primary crash: mobileMenuButton null-safety (§2, §3, §18)
# ─────────────────────────────────────────────────────────────────────────────

class TestMobileMenuNullSafety:
    def test_movie_page_has_guarded_mobile_menu_block(self, client, monkeypatch):
        """The exact production defect: getElementById('mobile-menu-button')
        was used unconditionally on a page that does not render that
        element, aborting the whole inline init handler. The guarded form
        must be present in the shipped template source."""
        r = _render_movie(client, monkeypatch)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'if (mobileMenuButton) {' in html
        # No unconditional addEventListener on the optional element.
        bare = '\n            mobileMenuButton.addEventListener'
        assert bare not in html

    def test_movie_page_does_not_render_mobile_menu_button(self, client, monkeypatch):
        """Why it is null: this page uses the base chrome, which has no
        #mobile-menu-button — so the query legitimately returns null."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'id="mobile-menu-button"' not in html

    def test_movie_page_mobile_menu_absent_but_review_init_present(self, auth_client, monkeypatch):
        """Isolation: with the optional element absent, the review
        form / star rating / submission wiring is still shipped."""
        r = _render_movie(auth_client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'id="review-form"' in html
        assert 'id="star-rating"' in html
        assert 'id="submit-review-btn"' in html
        assert "getElementById('review-form')" in html

    def test_base_nav_has_no_legacy_mobile_menu_button(self, client):
        """base.html's chrome uses the mobile-nav drawer; the legacy
        #mobile-menu-button belongs only to pages that render it."""
        r = client.get('/')
        html = r.get_data(as_text=True)
        assert 'id="mobile-nav-toggle"' in html
        assert 'id="mobile-menu-button"' not in html


# ─────────────────────────────────────────────────────────────────────────────
# Write Review control (§5)
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteReviewControl:
    def test_movie_write_review_has_anchor_target(self, auth_client, monkeypatch):
        """The button href pointed at #write-review but no element carried
        that id — clicking did nothing even with JS healthy."""
        r = _render_movie(auth_client, monkeypatch)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'id="write-review"' in html
        assert 'href="#write-review"' in html

    def test_tv_write_review_has_anchor_target(self, auth_client, monkeypatch):
        r = _render_tv(auth_client, monkeypatch)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'id="write-review"' in html

    def test_anonymous_movie_page_hides_write_review(self, client, monkeypatch):
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'write-review-btn' not in html


# ─────────────────────────────────────────────────────────────────────────────
# Dead duplicate review-render block removed (§4, §14, §20)
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateReviewBlockRemoved:
    def test_movie_page_no_user_reviews_grid_target(self, client, monkeypatch):
        """The deleted block fetched into #user-reviews-grid, which exists
        in no template — it crashed immediately even when reached."""
        r = _render_movie(client, monkeypatch)
        assert r.status_code == 200
        assert 'user-reviews-grid' not in r.get_data(as_text=True)

    def test_tv_page_no_user_reviews_grid_target(self, client, monkeypatch):
        r = _render_tv(client, monkeypatch)
        assert r.status_code == 200
        assert 'user-reviews-grid' not in r.get_data(as_text=True)

    def test_movie_page_no_duplicate_renderers(self, client, monkeypatch):
        """createReviewCard / loadUserReviews / threaded-comment rendering
        are owned by review-manager.js now — no duplicate definitions."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        for name in ('loadUserReviews', 'renderCommentTree',
                     'submitNewComment', 'attachReplyActions',
                     'attachLikeButtonListeners', 'attachCommentListeners'):
            assert name not in html, name

    def test_review_manager_container_present(self, client, monkeypatch):
        """The surviving review system targets #reviews-list."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'id="reviews-list"' in html
        assert 'review-manager.js' in html

    def test_review_manager_escapes_user_and_media_fields(self):
        """Usernames and media titles are user/catalog-controlled strings
        interpolated into innerHTML — they must be escaped."""
        src = open('static/js/review-manager.js', encoding='utf-8').read()
        assert "this.escapeHtml(review.user.username)" in src
        assert "this.escapeHtml(review.media.title)" in src
        assert 'escapeHtml(text)' in src


# ─────────────────────────────────────────────────────────────────────────────
# Review POST endpoint contract (§6, §11, §12, §13, §19)
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewPostEndpoint:
    def test_create_review_persists_with_internal_media_id(self, auth_client, db, app):
        """tmdb_id → MediaItem.id resolution: review rows must reference the
        internal MediaItem PK, never the TMDb id."""
        from models import Review, MediaItem
        with app.app_context():
            media = MediaItem(tmdb_id=603, media_type='movie', title='T')
            db.session.add(media)
            db.session.commit()

            r = auth_client.post('/api/reviews', json={
                'media_id': 603, 'media_type': 'movie', 'rating': 4.0,
                'content': 'Great film',
            })
            assert r.status_code == 201
            review = Review.query.one()
            assert review.media_id == media.id          # internal PK
            assert media.tmdb_id == 603                  # NOT the tmdb id
            assert review.user_id is not None

            db.session.delete(review)
            db.session.delete(media)
            db.session.commit()

    def test_duplicate_review_returns_409_not_500(self, auth_client, db, app):
        """UNIQUE(user_id, media_id, media_type) violation must surface as a
        graceful 409 — no uncaught IntegrityError, no broken session."""
        from models import Review, MediaItem
        with app.app_context():
            media = MediaItem(tmdb_id=777, media_type='movie', title='D')
            db.session.add(media)
            db.session.commit()

            r1 = auth_client.post('/api/reviews', json={
                'media_id': 777, 'media_type': 'movie', 'rating': 3.5})
            assert r1.status_code == 201
            r2 = auth_client.post('/api/reviews', json={
                'media_id': 777, 'media_type': 'movie', 'rating': 2.0})
            assert r2.status_code == 409
            assert b'already reviewed' in r2.data

            # Session remains usable after the rolled-back IntegrityError.
            assert db.session.query(Review).count() == 1

            db.session.query(Review).delete()
            db.session.delete(media)
            db.session.commit()

    def test_rating_bounds_rejected(self, auth_client, db, app):
        from models import MediaItem
        with app.app_context():
            media = MediaItem(tmdb_id=778, media_type='movie', title='R')
            db.session.add(media)
            db.session.commit()
            for bad in ('0.3', '5.5', 'abc'):
                r = auth_client.post('/api/reviews', json={
                    'media_id': 778, 'media_type': 'movie', 'rating': bad})
                assert r.status_code == 400, bad
            db.session.delete(media)
            db.session.commit()

    def test_anonymous_review_rejected(self, client):
        r = client.post('/api/reviews', json={
            'media_id': 603, 'media_type': 'movie', 'rating': 4.0})
        assert r.status_code in (301, 302, 401, 403)

    def test_csrf_missing_token_rejected(self, app, db):
        """CSRFProtect is global for state-changing verbs; a POST without a
        valid token must be rejected (400) even when authenticated."""
        from models import User
        with app.app_context():
            u = User(username=f'csrf_{uuid.uuid4().hex[:8]}',
                     email=f'csrf_{uuid.uuid4().hex[:8]}@example.com',
                     email_verified=True)
            u.set_password('TestPass1')
            db.session.add(u)
            db.session.commit()
            uid = u.id

        app.config['WTF_CSRF_ENABLED'] = True
        try:
            c = app.test_client()
            c.post('/login', data={'username': u.username,
                                   'password': 'TestPass1'},
                   follow_redirects=True)
            r = c.post('/api/reviews', json={
                'media_id': 603, 'media_type': 'movie', 'rating': 4.0})
            assert r.status_code == 400
        finally:
            app.config['WTF_CSRF_ENABLED'] = False
            with app.app_context():
                from models import User as _U
                _u = db.session.get(_U, uid)
                db.session.delete(_u)
                db.session.commit()

    def test_csrf_valid_token_accepted(self, app, db):
        """The real browser flow: base.html renders <meta csrf-token>, its
        fetch patch sends X-CSRFToken; a request carrying the session's
        signed token is accepted."""
        from models import User, MediaItem
        with app.app_context():
            u = User(username=f'csrfok_{uuid.uuid4().hex[:8]}',
                     email=f'csrfok_{uuid.uuid4().hex[:8]}@example.com',
                     email_verified=True)
            u.set_password('TestPass1')
            media = MediaItem(tmdb_id=780, media_type='movie', title='CS')
            db.session.add_all([u, media])
            db.session.commit()
            uname, uid, mid = u.username, u.id, media.id

        app.config['WTF_CSRF_ENABLED'] = True
        try:
            c = app.test_client()
            html = c.get('/login').get_data(as_text=True)
            token = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
            c.post('/login', data={'username': uname, 'password': 'TestPass1',
                                   'csrf_token': token}, follow_redirects=True)
            r = c.post('/api/reviews', json={
                'media_id': 780, 'media_type': 'movie', 'rating': 4.0},
                headers={'X-CSRFToken': token})
            assert r.status_code == 201
        finally:
            app.config['WTF_CSRF_ENABLED'] = False
            with app.app_context():
                from models import Review as _R, MediaItem as _M, User as _U
                db.session.query(_R).filter_by(user_id=uid).delete()
                db.session.query(_M).filter_by(id=mid).delete()
                db.session.delete(db.session.get(_U, uid))
                db.session.commit()

    def test_base_template_ships_csrf_fetch_patch(self, client, monkeypatch):
        """The browser flow relies on base.html's global fetch patch that
        injects X-CSRFToken on mutating requests."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'meta name="csrf-token"' in html
        assert 'X-CSRFToken' in html


# ─────────────────────────────────────────────────────────────────────────────
# Transaction safety (§12)
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionSafety:
    def test_db_error_rolls_back_and_session_stays_usable(self, auth_client, db, app):
        """A server-side failure must roll back and leave the session
        usable — no aborted Postgres transaction leaking into subsequent
        request work."""
        from models import Review, MediaItem
        with app.app_context():
            media = MediaItem(tmdb_id=790, media_type='movie', title='X')
            db.session.add(media)
            db.session.commit()
            media_id = media.id

        with patch('routes.reviews.Review') as boom:
            boom.side_effect = RuntimeError('simulated DB failure')
            r = auth_client.post('/api/reviews', json={
                'media_id': 790, 'media_type': 'movie', 'rating': 4.0})
            assert r.status_code == 500
            assert b'Traceback' not in r.data

        with app.app_context():
            # Rolled back: nothing persisted; session usable afterwards.
            assert db.session.query(Review).count() == 0
            db.session.add(Review(user_id=1, media_id=media_id,
                                  media_type='movie', rating=4.0))
            db.session.commit()
            assert db.session.query(Review).count() == 1
            db.session.query(Review).delete()
            db.session.delete(db.session.get(MediaItem, media_id))
            db.session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Ownership (§19)
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewOwnership:
    def test_other_users_review_cannot_be_modified(self, auth_client, db, app):
        from models import Review, MediaItem, User
        with app.app_context():
            other = User(username=f'other_{uuid.uuid4().hex[:8]}',
                         email=f'other_{uuid.uuid4().hex[:8]}@example.com',
                         email_verified=True)
            other.set_password('TestPass1')
            media = MediaItem(tmdb_id=795, media_type='movie', title='O')
            db.session.add_all([other, media])
            db.session.commit()
            review = Review(user_id=other.id, media_id=media.id,
                            media_type='movie', rating=4.0, content='mine')
            db.session.add(review)
            db.session.commit()
            rid, other_id = review.id, other.id

        r = auth_client.put(f'/api/reviews/{rid}', json={'rating': 1.0})
        assert r.status_code == 403
        r = auth_client.delete(f'/api/reviews/{rid}')
        assert r.status_code == 403

        with app.app_context():
            r2 = db.session.get(Review, rid)
            assert r2.rating == 4.0 and r2.is_deleted is False
            db.session.delete(r2)
            db.session.delete(media)
            db.session.delete(db.session.get(User, other_id))
            db.session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Review display pipeline (§14)
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewDisplay:
    def test_media_reviews_endpoint_serves_created_review(self, auth_client, db, app):
        """The exact pipeline: create via POST /api/reviews (tmdb id) →
        read via GET /api/media/<tmdb_id>/reviews (review-manager.js's
        fetch shape)."""
        from models import Review, MediaItem
        with app.app_context():
            media = MediaItem(tmdb_id=800, media_type='movie', title='DP')
            db.session.add(media)
            db.session.commit()

        auth_client.post('/api/reviews', json={
            'media_id': 800, 'media_type': 'movie', 'rating': 4.5,
            'content': 'Solid entry <script>alert(1)</script>'})

        r = auth_client.get('/api/media/movie/800/reviews')
        assert r.status_code == 200
        data = r.get_json()
        assert data['total'] == 1
        review = data['reviews'][0]
        assert review['rating'] == 4.5
        # API returns stored content verbatim; XSS-safety is the rendering
        # layer's job (guarded by test_review_manager_escapes_user_and_media_fields).
        assert 'Solid entry' in review['content']

        with app.app_context():
            db.session.query(Review).delete()
            db.session.delete(media)
            db.session.commit()

    def test_review_manager_targets_reviews_list_not_grid(self):
        src = open('static/js/review-manager.js', encoding='utf-8').read()
        assert "getElementById('reviews-list')" in src
        assert 'user-reviews-grid' not in src

    def test_review_manager_dispatches_rendered_event(self):
        src = open('static/js/review-manager.js', encoding='utf-8').read()
        assert "document.dispatchEvent(new CustomEvent('reviews:rendered'))" in src

    def test_templates_no_raw_review_content_interpolation(self, client, monkeypatch):
        """The deleted inline renderer interpolated raw review content into
        innerHTML; it must stay gone from the shipped page source."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert '${review.content' not in html
        assert '${comment.content' not in html


# ─────────────────────────────────────────────────────────────────────────────
# Follow button / shared script isolation (§15, §18)
# ─────────────────────────────────────────────────────────────────────────────

class TestSharedScriptIsolation:
    def test_follow_button_script_null_safe(self):
        src = open('static/js/follow-button.js', encoding='utf-8').read()
        assert "querySelectorAll('[data-follow-button]')" in src
        # It never touches optional chrome elements.
        assert 'mobile-menu-button' not in src

    def test_movie_page_includes_shared_social_scripts_once(self, client, monkeypatch):
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert html.count('js/follow-button.js') == 1
        assert html.count('js/review-manager.js') == 1

    def test_follow_init_on_dynamic_cards_wired(self, client, monkeypatch):
        """After the dead block's removal, dynamically rendered review-card
        follow buttons are initialized via the reviews:rendered event."""
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert "addEventListener('reviews:rendered'" in html
        assert 'initReviewFollowButtons' in html


# ─────────────────────────────────────────────────────────────────────────────
# Route health (§23)
# ─────────────────────────────────────────────────────────────────────────────

class TestRouteHealth:
    def test_movie_route_200_no_traceback(self, client, monkeypatch):
        r = _render_movie(client, monkeypatch)
        assert r.status_code == 200
        assert b'Traceback' not in r.data

    def test_tv_route_200_no_traceback(self, client, monkeypatch):
        r = _render_tv(client, monkeypatch)
        assert r.status_code == 200
        assert b'Traceback' not in r.data

    def test_review_section_rendered(self, client, monkeypatch):
        r = _render_movie(client, monkeypatch)
        html = r.get_data(as_text=True)
        assert 'User Reviews' in html or 'Reviews' in html
        assert 'id="user-reviews-tab"' in html
        assert 'id="review-sort"' in html


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation subsystem untouched (§25)
# ─────────────────────────────────────────────────────────────────────────────

class TestRecommendationUntouched:
    def test_no_recommendation_references_in_changed_templates(self):
        for path in ('templates/movie_detail.html', 'templates/tv_detail.html'):
            src = open(path, encoding='utf-8').read()
            assert 'for_you' not in src.lower()
            assert 'taste_profile' not in src.lower()
