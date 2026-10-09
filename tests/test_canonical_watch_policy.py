"""Canonical movie watch history: the `user_viewed` mirror is not history.

THE POLICY BEING PINNED
=======================
``DiaryEntry`` is the canonical authority for MOVIE watch history.
``user_viewed`` is a COMPATIBILITY MIRROR — a denormalised copy maintained for
legacy view-state surfaces, not an independent source of watch history.

Production holds 15 "viewed-only markers": ``user_viewed`` rows for movies with
no ``DiaryEntry``. They are real user data (the user marked something viewed),
so they are preserved. They are NOT watch history, so they must never be
counted as canonical history.

Four guarantees, one test each
------------------------------
1. a canonical diary entry is counted;
2. its matching mirror row does not double-count it;
3. an unmatched marker is preserved but contributes no canonical count;
4. a marker with a NULL ``date_viewed`` is preserved, with no date invented.

Why each surface needed fixing
------------------------------
These were counting the mirror table directly, so a marker inflated a real
statistic:

* ``routes/profile_enhancements.py`` — watched badges, enhanced stats and
  achievements (four call sites, all "N items watched");
* ``routes/auth.py`` — the profile VIEWED stat card, which additionally used
  ``.rowcount`` on a bare SELECT and therefore rendered as ``-1``;
* ``routes/main.py`` + ``templates/user_profile.html`` — "Movies Watched",
  which counted ``len(user.viewed_media)`` and so included TV-typed rows too;
* ``api/smart_lists.py`` — the ``diary`` scope, labelled "Watched History";
* ``routes/lists.py`` — the ``watched_count`` aggregate on a list's watched
  state.

Deliberately NOT changed
-----------------------
The per-item "watched" badge is the mirror's legitimate purpose: "has this been
marked viewed" is exactly what the mirror records. Only the AGGREGATES moved to
canonical history. TV statistics (``TVEpisodeWatch`` / TVViewed) are untouched,
and the ``diary`` smart-list scope now restricts to ``media_type='movie'`` so
TV history cannot leak into a movie surface.
"""
import datetime

import pytest

from models import DiaryEntry, MediaItem, User, db
from models.associations import user_viewed

WATCHED_DATE = datetime.date(2024, 3, 15)


# ── fixtures ─────────────────────────────────────────────────────────────────

# TMDb ids owned exclusively by this module. Kept in one place so no other
# test module can collide with them (MediaItem.tmdb_id is UNIQUE and the test
# database is session-scoped and shared across modules).
OWNED_TMDB_MIN = 9000
OWNED_TMDB_MAX = 31000


@pytest.fixture
def actor(app):
    """A fresh user plus exclusive cleanup.

    Deliberately does NOT wipe User/MediaItem globally: the test database is
    session-scoped and shared, so a blanket delete corrupts whichever module
    runs next. Only rows this module created are removed.
    """
    with app.app_context():
        user = User(username='policy', email='policy@example.test',
                    date_joined=datetime.datetime.utcnow())
        user.set_password('TestPass1')
        db.session.add(user)
        db.session.commit()
        try:
            yield user.id
        finally:
            db.session.rollback()
            # Lists first: other modules purge MediaItem wholesale, and a
            # leftover UserListItem would then violate its foreign key.
            from models import UserList, UserListItem
            list_ids = [row[0] for row in db.session.query(
                UserList.id).filter(UserList.user_id == user.id).all()]
            if list_ids:
                db.session.query(UserListItem).filter(
                    UserListItem.list_id.in_(list_ids)).delete(
                    synchronize_session=False)
                db.session.query(UserList).filter(
                    UserList.id.in_(list_ids)).delete(
                    synchronize_session=False)
            owned = [row[0] for row in db.session.query(
                MediaItem.id).filter(
                MediaItem.tmdb_id.between(OWNED_TMDB_MIN,
                                          OWNED_TMDB_MAX)).all()]
            if owned:
                db.session.execute(user_viewed.delete().where(
                    user_viewed.c.media_id.in_(owned)))
                db.session.execute(DiaryEntry.__table__.delete().where(
                    DiaryEntry.__table__.c.media_id.in_(owned)))
                db.session.query(MediaItem).filter(
                    MediaItem.id.in_(owned)).delete(
                    synchronize_session=False)
            db.session.execute(user_viewed.delete().where(
                user_viewed.c.user_id == user.id))
            db.session.query(DiaryEntry).filter(
                DiaryEntry.user_id == user.id).delete(
                synchronize_session=False)
            db.session.query(User).filter(User.id == user.id).delete(
                synchronize_session=False)
            db.session.commit()


def _movie(tmdb_id, title):
    """Create a MediaItem and return its internal id."""
    media = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title)
    db.session.add(media)
    db.session.flush()
    return media.id


def _diary(user_id, media_id, watched_date=WATCHED_DATE):
    db.session.add(DiaryEntry(user_id=user_id, media_id=media_id,
                              media_type='movie', watched_date=watched_date))
    db.session.flush()


def _marker(user_id, media_id, date_viewed=datetime.datetime(2023, 6, 1)):
    """A `user_viewed` row with NO diary entry — a legacy marker."""
    db.session.execute(user_viewed.insert().values(
        user_id=user_id, media_id=media_id, media_type='movie',
        date_viewed=date_viewed, rating=None))
    db.session.flush()


def _counted(user_id):
    """The one canonical definition every surface must agree with."""
    from api.statistics import canonical_movies_watched
    return canonical_movies_watched(user_id)


# ── guarantee 1: canonical diary entries are counted ─────────────────────────

def test_a_canonical_diary_entry_is_counted(app, actor):
    with app.app_context():
        media_id = _movie(9001, 'Canonically Watched')
        _diary(actor, media_id)
        db.session.commit()
        assert _counted(actor) == 1


def test_several_canonical_entries_are_counted(app, actor):
    with app.app_context():
        for index in range(3):
            _diary(actor, _movie(9010 + index, 'Watched %d' % index))
        db.session.commit()
        assert _counted(actor) == 3


def test_a_rewatch_is_one_title_not_two(app, actor):
    """Distinct titles: the badge/stat surfaces are denominated in titles."""
    with app.app_context():
        media_id = _movie(9020, 'Rewatched')
        _diary(actor, media_id, datetime.date(2022, 1, 1))
        _diary(actor, media_id, datetime.date(2024, 3, 15), )
        db.session.commit()
        assert _counted(actor) == 1


# ── guarantee 2: the mirror does not double-count ────────────────────────────

def test_a_matching_mirror_does_not_double_count(app, actor):
    """Diary row + its mirror row is ONE watched title, not two."""
    with app.app_context():
        media_id = _movie(9030, 'Diary And Mirror')
        _diary(actor, media_id)
        _marker_matching = user_viewed.insert().values(
            user_id=actor, media_id=media_id, media_type='movie',
            date_viewed=datetime.datetime(2024, 3, 15), rating=4)
        db.session.execute(_marker_matching)
        db.session.commit()
        assert _counted(actor) == 1


def test_mirror_rows_present_do_not_change_the_canonical_total(app, actor):
    """Adding mirrors for already-counted titles changes nothing."""
    with app.app_context():
        media_id = _movie(9040, 'Watched')
        _diary(actor, media_id)
        db.session.commit()
        before = _counted(actor)
        db.session.execute(user_viewed.insert().values(
            user_id=actor, media_id=media_id, media_type='movie',
            date_viewed=datetime.datetime(2024, 3, 15), rating=4))
        db.session.commit()
        assert _counted(actor) == before == 1


# ── guarantee 3: unmatched markers are preserved but never counted ───────────

def test_an_unmatched_marker_is_not_counted(app, actor):
    with app.app_context():
        _marker(actor, _movie(9050, 'Marker Only'))
        db.session.commit()
        assert _counted(actor) == 0


def test_an_unmatched_marker_is_preserved(app, actor):
    """Preserved means the row still exists and still says what it said."""
    with app.app_context():
        media_id = _movie(9051, 'Marker Only')
        _marker(actor, media_id)
        db.session.commit()

        rows = db.session.execute(user_viewed.select().where(
            user_viewed.c.user_id == actor)).fetchall()
        assert len(rows) == 1, 'the marker was deleted'
        assert rows[0][1] == media_id
        assert rows[0][3] == datetime.datetime(2023, 6, 1)
        assert DiaryEntry.query.filter_by(user_id=actor).count() == 0, \
            'a diary entry was fabricated from a marker'


def test_markers_and_canonical_entries_are_counted_separately(app, actor):
    """The mixed population: only the diary-backed titles count."""
    with app.app_context():
        _diary(actor, _movie(9060, 'Real A'))
        _diary(actor, _movie(9061, 'Real B'))
        _marker(actor, _movie(9062, 'Marker A'))
        _marker(actor, _movie(9063, 'Marker B'))
        db.session.commit()
        assert _counted(actor) == 2
        assert len(db.session.execute(user_viewed.select().where(
            user_viewed.c.user_id == actor)).fetchall()) == 2


def test_a_marker_never_creates_a_diary_entry(app, actor):
    with app.app_context():
        _marker(actor, _movie(9064, 'Marker'))
        db.session.commit()
        # Reading the statistic must not have back-filled anything.
        _counted(actor)
        assert DiaryEntry.query.filter_by(user_id=actor).count() == 0


# ── guarantee 4: a NULL date_viewed marker ───────────────────────────────────

def test_a_marker_with_a_null_date_is_preserved(app, actor):
    with app.app_context():
        media_id = _movie(9070, 'Undated Marker')
        _marker(actor, media_id, date_viewed=None)
        db.session.commit()
        rows = db.session.execute(user_viewed.select().where(
            user_viewed.c.user_id == actor)).fetchall()
        assert len(rows) == 1
        assert rows[0][3] is None, 'the NULL date was overwritten'


def test_a_null_date_marker_is_not_counted_and_invents_no_date(app, actor):
    with app.app_context():
        _marker(actor, _movie(9071, 'Undated Marker'), date_viewed=None)
        db.session.commit()
        assert _counted(actor) == 0
        assert DiaryEntry.query.filter_by(user_id=actor).count() == 0, \
            'a diary entry was fabricated, which would require inventing a date'


# ── the fixed surfaces ───────────────────────────────────────────────────────

def test_profile_stat_card_counts_canonically(app, actor, client):
    """/auth/profile — the VIEWED card. Previously rendered -1."""
    with app.app_context():
        _diary(actor, _movie(9080, 'Watched'))
        _marker(actor, _movie(9081, 'Marker'))
        db.session.commit()
        _login(client, 'policy')

    response = client.get('/profile')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '>1<' in body, 'the VIEWED figure is not the canonical count of 1'


def test_watched_badges_are_not_awarded_from_mirror_rows(app, actor, client):
    """A watched badge threshold must be reached on diary history alone.

    49 mirror markers are far past the 50-item threshold on their own, while the
    canonical count is 1. If the badge read the mirror it would be earned.
    """
    from routes.profile_enhancements import calculate_user_badges
    with app.app_context():
        _diary(actor, _movie(9090, 'Watched'))
        for index in range(49):
            # A dedicated TMDb range so this count cannot be perturbed by rows
            # left behind by another test in the same session.
            _marker(actor, _movie(30000 + index, 'Marker %d' % index))
        db.session.commit()
        assert len(db.session.execute(user_viewed.select().where(
            user_viewed.c.user_id == actor)).fetchall()) == 49
        assert _counted(actor) == 1
        badges = calculate_user_badges(actor)
    badge_ids = {str(b.get('id', '')) for b in badges}
    assert 'viewed_50' not in badge_ids, (
        'a watched badge was earned from 49 mirror markers while the canonical '
        'title count is 1')


def test_enhanced_stats_count_canonically(app, actor, client):
    with app.app_context():
        _diary(actor, _movie(9100, 'Watched'))
        _marker(actor, _movie(9101, 'Marker'))
        db.session.commit()
        _login(client, 'policy')

    response = client.get('/api/users/%d/stats/enhanced' % actor)
    assert response.status_code == 200
    assert response.get_json()['stats']['viewed_count'] == 1


def test_diary_smart_list_scope_excludes_markers(app, actor, client):
    """The scope is labelled "Watched History"."""
    from models.smart_lists import SmartList
    from api.smart_lists import evaluate_smart_list
    with app.app_context():
        real = _movie(9110, 'Real')
        marker = _movie(9111, 'Marker')
        _diary(actor, real)
        _marker(actor, marker)
        smart = SmartList(user_id=actor, name='WL', scope='diary', sort='date_added')
        smart.filters = {}
        db.session.add(smart)
        db.session.commit()

        result = evaluate_smart_list(smart)
        titles = [item['title'] for item in result['items']]
        assert titles == ['Real']
        assert result['total'] == 1


def test_list_watched_count_ignores_markers_but_badges_them(app, actor, client):
    """The badge is the mirror's job; the count is the diary's.

    Both assertions together prove the two were separated rather than one being
    quietly swapped for the other: the marker item still badges as 'watched'
    (the mirror legitimately records that the user marked it), while
    ``watched_count`` stays at the diary-derived 1.
    """
    from models import UserList, UserListItem
    with app.app_context():
        real = _movie(9120, 'Real')
        marker = _movie(9121, 'Marker')
        user_list = UserList(user_id=actor, title='L', description='')
        db.session.add(user_list)
        db.session.flush()
        db.session.add(UserListItem(list_id=user_list.id, media_id=real,
                                    media_type='movie', position=0))
        db.session.add(UserListItem(list_id=user_list.id, media_id=marker,
                                    media_type='movie', position=1))
        _diary(actor, real)
        _marker(actor, marker)
        db.session.commit()
        list_id = user_list.id
        _login(client, 'policy')

    response = client.get('/api/lists/%d/watched-state' % list_id)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    data = response.get_json()
    assert data['total'] == 2
    assert data['watched_count'] == 1, \
        'an unmatched marker was counted as canonical watch history'
    by_title = {item['media']['title']: item for item in data['items']}
    assert by_title['Marker']['watched'] == 'watched', \
        'the mirror no longer badges a marked-viewed item'
    assert by_title['Marker']['watched_canonical'] is False, \
        'the marker was treated as canonical watch history'
    assert by_title['Real']['watched_canonical'] is True
    assert data['watched_count'] == 1, \
        'an unmatched marker was counted as canonical watch history'


# ── TV is not mixed in ───────────────────────────────────────────────────────

def test_tv_viewed_rows_are_not_counted_as_movie_history(app, actor):
    with app.app_context():
        media_id = _movie(9130, 'TV Typed In Mirror')
        db.session.execute(user_viewed.insert().values(
            user_id=actor, media_id=media_id, media_type='tv',
            date_viewed=datetime.datetime(2024, 1, 1), rating=None))
        db.session.commit()
        assert _counted(actor) == 0


def test_tv_diary_entries_are_not_counted_as_movie_history(app, actor):
    with app.app_context():
        media_id = _movie(9131, 'TV Diary')
        db.session.add(DiaryEntry(user_id=actor, media_id=media_id,
                                  media_type='tv', watched_date=WATCHED_DATE))
        db.session.commit()
        assert _counted(actor) == 0


# ── helpers ──────────────────────────────────────────────────────────────────


def _login(client, username):
    """Log in through the real form, matching the other route test modules."""
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'},
                follow_redirects=True)
