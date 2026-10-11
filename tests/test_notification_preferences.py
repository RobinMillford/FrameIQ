"""Feature F12 — notification preferences + reliable notification categories.

Covers:
- defaults: a user with no preference record keeps the pre-F12 behaviour
- validation: unknown categories and bad values are rejected, not persisted
- persistence: one row per user, scoped, safe under repeated saves
- generation: a disabled category stops NEW notifications only
- season premieres: an alternative presentation of S1E1, never a second
  notification for the same episode (including after a preference change)
- idempotency: re-running the producer creates nothing new
- security: cross-user reads/writes are impossible; CSRF/auth intact
- performance/schema: one batched preference lookup, bounded inserts, additive
  migration that does not depend on the deferred destructive 0002
"""
from datetime import datetime, timedelta

import pytest

from models import db, TVShowProgress, TVEpisodeWatch, UpcomingEpisode
from models import User
from models.notification import Notification
from models.notification_preferences import UserNotificationPreference
from api.notification_preferences import (
    CATEGORIES, DEFAULTS, PreferenceError, get_preferences,
    preferences_for_users, save_preferences, validate_preferences,
)
from api.notifications import notify_newly_aired_episodes

SHOW_A = 2399
TODAY = datetime.utcnow().date()
FUTURE = datetime.utcnow().date() + timedelta(days=30)


def _upcoming(show_id, season, episode, air_date=TODAY):
    return UpcomingEpisode(
        show_id=show_id, show_name=f"Show {show_id}", poster_path=None,
        season_number=season, episode_number=episode,
        episode_name=f"S{season}E{episode} Name", air_date=air_date)


@pytest.fixture
def env(app, db):
    """Two users; A tracks SHOW_A as 'watching'."""
    with app.app_context():
        ua = User(username='f12_a', email='f12a@example.com',
                  email_verified=True)
        ua.set_password('F12Pass123')
        ub = User(username='f12_b', email='f12b@example.com',
                  email_verified=True)
        ub.set_password('F12Pass123')
        db.session.add_all([ua, ub])
        db.session.commit()
        db.session.add(TVShowProgress(user_id=ua.id, show_id=SHOW_A,
                                      status='watching'))
        db.session.commit()
        yield {'a': ua, 'b': ub}
        # Roll back first so a failed assertion can never leave the shared
        # session unusable for the next test.
        db.session.rollback()
        Notification.query.delete()
        UserNotificationPreference.query.delete()
        TVEpisodeWatch.query.delete()
        TVShowProgress.query.delete()
        UpcomingEpisode.query.delete()
        User.query.filter(User.id.in_([ua.id, ub.id])).delete(
            synchronize_session=False)
        db.session.commit()


@pytest.fixture
def client_a(client, env):
    client.post('/login', data={'username': 'f12_a', 'password': 'F12Pass123'})
    return client


# ── Defaults & compatibility ────────────────────────────────────────────────

class TestDefaults:

    def test_documented_defaults_are_new_episode_on_premiere_off(self):
        assert DEFAULTS == {'new_episode': True, 'season_premiere': False}

    def test_user_without_a_record_gets_the_defaults(self, env):
        assert get_preferences(env['a'].id) == DEFAULTS

    def test_reading_preferences_never_creates_a_row(self, env):
        get_preferences(env['a'].id)
        get_preferences(env['a'].id)
        assert UserNotificationPreference.query.filter_by(
            user_id=env['a'].id).count() == 0

    def test_existing_user_without_preferences_still_gets_episode_alerts(
            self, env):
        db.session.add(_upcoming(SHOW_A, 2, 4))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert Notification.query.one().type == Notification.TYPE_NEW_EPISODE

    def test_existing_user_receives_season_one_episode_one_as_before(
            self, env):
        # The pre-F12 behaviour for S1E1 was an ordinary episode alert.
        db.session.add(_upcoming(SHOW_A, 1, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        row = Notification.query.one()
        assert row.type == Notification.TYPE_NEW_EPISODE
        assert row.title == 'New episode available'


# ── Validation ──────────────────────────────────────────────────────────────

class TestValidation:

    def test_supported_categories_are_exactly_the_documented_pair(self):
        assert set(CATEGORIES) == {'new_episode', 'season_premiere'}

    def test_unknown_category_is_rejected(self):
        with pytest.raises(PreferenceError):
            validate_preferences({'availability_change': True})

    def test_unknown_category_alongside_a_valid_one_is_rejected(self):
        with pytest.raises(PreferenceError):
            validate_preferences({'new_episode': False, 'social': True})

    @pytest.mark.parametrize('bad', ['yes', 2, None, [], {}])
    def test_non_boolean_values_are_rejected(self, bad):
        with pytest.raises(PreferenceError):
            validate_preferences({'new_episode': bad})

    def test_string_checkbox_values_are_accepted(self):
        assert validate_preferences(
            {'new_episode': 'false', 'season_premiere': '1'}) == {
                'new_episode': False, 'season_premiere': True}

    def test_empty_payload_is_rejected(self):
        with pytest.raises(PreferenceError):
            validate_preferences({})

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(PreferenceError):
            validate_preferences(['new_episode'])


# ── Persistence ─────────────────────────────────────────────────────────────

class TestPersistence:

    def test_saving_creates_one_row_scoped_to_the_user(self, env):
        stored = save_preferences(env['a'].id, {'new_episode': False})
        assert stored['new_episode'] is False
        assert stored['season_premiere'] is False
        row = UserNotificationPreference.query.filter_by(
            user_id=env['a'].id).one()
        assert row.new_episode is False

    def test_repeated_saves_never_create_a_second_row(self, env):
        for _ in range(5):
            save_preferences(env['a'].id, {'new_episode': False})
        save_preferences(env['a'].id, {'season_premiere': True})
        assert UserNotificationPreference.query.filter_by(
            user_id=env['a'].id).count() == 1
        stored = get_preferences(env['a'].id)
        assert stored == {'new_episode': False, 'season_premiere': True}

    def test_saving_updates_in_place(self, env):
        save_preferences(env['a'].id, {'new_episode': True})
        save_preferences(env['a'].id, {'season_premiere': True})
        assert get_preferences(env['a'].id)['season_premiere'] is True

    def test_unspecified_category_keeps_its_stored_value(self, env):
        save_preferences(env['a'].id,
                         {'new_episode': False, 'season_premiere': True})
        save_preferences(env['a'].id, {'new_episode': True})
        assert get_preferences(env['a'].id) == {'new_episode': True,
                                                'season_premiere': True}

    def test_preferences_are_isolated_between_users(self, env):
        save_preferences(env['a'].id, {'new_episode': False})
        assert get_preferences(env['b'].id) == DEFAULTS

    def test_batch_resolution_covers_users_without_a_row(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        batch = preferences_for_users([env['a'].id, env['b'].id])
        assert batch[env['a'].id]['season_premiere'] is True
        assert batch[env['b'].id] == DEFAULTS

    def test_batch_resolution_is_one_query(self, env):
        save_preferences(env['a'].id, {'new_episode': False})
        save_preferences(env['b'].id, {'new_episode': False})
        from sqlalchemy import event
        from models.base import db as _db
        count = {'n': 0}

        def _count(conn, cursor, statement, *a, **k):
            if 'user_notification_preference' in statement.lower():
                count['n'] += 1

        event.listen(_db.engine, 'before_cursor_execute', _count)
        try:
            preferences_for_users([env['a'].id, env['b'].id])
        finally:
            event.remove(_db.engine, 'before_cursor_execute', _count)
        assert count['n'] == 1

    def test_empty_batch_is_safe(self, env):
        assert preferences_for_users([]) == {}


# ── Generation ──────────────────────────────────────────────────────────────

class TestGeneration:

    def test_enabled_category_produces_the_notification(self, env):
        save_preferences(env['a'].id, {'new_episode': True})
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert Notification.query.one().episode == 3

    def test_disabled_category_prevents_new_notifications(self, env):
        save_preferences(env['a'].id, {'new_episode': False})
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 0

    def test_disabling_does_not_delete_existing_notifications(self, env):
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        save_preferences(env['a'].id, {'new_episode': False})
        assert Notification.query.count() == 1

    def test_existing_unread_stay_unread_after_disabling(self, env):
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        notify_newly_aired_episodes()
        save_preferences(env['a'].id, {'new_episode': False})
        row = Notification.query.one()
        assert row.read_at is None
        from api.notifications import unread_count
        assert unread_count(env['a'].id) == 1

    def test_preference_is_read_once_per_run_not_per_candidate(self, env):
        for ep in range(1, 9):
            db.session.add(_upcoming(SHOW_A, 3, ep))
        db.session.commit()
        from sqlalchemy import event
        from models.base import db as _db
        count = {'n': 0}

        def _count(conn, cursor, statement, *a, **k):
            if 'user_notification_preference' in statement.lower():
                count['n'] += 1

        event.listen(_db.engine, 'before_cursor_execute', _count)
        try:
            notify_newly_aired_episodes()
        finally:
            event.remove(_db.engine, 'before_cursor_execute', _count)
        assert count['n'] == 1

    def test_untracked_user_is_still_not_notified(self, env):
        save_preferences(env['b'].id, {'new_episode': True})
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        notify_newly_aired_episodes()
        assert Notification.query.filter_by(
            user_id=env['b'].id).count() == 0

    def test_already_watched_episode_is_not_new(self, env):
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.add(TVEpisodeWatch(user_id=env['a'].id, show_id=SHOW_A,
                                      season_number=2, episode_number=3))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0

    def test_future_episode_is_not_notified(self, env):
        db.session.add(_upcoming(SHOW_A, 2, 3, air_date=FUTURE))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0


# ── Season premieres ────────────────────────────────────────────────────────

class TestSeasonPremieres:

    def test_premiere_is_used_when_opted_in(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        row = Notification.query.one()
        assert row.type == Notification.TYPE_SEASON_PREMIERE
        assert 'Season 4' in row.title + row.body

    def test_premiere_and_episode_are_never_both_created(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 1))
        db.session.commit()
        notify_newly_aired_episodes()
        assert Notification.query.count() == 1
        types = {n.type for n in Notification.query.all()}
        assert types == {Notification.TYPE_SEASON_PREMIERE}

    def test_opting_in_later_does_not_duplicate_an_existing_episode_alert(
            self, env):
        # Already received a plain S1E1 alert, THEN enables premieres.
        db.session.add(_upcoming(SHOW_A, 1, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        save_preferences(env['a'].id, {'season_premiere': True})
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 1
        assert Notification.query.one().type == Notification.TYPE_NEW_EPISODE

    def test_premiere_is_gated_independently_of_new_episode(self, env):
        # new_episode and season_premiere are INDEPENDENT categories, not a
        # master switch plus a sub-mode: with premieres on and ordinary
        # episodes off, S1E1 is announced as a premiere while an ordinary
        # episode of the same season stays silent.
        save_preferences(env['a'].id, {'new_episode': False,
                                       'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert Notification.query.one().type == Notification.TYPE_SEASON_PREMIERE
        db.session.add(_upcoming(SHOW_A, 4, 2))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0

    def test_non_premiere_episode_is_unaffected_by_the_premiere_setting(
            self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 5))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert Notification.query.one().type == Notification.TYPE_NEW_EPISODE

    def test_specials_season_zero_episode_one_is_not_a_premiere(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 0, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert Notification.query.one().type == Notification.TYPE_NEW_EPISODE


# ── Idempotency ─────────────────────────────────────────────────────────────

class TestIdempotency:

    def test_running_twice_creates_nothing_new(self, env):
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 1

    def test_premiere_run_is_idempotent(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 1))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 1

    def test_target_url_is_server_generated(self, env):
        save_preferences(env['a'].id, {'season_premiere': True})
        db.session.add(_upcoming(SHOW_A, 4, 1))
        db.session.commit()
        notify_newly_aired_episodes()
        assert Notification.query.one().target_url == f'/watch/tv/{SHOW_A}/4/1'

    def test_missing_optional_metadata_does_not_crash(self, env):
        # episode_name and poster_path are nullable upstream; the producer must
        # still build a complete, linkable notification.
        ep = _upcoming(SHOW_A, 2, 6)
        ep.episode_name = None
        ep.poster_path = None
        db.session.add(ep)
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        row = Notification.query.one()
        assert row.episode_name is None
        assert row.target_url == f'/watch/tv/{SHOW_A}/2/6'
        assert row.body


# ── API ─────────────────────────────────────────────────────────────────────

class TestPreferencesApi:

    def test_requires_authentication(self, client):
        assert client.get('/api/notification-preferences').status_code in (
            301, 302, 401)

    def test_returns_defaults_and_the_catalogue(self, client_a, env):
        r = client_a.get('/api/notification-preferences')
        assert r.status_code == 200
        assert r.json['preferences'] == DEFAULTS
        assert r.json['saved'] is False
        keys = [c['key'] for c in r.json['categories']]
        assert keys == list(CATEGORIES)
        assert all(c['description'] for c in r.json['categories'])

    def test_saving_persists_and_flags_saved(self, client_a, env):
        r = client_a.post('/api/notification-preferences',
                          json={'preferences': {'new_episode': False}})
        assert r.status_code == 200
        assert r.json['preferences']['new_episode'] is False
        assert r.json['saved'] is True
        assert get_preferences(env['a'].id)['new_episode'] is False

    def test_saving_accepts_a_bare_object(self, client_a, env):
        r = client_a.post('/api/notification-preferences',
                          json={'new_episode': False})
        assert r.status_code == 200

    def test_unsupported_category_is_rejected_with_400(self, client_a, env):
        r = client_a.post('/api/notification-preferences',
                          json={'preferences': {'social': True}})
        assert r.status_code == 400
        assert 'social' in r.json['error']
        assert UserNotificationPreference.query.count() == 0

    def test_invalid_value_is_rejected_with_400(self, client_a, env):
        r = client_a.post('/api/notification-preferences',
                          json={'preferences': {'new_episode': 'perhaps'}})
        assert r.status_code == 400

    def test_one_user_cannot_see_anothers_preferences(self, client_a, env):
        save_preferences(env['b'].id, {'new_episode': False})
        r = client_a.get('/api/notification-preferences')
        assert r.json['preferences']['new_episode'] is True

    def test_payload_cannot_target_another_user(self, client_a, env):
        save_preferences(env['b'].id, {'new_episode': True})
        client_a.post('/api/notification-preferences',
                      json={'user_id': env['b'].id,
                            'preferences': {'new_episode': False}})
        assert get_preferences(env['b'].id)['new_episode'] is True

    def test_categories_endpoint_is_authenticated_and_static(self, client_a):
        r = client_a.get('/api/notification-preferences/categories')
        assert r.status_code == 200
        assert [c['key'] for c in r.json['categories']] == list(CATEGORIES)

    def test_existing_notification_endpoints_are_untouched(self, client_a, env):
        db.session.add(_upcoming(SHOW_A, 2, 3))
        db.session.commit()
        notify_newly_aired_episodes()
        listing = client_a.get('/api/notifications')
        assert listing.status_code == 200
        assert listing.json['unread_count'] == 1
        payload = listing.json['notifications'][0]
        for field in ('id', 'type', 'title', 'body', 'target_url', 'show_id',
                      'season', 'episode', 'created_at', 'read'):
            assert field in payload
        marked = client_a.post(
            f"/api/notifications/{payload['id']}/read")
        assert marked.status_code == 200
        assert marked.json['unread_count'] == 0


# ── Schema safety ───────────────────────────────────────────────────────────

class TestSchemaSafety:

    def test_preference_table_is_declared_in_models(self, db):
        assert 'user_notification_preference' in db.metadata.tables

    def test_preference_table_is_unique_per_user(self):
        table = UserNotificationPreference.__table__
        names = {c.name for c in table.constraints if c.name}
        assert 'uq_user_notification_preference' in names

    def test_migration_creates_exactly_the_declared_table(self):
        import migrates.migrations_0004_notification_preferences as m
        assert m.CREATES_TABLES == ('user_notification_preference',)
        assert m.ADDS_COLUMNS == ()
        assert m.DESTRUCTIVE is False

    def test_migration_does_not_depend_on_the_deferred_0002(self):
        from migrates import registry
        spec = registry.by_version()['0004_notification_preferences']
        assert spec.depends_on == ('0001_canonical_watched_reconcile',)
        assert '0002_remove_legacy_wishlist' not in spec.depends_on

    def test_registry_remains_valid(self):
        from migrates import registry
        assert registry.validate() == []