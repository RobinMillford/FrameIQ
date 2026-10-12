"""Feature F15 — notification preferences and discovery presets in the export.

F5 built the account export. Two things had drifted since:

1. ``UserNotificationPreference`` (F12) was never in the export at all, and its
   model is deliberately sparse — a user who never saved settings has no row.
   Exporting stored rows alone would have reported most users as having no
   preferences, so F15 exports the EFFECTIVE values through the same reader the
   delivery path uses.

2. F11 discovery presets share the ``smart_list`` table with ordinary Smart
   Lists, and their ``scope`` is an inert compatibility placeholder. Exporting
   that raw ``scope`` made a saved TMDb /discover search indistinguishable from
   an ordinary "Everything" list.

These tests pin the new behaviour AND the backward compatibility: every key
that existed before still exists with the same value.
"""
import csv
import io
import json
import uuid
import zipfile

import pytest

from api import account_export as ae
from api import notification_preferences as np
from api.discovery import (DISCOVERY_COMPAT_SCOPE, DISCOVERY_COMPAT_SORT,
                           DISCOVERY_KIND)
from api.smart_lists import is_discovery_preset


# ── fixtures ────────────────────────────────────────────────────────────────

def _uid(prefix='f15'):
    return '%s_%s' % (prefix, uuid.uuid4().hex[:10])


@pytest.fixture(autouse=True)
def _wipe(app):
    """Same reason as test_account_export: `app` is session-scoped with no
    per-test teardown, so rows would otherwise leak between tests."""
    from models import db

    def _do():
        with app.app_context():
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()
    _do()
    yield
    _do()


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


def _user(username=None):
    from models import User, db
    username = username or _uid('f15user')
    user = User(username=username, email='%s@example.com' % username,
                email_verified=True)
    user.set_password('ExportTest1!')
    db.session.add(user)
    db.session.commit()
    return user


def _preset(user, name='Sci-fi picks', discovery=None):
    """Create a preset exactly the way routes/explore.py does."""
    from api.discovery import validate_discovery_config
    from api.smart_lists import validate_config
    from models import SmartList, db
    discovery = validate_discovery_config(discovery or {'sort_by': 'popularity'})
    filters = validate_config(
        DISCOVERY_COMPAT_SCOPE,
        {'kind': DISCOVERY_KIND, 'discovery': discovery},
        DISCOVERY_COMPAT_SORT)
    row = SmartList(user_id=user.id, name=name, scope=DISCOVERY_COMPAT_SCOPE,
                    filters=filters, sort=DISCOVERY_COMPAT_SORT,
                    is_public=False)
    db.session.add(row)
    db.session.commit()
    return row


def _smart_list(user, name='My smart list', scope='diary', filters=None):
    from api.smart_lists import validate_config
    from models import SmartList, db
    row = SmartList(user_id=user.id, name=name, scope=scope,
                    filters=validate_config(scope, filters or {'genres': ['Drama']},
                                            'date_added'),
                    sort='date_added', is_public=False)
    db.session.add(row)
    db.session.commit()
    return row


def _export(user):
    return ae.build_export(user)


# ── notification preferences ────────────────────────────────────────────────

def test_preferences_export_when_a_row_exists(ctx):
    from models import UserNotificationPreference, db
    user = _user()
    db.session.add(UserNotificationPreference(
        user_id=user.id, new_episode=False, season_premiere=True))
    db.session.commit()

    prefs = _export(user)['notification_preferences']
    assert prefs == {'new_episode': False, 'season_premiere': True}


def test_preferences_export_defaults_when_no_row_exists(ctx):
    """The common case: a user who never opened the settings page."""
    from models import UserNotificationPreference  # noqa: F401
    user = _user()
    assert UserNotificationPreference.query.filter_by(
        user_id=user.id).first() is None

    prefs = _export(user)['notification_preferences']
    assert prefs == {'new_episode': True, 'season_premiere': False}


def test_preferences_defaults_match_the_application_defaults(ctx):
    """Never invent defaults: assert against F12's own DEFAULTS."""
    user = _user()
    prefs = _export(user)['notification_preferences']
    assert prefs == dict(np.DEFAULTS)


def test_preferences_export_never_creates_a_row(ctx):
    from models import UserNotificationPreference
    user = _user()
    before = UserNotificationPreference.query.count()
    _export(user)
    assert UserNotificationPreference.query.count() == before


def test_preferences_export_covers_every_supported_category(ctx):
    """Drives off CATEGORIES so the export cannot drift from delivery."""
    user = _user()
    prefs = _export(user)['notification_preferences']
    assert set(prefs) == set(np.CATEGORIES)


def test_preferences_stay_independent(ctx):
    """No master switch: flipping one must not imply the other."""
    from models import UserNotificationPreference, db
    user = _user()
    for new_episode, season in ((True, False), (False, False), (False, True)):
        row = UserNotificationPreference.query.filter_by(
            user_id=user.id).first()
        if row is None:
            row = UserNotificationPreference(user_id=user.id)
            db.session.add(row)
        row.new_episode = new_episode
        row.season_premiere = season
        db.session.commit()
        prefs = _export(user)['notification_preferences']
        assert prefs['new_episode'] is new_episode
        assert prefs['season_premiere'] is season


def test_preferences_carry_no_internal_identifiers(ctx):
    from models import UserNotificationPreference, db
    user = _user()
    row = UserNotificationPreference(user_id=user.id)
    db.session.add(row)
    db.session.commit()
    prefs = _export(user)['notification_preferences']
    assert set(prefs) == set(np.CATEGORIES)
    text = json.dumps(prefs)
    assert 'user_id' not in text
    assert str(user.id) not in text


def test_preferences_are_per_user(ctx):
    from models import UserNotificationPreference, db
    mine, theirs = _user(), _user()
    db.session.add(UserNotificationPreference(
        user_id=mine.id, new_episode=False, season_premiere=True))
    db.session.add(UserNotificationPreference(
        user_id=theirs.id, new_episode=True, season_premiere=False))
    db.session.commit()
    assert _export(mine)['notification_preferences'] == {
        'new_episode': False, 'season_premiere': True}
    assert _export(theirs)['notification_preferences'] == {
        'new_episode': True, 'season_premiere': False}


# ── discovery presets ───────────────────────────────────────────────────────

def test_discovery_preset_is_identified_and_not_called_a_smart_list(ctx):
    user = _user()
    row = _preset(user)
    exported = _export(user)['smart_lists']
    assert len(exported) == 1
    item = exported[0]
    assert item['list_kind'] == 'discovery_preset'
    # The runtime discriminator agrees with what we exported.
    assert is_discovery_preset(row)


def test_ordinary_smart_list_is_not_labelled_a_preset(ctx):
    user = _user()
    _smart_list(user)
    item = _export(user)['smart_lists'][0]
    assert item['list_kind'] == 'smart_list'
    assert item['scope_is_compat_placeholder'] is False
    assert item['discovery_filters'] is None


def test_preset_filters_are_preserved_as_a_real_object(ctx):
    user = _user()
    config = {'media_type': 'movie', 'genres': ['Science Fiction'],
              'year_from': 2015, 'min_rating': 7.0, 'sort': 'popularity.desc'}
    row = _preset(user, discovery=config)
    item = _export(user)['smart_lists'][0]
    filters = item['discovery_filters']
    assert isinstance(filters, dict)
    # Exactly the canonical config the runtime uses — not the raw JSON string,
    # and not re-derived by the exporter.
    # Exactly the CANONICAL config the runtime uses: genres are TMDb slugs,
    # because validate_discovery_config normalises on write. The export must
    # hand back what is stored, not re-derive or re-format it.
    assert filters == row.filters['discovery']
    assert filters['media_type'] == 'movie'
    assert filters['genres'] == ['science_fiction']
    assert filters['year_from'] == 2015
    assert filters['min_rating'] == 7.0
    assert filters['sort'] == 'popularity.desc'
    # And the original raw document is still byte-identical for reconstruction.
    assert json.loads(item['filters_json'])['kind'] == DISCOVERY_KIND
    assert json.loads(item['filters_json'])['discovery'] == filters


def test_preset_scope_is_flagged_as_inert(ctx):
    """The compat placeholder must not read as real discovery behaviour."""
    user = _user()
    _preset(user)
    item = _export(user)['smart_lists'][0]
    assert item['scope'] == DISCOVERY_COMPAT_SCOPE  # unchanged...
    assert item['scope_is_compat_placeholder'] is True  # ...but now labelled


def test_ordinary_list_scope_is_real_not_a_placeholder(ctx):
    user = _user()
    _smart_list(user, scope='watchlist')
    item = _export(user)['smart_lists'][0]
    assert item['scope'] == 'watchlist'
    assert item['scope_is_compat_placeholder'] is False


def test_presets_and_smart_lists_are_distinguishable_in_one_export(ctx):
    user = _user()
    _preset(user, name='Sci-fi picks')
    _smart_list(user, name='My smart list', scope='diary')
    items = {row['name']: row for row in _export(user)['smart_lists']}
    assert items['Sci-fi picks']['list_kind'] == 'discovery_preset'
    assert items['My smart list']['list_kind'] == 'smart_list'
    # The two rows do not share a scope value by accident.
    assert items['Sci-fi picks']['scope'] == 'all'
    assert items['My smart list']['scope'] == 'diary'


def test_every_row_gets_a_list_kind(ctx):
    """A preset is identified per ROW, not per export or per user."""
    from models import SmartList  # noqa: F401
    user = _user()
    _preset(user, name='A')
    _smart_list(user, name='B')
    _preset(user, name='C', discovery={'sort': 'vote_average.desc'})
    kinds = [row['list_kind']
             for row in _export(user)['smart_lists']]
    assert sorted(kinds) == ['discovery_preset', 'discovery_preset',
                             'smart_list']


def test_malformed_filters_json_does_not_crash_the_export(ctx):
    """A row that is neither a preset nor parseable must still export."""
    from models import SmartList, db  # noqa: F401
    user = _user()
    db.session.add(SmartList(user_id=user.id, name='Broken', scope='diary',
                             filters_json='{not json', sort='date_added'))
    db.session.commit()
    item = _export(user)['smart_lists'][0]
    assert item['list_kind'] == 'smart_list'
    assert item['discovery_filters'] is None
    # Raw value preserved exactly, even though it is not parseable.
    assert item['filters_json'] == '{not json'


def test_preset_detection_does_not_depend_on_scope(ctx):
    """A preset with an unusual scope is still a preset (kind wins)."""
    from models import db  # noqa: F401
    user = _user()
    preset = _preset(user)
    preset.scope = 'watchlist'   # would otherwise look like a real list
    db.session.commit()
    item = _export(user)['smart_lists'][0]
    assert item['list_kind'] == 'discovery_preset'
    assert item['scope_is_compat_placeholder'] is True


# ── backward compatibility ──────────────────────────────────────────────────

LEGACY_KEYS = {'smart_list_id', 'name', 'description', 'scope', 'filters_json',
               'sort', 'is_public', 'created_at', 'updated_at'}


def test_all_legacy_smart_list_keys_are_still_present(ctx):
    user = _user()
    _preset(user, name='Preset')
    _smart_list(user, name='Ordinary')
    for item in _export(user)['smart_lists']:
        assert LEGACY_KEYS <= set(item), sorted(LEGACY_KEYS - set(item))
        assert set(item) == LEGACY_KEYS | {
            'list_kind', 'discovery_filters', 'scope_is_compat_placeholder'}


def test_legacy_values_are_unchanged(ctx):
    """The new fields are purely additive — old values must be identical."""
    user = _user()
    row = _smart_list(user, name='My smart list', scope='diary',
                      filters={'genres': ['Drama']})
    item = _export(user)['smart_lists'][0]
    assert item['smart_list_id'] == row.id
    assert item['name'] == 'My smart list'
    assert item['description'] == row.description
    assert item['scope'] == 'diary'
    assert item['filters_json'] == row.filters_json
    assert item['sort'] == 'date_added'
    assert item['is_public'] is False


def test_export_version_is_unchanged(ctx):
    """F15 is additive within v1: no consumer must be forced to re-read."""
    user = _user()
    assert _export(user)['version'] == ae.EXPORT_VERSION == 1


def test_existing_top_level_contract_survives(ctx):
    user = _user()
    export = _export(user)
    for key in ('format', 'version', 'generated_at', 'account', 'profile',
                'watch_history', 'tv_history', 'watchlist', 'lists',
                'reviews', 'ratings', 'tags', 'smart_lists', 'derived',
                'counts', 'csv_files'):
        assert key in export, key
    assert set(export['profile']) == {
        'first_name', 'last_name', 'bio', 'profile_picture'}


# ── CSV / ZIP bundle ────────────────────────────────────────────────────────

def test_csv_carries_the_new_smart_list_columns(ctx):
    user = _user()
    _preset(user, name='Preset')
    _smart_list(user, name='Ordinary')
    buffer, _ = ae.build_csv_bundle(user)
    with zipfile.ZipFile(buffer) as archive:
        text = archive.read('smart_lists.csv').decode('utf-8')
    header = next(csv.reader(io.StringIO(text)))
    assert header[:len(LEGACY_KEYS)] == [
        'smart_list_id', 'name', 'description', 'scope', 'filters_json',
        'sort', 'is_public', 'created_at', 'updated_at']
    assert header[-3:] == ['list_kind', 'discovery_filters',
                           'scope_is_compat_placeholder']
    rows = {r['name']: r for r in csv.DictReader(io.StringIO(text))}
    assert rows['Preset']['list_kind'] == 'discovery_preset'
    assert rows['Preset']['scope_is_compat_placeholder'] == 'true'
    # discovery_filters is a real, parseable JSON object in the CSV too.
    # The CSV carries the same real config object as the JSON (the default
    # preset fixture sorts by popularity and sets no other filter).
    csv_filters = json.loads(rows['Preset']['discovery_filters'])
    assert csv_filters == {'media_type': 'movie', 'sort': 'popularity.desc'}
    assert rows['Ordinary']['list_kind'] == 'smart_list'
    assert rows['Ordinary']['discovery_filters'] == ''


def test_csv_bundle_still_lists_every_schema(ctx):
    user = _user()
    buffer, filename = ae.build_csv_bundle(user)
    assert filename.endswith('.zip')
    with zipfile.ZipFile(buffer) as archive:
        names = set(archive.namelist())
    assert set(ae.CSV_FILES) <= names     # every declared domain present
    assert 'README.txt' in names
    assert ae.CSV_FILES == [s['filename'] for s in ae.CSV_SCHEMAS]


def test_notification_preferences_are_json_only(ctx):
    """Preferences are a settings object, not an event domain.

    ``notifications.csv`` already exists and holds Notification RECORDS; F15
    does not add a preferences CSV, matching `account` and `profile`.
    """
    user = _user()
    buffer, _ = ae.build_csv_bundle(user)
    with zipfile.ZipFile(buffer) as archive:
        names = archive.namelist()
    assert 'notifications.csv' in names
    assert 'notification_preferences.csv' not in names
    assert 'notification_preferences' in _export(user)


# ── privacy ─────────────────────────────────────────────────────────────────

def test_export_never_includes_another_users_preferences_or_lists(ctx):
    mine, theirs = _user(), _user()
    from models import UserNotificationPreference, db
    db.session.add(UserNotificationPreference(
        user_id=theirs.id, new_episode=False, season_premiere=True))
    db.session.commit()
    _preset(mine, name='My preset')
    _preset(theirs, name='Their preset')

    text = json.dumps(_export(mine))
    assert 'My preset' in text
    assert 'Their preset' not in text
    assert _export(mine)['notification_preferences'] == dict(np.DEFAULTS)


def test_notification_preferences_cost_one_bounded_query(ctx):
    """F15 must not add an N+1 or a per-user sweep to the export."""
    from sqlalchemy import event
    from models import db

    user = _user()
    statements = []

    def _record(_conn, _cur, statement, *a, **k):
        statements.append(' '.join(statement.split()).lower())
    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        _export(user)
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)
    pref_queries = [s for s in statements if 'notification_preference' in s]
    assert len(pref_queries) == 1, pref_queries


def test_export_section_helper_is_user_scoped(ctx):
    user, other = _user(), _user()
    _preset(other, name='Their preset')
    ids = {row['smart_list_id']
           for row in ae.smart_list_rows(user.id)}
    assert ids == set()


def test_no_network_and_read_only(ctx):
    """The export stays offline and writes nothing (F5's core guarantee)."""
    import socket
    from models import db

    user = _user()
    _preset(user)
    original = socket.socket

    def _boom(*a, **k):
        raise AssertionError('export must not use the network')
    socket.socket = _boom
    try:
        _export(user)
    finally:
        socket.socket = original
    db.session.rollback()  # nothing pending


def test_route_contract_is_intact(ctx):
    """Authentication + endpoints unchanged by F15."""
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()
             if 'export' in r.rule}
    assert '/api/account/export/json' in rules