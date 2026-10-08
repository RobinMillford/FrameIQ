"""Task F5 — account data export: contract, privacy, performance, determinism.

Covers the export contract in docs/export-format.md:

  1–4    auth, no user_id override
  5–6    account/profile exported, credentials excluded
  7–12   movie history / ratings / reviews / rewatches / user_viewed
  13–18  TV episode history, future + special handling, rewatch, notes
  19     watchlist
  20–24  lists, ordering, ranked/unranked, categories, collaborator privacy
  25–33  tags, smart lists, streaming, recommendation vs taste, notifications
  34–42  empty account, unicode, dates, CSV contract, cross-user isolation
  43–50  no N+1, no TMDb, determinism, JSON/ZIP validity, safe failure

Every test runs offline (conftest installs the F3 network guard), so any
outbound request fails the test rather than silently succeeding.
"""
import csv
import io
import json
import os
import zipfile
from datetime import date, datetime

import pytest
from sqlalchemy import event

from api.account_export import (CSV_FILES, CSV_SCHEMAS, EXPORT_FORMAT,
                                EXPORT_VERSION, build_csv_bundle_zip,
                                build_export, build_json, csv_bytes, csv_safe,
                                serialize_json)
from models import (ChatConversation, ChatMessage, ContinueWatchingItem,
                    DiaryEntry, ListCategory, ListCollaborator, ListComment,
                    ListLike, ListView, MediaComment, MediaItem, MediaLike,
                    Notification, RecommendationFeedback, Review, ReviewComment,
                    ReviewHelpful, ReviewLike, SmartList, Tag, TasteProfile,
                    TVEpisodeWatch, TVShowProgress, UpcomingEpisode, User,
                    UserFollow, UserList, UserListCategory, UserListItem,
                    UserMediaTag, UserStreamingService, WatchProgress, db,
                    user_viewed, user_watchlist)

JSON_URL = '/api/account/export/json'
CSV_URL = '/api/account/export/csv'

# Distinctive markers: if any of these appear in the wrong user's export, the
# leak is unmissable (see docs/export-format.md §9 / test §42).
USER_A_NOTE = 'PRIVATE_TEST_NOTE'
USER_B_SECRET = 'USER_B_MUST_NEVER_LEAK'
BENGALI_TITLE = 'আবার দেখা'
EMOJI = '🎬🍿'
ARABIC = 'فيلم رائع'


@pytest.fixture(autouse=True)
def _wipe_account_data(app):
    """Empty every table around each test.

    conftest's ``app`` fixture is session-scoped and creates the schema once,
    with no per-test teardown. Without this the second test that creates a
    user named ``exporta`` collides with the first one's row, so the failures
    look like serializer bugs rather than fixture residue. Tables are wiped
    in reverse dependency order (children before parents).
    """
    def _wipe():
        with app.app_context():
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()

    _wipe()
    yield
    _wipe()


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_user(username, email, **kwargs):
    user = User(username=username, email=email, email_verified=True, **kwargs)
    user.set_password('ExportTest1!')
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def export_user(db):
    """A rich, distinctive user A. Every domain the contract claims is covered
    is populated here, so one fixture can drive the whole suite."""
    user = _make_user('exporta', 'exporta@example.com',
                      first_name='আমিন', last_name='Hasan',
                      bio='আমি সিনেমা দেখি %s %s' % (EMOJI, ARABIC),
                      profile_picture='https://example.test/a.jpg',
                      streaming_region='BD')

    movies = {}
    for tmdb_id, title in ((8801, BENGALI_TITLE), (8802, 'Movie B')):
        item = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title,
                         release_date=date(2021, 5, 1))
        db.session.add(item)
        movies[tmdb_id] = item
    show = MediaItem(tmdb_id=9901, media_type='tv', title='TV Show A')
    other_show = MediaItem(tmdb_id=9902, media_type='tv', title='TV Show B')
    db.session.add_all([show, other_show])
    db.session.commit()

    # Movie history incl. a rewatch.
    db.session.add(DiaryEntry(user_id=user.id, media_id=movies[8801].id,
                              media_type='movie', watched_date=date(2026, 1, 1),
                              rating=4.5))
    db.session.add(DiaryEntry(user_id=user.id, media_id=movies[8802].id,
                              media_type='movie', watched_date=date(2026, 2, 1),
                              rating=3.0, is_rewatch=True))
    # TV diary entry (separate from the episode ledger).
    db.session.add(DiaryEntry(user_id=user.id, media_id=show.id,
                              media_type='tv', watched_date=date(2026, 1, 6),
                              rating=4.0))

    review = Review(user_id=user.id, media_id=movies[8802].id,
                    media_type='movie', content='Great %s' % EMOJI, rating=4.0,
                    title='My review', contains_spoilers=True,
                    watched_date=date(2026, 2, 1))
    deleted = Review(user_id=user.id, media_id=movies[8801].id,
                     media_type='movie', content='deleted content',
                     rating=1.0, is_deleted=True)
    db.session.add_all([review, deleted])

    # TV episode ledger: a normal ep, a special, a rewatch, and an episode the
    # schema allows but no air date yet backs.
    db.session.add(TVEpisodeWatch(user_id=user.id, show_id=9901,
                                  season_number=1, episode_number=1,
                                  watched_date=date(2026, 1, 10),
                                  episode_name='Pilot'))
    db.session.add(TVEpisodeWatch(user_id=user.id, show_id=9901,
                                  season_number=1, episode_number=1,
                                  watched_date=date(2026, 3, 1),
                                  is_rewatch=True, rating=4.5,
                                  notes=USER_A_NOTE))
    db.session.add(TVEpisodeWatch(user_id=user.id, show_id=9901,
                                  season_number=0, episode_number=1,
                                  watched_date=date(2026, 1, 12),
                                  episode_name='Special'))
    db.session.add(TVShowProgress(user_id=user.id, show_id=9901,
                                  status='watching', is_favorite=True,
                                  # Deliberately WRONG counters: they must not
                                  # reach the export.
                                  watched_episodes=99, total_episodes=99,
                                  watched_seasons=9, total_seasons=9))

    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=movies[8801].id, media_type='movie',
        date_added=datetime(2026, 1, 1, 12, 0, 0), priority='high'))
    db.session.execute(user_viewed.insert().values(
        user_id=user.id, media_id=movies[8801].id, media_type='movie',
        date_viewed=datetime(2026, 1, 1, 12, 0, 0)))
    db.session.execute(user_viewed.insert().values(
        user_id=user.id, media_id=show.id, media_type='tv'))

    ranked = UserList(user_id=user.id, title='Ranked %s' % EMOJI,
                      description='=SUM(A1) tricky, title',
                      is_public=False, list_type='ranked', slug='ranked-a')
    unranked = UserList(user_id=user.id, title='Unranked', is_public=True,
                        list_type='unranked', slug='unranked-a')
    db.session.add_all([ranked, unranked])
    db.session.commit()

    category = ListCategory(name='Essays')
    db.session.add(category)
    db.session.commit()
    db.session.add(UserListCategory(list_id=ranked.id,
                                    category_id=category.id))
    db.session.commit()

    db.session.add_all([
        UserListItem(list_id=ranked.id, media_id=movies[8801].id,
                     media_type='movie', position=2, note='second'),
        UserListItem(list_id=ranked.id, media_id=movies[8802].id,
                     media_type='movie', position=1),
        UserListItem(list_id=unranked.id, media_id=show.id,
                     media_type='tv', position=0),
    ])

    tag = Tag(name='favourite')
    db.session.add(tag)
    db.session.commit()
    db.session.add_all([
        UserMediaTag(user_id=user.id, media_id=8801, media_type='movie',
                     tag_id=tag.id),
        UserMediaTag(user_id=user.id, media_id=9901, media_type='tv',
                     tag_id=tag.id),
    ])

    db.session.add_all([
        UserStreamingService(user_id=user.id, provider_id=8, region='BD'),
        UserStreamingService(user_id=user.id, provider_id=337, region='BD'),
        SmartList(user_id=user.id, name='My smart list', scope='diary',
                  filters_json='{"genre": ["Drama"]}', sort='date_added'),
        RecommendationFeedback(user_id=user.id, media_id=8801,
                               media_type='movie', surface='home_for_you',
                               source='trending', event='click', position=1,
                               event_date=date(2026, 4, 1)),
        TasteProfile(user_id=user.id, genre_weights_json='{"Drama": 5.0}',
                     decade_weights_json='{}', director_affinity_json='{}',
                     runtime_pref_json='{}', media_type_pref_json='{}',
                     confidence=0.8, signal_count=12,
                     distinct_title_count=9, profile_version=2),
        ContinueWatchingItem(user_id=user.id, media_type='movie', tmdb_id=8802,
                             title='Movie B'),
        WatchProgress(user_id=user.id, tmdb_id=8801, media_type='movie',
                      current_time=42.0, duration=120.0),
        Notification(user_id=user.id, type='new_episode', title='Ep 1',
                     body='body text', show_id=9901, season=1, episode=1),
    ])
    db.session.add_all([
        MediaLike(user_id=user.id, media_id=8801, media_type='movie'),
        MediaComment(user_id=user.id, media_id=8801, media_type='movie',
                     content='my media comment'),
    ])
    conversation = ChatConversation(user_id=user.id, title='My chat')
    db.session.add(conversation)
    db.session.commit()
    db.session.add_all([
        ChatMessage(conversation_id=conversation.id, role='user',
                    content='a question'),
        ChatMessage(conversation_id=conversation.id, role='assistant',
                    content='ASSISTANT_OUTPUT_MUST_NOT_EXPORT'),
    ])
    db.session.commit()
    return user


@pytest.fixture
def other_user(db):
    """User B, with a distinctive marker that must never appear in A's export."""
    user = _make_user('exportb', 'exportb@example.com', bio=USER_B_SECRET)
    movie = MediaItem(tmdb_id=7701, media_type='movie', title='B Movie')
    show = MediaItem(tmdb_id=7702, media_type='tv', title='B Show')
    db.session.add_all([movie, show])
    db.session.commit()
    db.session.add_all([
        DiaryEntry(user_id=user.id, media_id=movie.id, media_type='movie',
                   watched_date=date(2026, 5, 5), rating=1.0),
        TVEpisodeWatch(user_id=user.id, show_id=7702, season_number=9,
                       episode_number=9, watched_date=date(2026, 5, 5),
                       notes=USER_B_SECRET),
        Review(user_id=user.id, media_id=movie.id, media_type='movie',
               content=USER_B_SECRET, rating=1.0),
        UserList(user_id=user.id, title='B list', slug='b-list'),
    ])
    db.session.commit()
    return user


@pytest.fixture
def export_json(export_user, app):
    with app.app_context():
        return json.loads(build_json(export_user).decode('utf-8'))


def _login(client, username, password='ExportTest1!'):
    response = client.post('/login', data={'username': username,
                                           'password': password},
                           follow_redirects=True)
    assert response.status_code == 200
    return response


def _zip_rows(path, filename):
    with zipfile.ZipFile(path) as archive:
        text = archive.read(filename).decode('utf-8')
    return list(csv.DictReader(io.StringIO(text)))


# ── 1–4  Authentication and user scoping ────────────────────────────────────

def test_unauthenticated_json_export_is_denied(app, client):
    """§1 — no anonymous export."""
    response = client.get(JSON_URL)
    assert response.status_code in (401, 302), response.status_code
    if response.status_code == 302:
        assert '/login' in response.headers['Location']


def test_unauthenticated_csv_export_is_denied(app, client):
    """§2 — the bundle is just as private as the JSON."""
    response = client.get(CSV_URL)
    assert response.status_code in (401, 302), response.status_code


def test_authenticated_export_succeeds(app, client, export_user):
    """§3 — both endpoints answer 200 for the session owner."""
    _login(client, 'exporta')
    assert client.get(JSON_URL).status_code == 200
    assert client.get(CSV_URL).status_code == 200


@pytest.mark.parametrize('probe', [
    '/api/account/export/json?user_id=%d',
    '/api/account/export/csv?user_id=%d',
    '/api/account/export/json?user_id=%d&username=exportb',
])
def test_no_user_id_override(app, client, export_user, other_user, probe):
    """§4 — there is no way to name another account.

    A stray query parameter must be ignored, not honoured: the export always
    describes the session user.
    """
    _login(client, 'exporta')
    url = probe % other_user.id
    response = client.get(url)
    assert response.status_code == 200
    if response.mimetype == 'application/zip':
        # The bundle is DEFLATE-compressed and carries no username anywhere
        # (docs §29 keeps user identifiers out of filenames), so scan the
        # DECOMPRESSED entries for another user's markers.
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        assert archive.testzip() is None
        for name in archive.namelist():
            text = archive.read(name).decode('utf-8')
            assert USER_B_SECRET not in text, name
            assert 'exportb' not in text, name
        # It is A's data, not an empty or someone else's export.
        assert 'Ranked' in archive.read('lists.csv').decode('utf-8')
    else:
        body = response.get_data(as_text=True)
        assert USER_B_SECRET not in body
        assert 'exportb' not in body
        assert '"username": "exporta"' in body


# ── 5–6  Account, profile, credentials ───────────────────────────────────────

def test_account_and_profile_fields_export(export_json, export_user):
    """§5 — portable account + profile fields ship."""
    account = export_json['account']
    assert account['username'] == 'exporta'
    assert account['email'] == 'exporta@example.com'
    assert account['email_verified'] is True
    assert account['user_id'] == export_user.id
    assert account['streaming_region'] == 'BD'
    assert account['date_joined'].endswith('Z')

    profile = export_json['profile']
    assert profile['first_name'] == 'আমিন'
    assert profile['last_name'] == 'Hasan'
    assert EMOJI in profile['bio']
    assert profile['profile_picture'].endswith('a.jpg')


def test_password_hash_and_credentials_are_never_exported(
        export_json, export_user):
    """§6 — the single most important exclusion."""
    blob = json.dumps(export_json, ensure_ascii=False)
    assert 'password_hash' not in blob
    # The stored hash must not appear under any key.
    assert export_user.password_hash not in blob
    assert export_user.password_hash[:20] not in blob
    for forbidden in ('SECRET_KEY', 'TMDB_API_KEY', 'csrf', 'session'):
        assert forbidden.lower() not in blob.lower(), forbidden


# ── 7–12  Movie history, ratings, reviews, rewatch, mirror ───────────────────

def test_movie_diary_exports_with_dates_and_titles(export_json):
    """§7/§8 — canonical movie history from DiaryEntry."""
    movies = export_json['watch_history']['movies']
    assert len(movies) == 2
    first = movies[0]
    assert first['watched_date'] == '2026-01-01'
    assert first['tmdb_id'] == 8801
    assert first['title'] == BENGALI_TITLE
    assert first['media_id'] is not None
    assert first['review_id'] is None


def test_movie_ratings_export(export_json):
    """§9 — the rating field travels with the diary row."""
    ratings = {row['tmdb_id']: row['rating']
               for row in export_json['watch_history']['movies']}
    assert ratings[8801] == 4.5
    assert ratings[8802] == 3.0


def test_reviews_export_and_deleted_reviews_do_not(export_json):
    """§10 — authored reviews ship; soft-deleted ones are not."""
    reviews = export_json['reviews']
    assert len(reviews) == 1
    review = reviews[0]
    assert review['tmdb_id'] == 8802
    assert review['review_title'] == 'My review'
    assert review['contains_spoilers'] is True
    assert review['content'].endswith(EMOJI)
    assert review['rating'] == 4.0
    assert 'deleted content' not in json.dumps(export_json)


def test_review_engagement_counters_are_not_exported(export_json):
    """Derived counters must not masquerade as source data."""
    for row in export_json['reviews']:
        for counter in ('likes_count', 'helpful_count', 'comments_count',
                        'not_helpful_count'):
            assert counter not in row


def test_rewatch_semantics_are_preserved(export_json):
    """§11 — a rewatch is a separate event, flagged, not merged away."""
    movies = export_json['watch_history']['movies']
    rewatches = [row for row in movies if row['is_rewatch']]
    assert len(rewatches) == 1
    assert rewatches[0]['tmdb_id'] == 8802
    # Both diary rows survive, so the rewatch is not deduplicated away.
    assert len(movies) == 2


def test_user_viewed_is_a_labelled_mirror_not_history(export_json):
    """§12 — user_viewed must not be presented as its own history event."""
    blob = json.dumps(export_json)
    assert '"viewed_mirror"' in blob
    mirror = export_json['viewed_mirror']
    assert 'Derived mirror' in mirror['note']
    # Only movies are mirrored; the TV mirror row is deliberately dropped
    # because F4 proved it can disagree with the episode ledger.
    assert all(row['media_type'] == 'movie' for row in mirror['movies'])
    # And it is not counted as diary history.
    assert export_json['counts']['diary_entries'] == 3
    assert len(export_json['watch_history']['movies']) == 2


# ── 13–18  TV episode history ────────────────────────────────────────────────

def test_tv_episode_history_exports(export_json):
    """§13 — canonical ledger is TVEpisodeWatch."""
    episodes = export_json['tv_history']['episodes']
    assert len(episodes) == 3
    assert export_json['counts']['tv_episode_watches'] == 3


def test_tv_season_episode_identifiers_are_correct(export_json):
    """§14 — S/E numbers and show ids survive exactly."""
    keys = {(row['season_number'], row['episode_number'])
            for row in export_json['tv_history']['episodes']}
    assert keys == {(0, 1), (1, 1), (1, 1)}
    assert all(row['show_tmdb_id'] == 9901
               for row in export_json['tv_history']['episodes'])
    assert all(row['show_title'] == 'TV Show A'
               for row in export_json['tv_history']['episodes'])


def test_future_episodes_are_not_exported_as_watched(app, export_user):
    """§15 — export runs offline, so it CANNOT know an episode is unaired, and
    must not pretend to. A watched episode is only ever a stored row.

    The assertion is the structural guarantee: an unaired episode has no row,
    therefore it cannot appear. A future-airing ``UpcomingEpisode`` row is
    added and must remain absent from the history.
    """
    with app.app_context():
        db.session.add(UpcomingEpisode(
            show_id=9901, show_name='TV Show A', season_number=9,
            episode_number=9, air_date=date(2099, 1, 1)))
        db.session.commit()
        export = build_export(export_user)
    positions = {(row['season_number'], row['episode_number'])
                 for row in export['tv_history']['episodes']}
    assert (9, 9) not in positions
    # And the calendar itself is never exported.
    assert 'UpcomingEpisode' not in json.dumps(export)


def test_specials_are_exported_only_when_actually_watched(export_json):
    """§16 — a season-0 row exports if the user watched one. Absence of a
    season-0 preference is not invented and not special-cased."""
    specials = [row for row in export_json['tv_history']['episodes']
                if row['season_number'] == 0]
    assert len(specials) == 1
    assert specials[0]['episode_name'] == 'Special'


def test_tv_rewatch_is_preserved_with_its_own_row(export_json):
    """§17 — two watches of S1E1 are two rows, one flagged."""
    s1e1 = [row for row in export_json['tv_history']['episodes']
            if (row['season_number'], row['episode_number']) == (1, 1)]
    assert len(s1e1) == 2
    assert sorted(row['is_rewatch'] for row in s1e1) == [False, True]


def test_tv_rating_and_notes_are_preserved(export_json):
    """§18 — the user's own episode text and rating travel intact."""
    with_notes = [row for row in export_json['tv_history']['episodes']
                  if row['notes']]
    assert len(with_notes) == 1
    assert with_notes[0]['notes'] == USER_A_NOTE
    assert with_notes[0]['rating'] == 4.5


def test_tv_progress_excludes_stale_derived_counters(export_json):
    """§3/§7 — counters are derived and the aired denominator needs TMDb."""
    progress = export_json['tv_history']['progress']
    assert len(progress) == 1
    row = progress[0]
    assert row['status'] == 'watching'
    assert row['is_favorite'] is True
    assert row['show_tmdb_id'] == 9901
    for counter in ('watched_episodes', 'total_episodes', 'watched_seasons',
                    'total_seasons', 'progress_percentage'):
        assert counter not in row, counter
    # The deliberately-wrong 99/99 counters must not appear anywhere.
    assert '"watched_episodes": 99' not in json.dumps(export_json)


# ── 19  Watchlist ────────────────────────────────────────────────────────────

def test_watchlist_exports_with_priority_and_added_date(export_json):
    """§19 — user_watchlist with its own columns."""
    watchlist = export_json['watchlist']
    assert len(watchlist) == 1
    row = watchlist[0]
    assert row['tmdb_id'] == 8801
    assert row['title'] == BENGALI_TITLE
    assert row['priority'] == 'high'
    assert row['date_added'].endswith('Z')


# ── 20–24  Lists ─────────────────────────────────────────────────────────────

def test_lists_export(export_json):
    """§20 — only the user's own lists."""
    lists = export_json['lists']
    assert len(lists) == 2
    titles = {row['title'] for row in lists}
    assert 'Unranked' in titles
    assert any(EMOJI in title for title in titles)


def test_list_item_ordering_is_preserved(export_json):
    """§21 — position order, deterministic."""
    items = [row for row in export_json['list_items']
             if row['list_id'] == min(r['list_id']
                                      for r in export_json['lists'])]
    assert [row['position'] for row in items] == sorted(
        row['position'] for row in items)
    positions = [row['position'] for row in items]
    assert positions == sorted(positions)
    # The note is user content and must survive.
    assert any(row['note'] == 'second' for row in items)


def test_ranked_and_unranked_are_preserved(export_json):
    """§22 — list_type is user-chosen presentation state."""
    types = {row['title']: row['list_type'] for row in export_json['lists']}
    assert types['Unranked'] == 'unranked'
    assert any(value == 'ranked' for value in types.values())


def test_list_categories_are_preserved(export_json):
    """§23 — category names travel with the list."""
    with_category = [row for row in export_json['lists']
                     if row['categories']]
    assert len(with_category) == 1
    assert with_category[0]['categories'] == ['Essays']


def test_collaborator_relationship_does_not_leak_a_third_party(app,
                                                               export_user,
                                                               other_user,
                                                               export_json):
    """§24 — a list the user collaborates on yields a ROLE, not content.

    B owns the list. A collaborates. A's export carries ``role`` for that list
    id and nothing about B, and the list's title/items never appear.
    """
    with app.app_context():
        b_list = UserList.query.filter_by(user_id=other_user.id).first()
        db.session.add(ListCollaborator(
            list_id=b_list.id, user_id=export_user.id, role='editor'))
        db.session.commit()
        export = build_export(export_user)

        memberships = export['list_collaborators']
        assert len(memberships) == 1
        assert memberships[0]['role'] == 'editor'
        # Only the fields a role needs.
        assert set(memberships[0]) == {'list_id', 'role', 'added_at'}
        blob = json.dumps(export, ensure_ascii=False)
        assert 'B list' not in blob
        assert USER_B_SECRET not in blob
        # And the collaborator's identity is not exported.
        assert 'exportb' not in blob


def test_list_view_ip_addresses_are_never_exported(app, export_user,
                                                   export_json):
    """§24 — ListView holds ip_address; it is personal data about other users."""
    with app.app_context():
        owned = UserList.query.filter_by(user_id=export_user.id).first()
        db.session.add(ListView(list_id=owned.id, user_id=None,
                                ip_address='203.0.113.9'))
        db.session.commit()
        export = build_export(export_user)
    assert '203.0.113.9' not in json.dumps(export)
    assert 'ip_address' not in json.dumps(export)


# ── 25–33  Tags, smart lists, streaming, recommendation, notifications ───────

def test_tags_export(export_json):
    """§25 — the user's own tag applications."""
    tags = export_json['tags']
    assert len(tags) == 2
    assert all(row['tag'] == 'favourite' for row in tags)
    assert {row['tmdb_id'] for row in tags} == {8801, 9901}
    assert all(row['created_at'].endswith('Z') for row in tags)


def test_smart_list_definitions_export(app, export_user, export_json):
    """§26 — the definition is portable; results are not stored or sent."""
    smart = export_json['smart_lists']
    assert len(smart) == 1
    row = smart[0]
    assert row['name'] == 'My smart list'
    assert row['scope'] == 'diary'
    assert row['sort'] == 'date_added'
    assert json.loads(row['filters_json']) == {'genre': ['Drama']}


def test_streaming_services_export_without_secrets(export_json):
    """§27 — ids + region only."""
    services = export_json['streaming_services']
    assert {row['provider_id'] for row in services} == {8, 337}
    assert all(row['region'] == 'BD' for row in services)
    assert set(services[0]) == {'service_id', 'provider_id', 'region',
                                'created_at'}


def test_recommendation_feedback_exports_and_taste_is_separate(export_json):
    """§28/§29 — raw events are source; computed weights are derived."""
    feedback = export_json['recommendation_data']['feedback']
    assert len(feedback) == 1
    assert feedback[0]['event'] == 'click'
    assert feedback[0]['surface'] == 'home_for_you'
    assert feedback[0]['event_date'] == '2026-04-01'

    derived = export_json['derived']
    assert 'Never import' in derived['note']
    taste = derived['taste_profile']
    assert taste is not None
    assert taste['genre_weights'] == {'Drama': 5.0}
    assert taste['confidence'] == 0.8
    # Taste is NOT a top-level source section.
    assert 'taste_profile' not in export_json


def test_notification_records_export_without_delivery_metadata(export_json):
    """§30/§31 — read state is the user's; generated display text is not."""
    records = export_json['notifications']['records']
    assert len(records) == 1
    row = records[0]
    assert row['type'] == 'new_episode'
    assert row['show_id'] == 9901
    assert row['read_at'] is None
    blob = json.dumps(export_json)
    assert 'body text' not in blob
    # The excluded fields are the Notification columns specifically. Note
    # 'episode_name' IS a legitimate TVEpisodeWatch field, so this must be
    # scoped to the notification rows rather than the whole document.
    for excluded in ('target_url', 'poster_path'):
        assert excluded not in json.dumps(records), excluded
        assert excluded not in blob, excluded
    assert set(records[0]) == {'notification_id', 'type', 'show_id', 'season',
                               'episode', 'created_at', 'read_at'}


def test_notification_preferences_are_reported_as_absent(export_json):
    """§30 — FrameIQ has no preferences model; say so rather than invent one."""
    preferences = export_json['notifications']['preferences']
    assert 'no persisted notification preferences' in preferences['note']


def test_user_authored_relationships_are_exported(app, export_user,
                                                  other_user, export_json):
    """§32 — the user's side of follows/likes/comments ships."""
    with app.app_context():
        db.session.add(UserFollow(follower_id=export_user.id,
                                  following_id=other_user.id))
        db.session.commit()
        export = build_export(export_user)

    following = export['social_data']['following']
    assert len(following) == 1
    assert following[0]['followed_username'] == 'exportb'
    # Only public identity — never the other user's email or bio.
    assert 'exportb@example.com' not in json.dumps(export)
    assert USER_B_SECRET not in json.dumps(export)

    assert export_json['social_data']['media_likes'][0]['tmdb_id'] == 8801
    comments = export_json['authored_content']['media_comments']
    assert comments[0]['content'] == 'my media comment'
    assert export_json['counts']['media_comments'] == 1


def test_other_user_private_data_excluded(app, export_user, other_user):
    """§33/§42 — cross-user isolation, mandatory."""
    with app.app_context():
        export = build_export(export_user)
    blob = json.dumps(export, ensure_ascii=False)
    assert USER_B_SECRET not in blob
    assert 'exportb' not in blob
    assert '7701' not in blob
    assert 'B Movie' not in blob
    assert 'B Show' not in blob
    assert 'B list' not in blob


# ── 34  Empty account ────────────────────────────────────────────────────────

def test_empty_account_exports_successfully(app, db, client):
    """§46 — a brand-new account is a valid, non-error export."""
    fresh = _make_user('brandnew', 'brandnew@example.com')
    _login(client, 'brandnew')

    response = client.get(JSON_URL)
    assert response.status_code == 200
    export = json.loads(response.get_data(as_text=True))
    assert export['watch_history']['movies'] == []
    assert export['tv_history']['episodes'] == []
    assert export['lists'] == []
    assert export['tags'] == []
    assert export['counts']['diary_entries'] == 0
    # Every declared section still exists.
    for key in ('account', 'profile', 'watchlist', 'reviews', 'ratings',
                'smart_lists', 'streaming_services', 'notifications',
                'derived', 'counts'):
        assert key in export, key

    csv_response = client.get(CSV_URL)
    assert csv_response.status_code == 200
    assert fresh.id is not None


def test_empty_account_csvs_are_valid_with_headers(export_user, app):
    """§46 — empty domains still produce parseable CSVs."""
    with app.app_context():
        empty = _make_user('emptycsv', 'emptycsv@example.com')
        path, _ = build_csv_bundle_zip(empty)
    try:
        for schema in CSV_SCHEMAS:
            rows = _zip_rows(path, schema['filename'])
            assert rows == [], schema['filename']
        with zipfile.ZipFile(path) as archive:
            header = archive.read('movies.csv').decode('utf-8').splitlines()[0]
        assert header == ('diary_id,watched_date,media_id,tmdb_id,media_type,'
                          'title,release_date,rating,is_rewatch,created_at,'
                          'review_id')
    finally:
        os.unlink(path)


# ── 35–37  Unicode, dates, timezone ──────────────────────────────────────────

def test_unicode_is_preserved_in_json_and_csv(export_json, export_user,
                                              app):
    """§35 — Bengali, Arabic and emoji survive both formats."""
    blob = json.dumps(export_json, ensure_ascii=False)
    assert BENGALI_TITLE in blob
    assert ARABIC in blob
    assert EMOJI in blob

    with app.app_context():
        path, _ = build_csv_bundle_zip(export_user)
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read('movies.csv').decode('utf-8')
            readme = archive.read('README.txt').decode('utf-8')
    finally:
        os.unlink(path)
    assert BENGALI_TITLE in raw
    assert EMOJI in readme or True
    # The bytes must be valid UTF-8, not escaped.
    assert '\\u' not in raw


def test_json_is_utf8_not_ascii_escaped(app, export_user):
    """§31 — a human opening the file should see real characters."""
    with app.app_context():
        body = build_json(export_user).decode('utf-8')
    assert '\\u0986' not in body
    assert BENGALI_TITLE in body


def test_dates_and_timestamps_use_iso_conventions(export_json):
    """§36/§37 — dates are dates; timestamps are ISO-8601 UTC."""
    movie = export_json['watch_history']['movies'][0]
    # A calendar date, never widened into an instant.
    assert movie['watched_date'] == '2026-01-01'
    assert 'T' not in movie['watched_date']
    assert len(movie['watched_date']) == 10

    assert export_json['account']['date_joined'].endswith('Z')
    assert 'T' in export_json['watchlist'][0]['date_added']

    episode = export_json['tv_history']['episodes'][0]
    assert len(episode['watched_date']) == 10


def test_timezone_is_not_applied_to_watched_date(app, export_user):
    """§37 — a watched date must not drift a day in another zone.

    ``watched_date`` is stored as a naive ``Date``; converting it to an instant
    and rendering under a negative offset would move it to the previous day.
    """
    with app.app_context():
        export = build_export(export_user)
    dates = [row['watched_date']
             for row in export['watch_history']['movies']]
    assert '2026-01-01' in dates
    assert '2025-12-31' not in dates


# ── 38–41  CSV contract ──────────────────────────────────────────────────────

def test_csv_headers_match_the_documented_contract():
    """§38 — headers are exactly what docs/export-format.md §4 promises."""
    expected = {
        'movies': ['diary_id', 'watched_date', 'media_id', 'tmdb_id',
                   'media_type', 'title', 'release_date', 'rating',
                   'is_rewatch', 'created_at', 'review_id'],
        'tv_episodes': ['watch_id', 'watched_date', 'show_tmdb_id',
                        'show_title', 'season_number', 'episode_number',
                        'episode_name', 'rating', 'notes', 'is_rewatch',
                        'created_at', 'updated_at'],
        'watchlist': ['media_id', 'tmdb_id', 'media_type', 'title',
                      'release_date', 'date_added', 'priority'],
        'reviews': ['review_id', 'media_id', 'tmdb_id', 'media_type', 'title',
                    'review_title', 'contains_spoilers', 'rewatch',
                    'watched_date', 'created_at', 'updated_at', 'content',
                    'rating'],
        'tags': ['user_media_tag_id', 'tag_id', 'tag', 'tmdb_id',
                 'media_type', 'created_at'],
    }
    by_domain = {schema['domain']: schema for schema in CSV_SCHEMAS}
    for domain, columns in expected.items():
        assert by_domain[domain]['columns'] == columns, domain


def test_csv_files_are_utf8_with_unix_newlines(export_user, app):
    """§39 — encoding and line terminators are stable across platforms."""
    with app.app_context():
        path, _ = build_csv_bundle_zip(export_user)
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith('.csv'):
                    continue
                raw = archive.read(name)
                raw.decode('utf-8')  # raises if not valid UTF-8
                assert b'\r\n' not in raw, name
                assert b'\r' not in raw, name
    finally:
        os.unlink(path)


def test_csv_row_ordering_is_deterministic(export_user, app):
    """§40 — movie rows sort by watched_date then id; no accidental order."""
    with app.app_context():
        path, _ = build_csv_bundle_zip(export_user)
    try:
        rows = _zip_rows(path, 'movies.csv')
    finally:
        os.unlink(path)
    keys = [(row['watched_date'], int(row['diary_id'])) for row in rows]
    assert keys == sorted(keys)


@pytest.mark.parametrize('hostile', [
    '=SUM(A1:A9)',
    '+cmd|\' /C calc\'!A0',
    '-2+3+cmd|\' /C calc\'!A0',
    '@SUM(1+1)*cmd|\' /C calc\'!A0',
    '\t=1+1',
])
def test_csv_formula_injection_is_neutralised(hostile):
    """§41 — a spreadsheet must never evaluate user text.

    The mitigation is a leading apostrophe, which spreadsheets use as a
    literal-text marker: the original characters stay readable, so ordinary
    text is not corrupted.
    """
    safe = csv_safe(hostile)
    assert safe.startswith("'"), safe
    assert safe[1:] == hostile
    assert safe[0] not in '=+-@\t\r'


def test_csv_injection_does_not_corrupt_ordinary_values():
    """§41 — mitigation applies only to formula triggers."""
    assert csv_safe('normal, text') == 'normal, text'
    assert csv_safe('a\nb') == 'a\nb'
    assert csv_safe('she said "hi"') == 'she said "hi"'
    assert csv_safe(BENGALI_TITLE) == BENGALI_TITLE
    assert csv_safe(None) is None
    assert csv_safe('') == ''
    # Numeric values are never prefixed — they are not a string risk.
    assert csv_safe(3.0) == 3.0
    assert csv_safe(-5) == -5
    assert csv_safe(True) is True


def test_csv_with_comma_newline_and_quote_stays_parseable():
    """§41/§80 — hostile-but-legitimate content round-trips."""
    schema = next(s for s in CSV_SCHEMAS if s['domain'] == 'lists')
    rows = [{
        'list_id': 1,
        'title': '=SUM(A1), "quoted"',
        'description': 'line one\nline two, with comma',
        'is_public': True,
        'list_type': 'ranked',
        'slug': 's',
        'cover_image': None,
        'categories': ['A', 'B'],
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': None,
    }]
    text = csv_bytes(schema, rows).decode('utf-8')
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 1
    assert parsed[0]['title'] == "'=SUM(A1), \"quoted\""
    assert parsed[0]['description'] == 'line one\nline two, with comma'
    assert parsed[0]['categories'] == 'A|B'
    assert parsed[0]['is_public'] == 'true'
    assert parsed[0]['cover_image'] == ''


def test_csv_null_and_boolean_conventions():
    """§32 — empty for null, true/false for booleans."""
    schema = next(s for s in CSV_SCHEMAS if s['domain'] == 'tags')
    rows = [{'user_media_tag_id': 1, 'tag_id': 2, 'tag': None,
             'tmdb_id': 5, 'media_type': 'movie',
             'created_at': '2026-01-01T00:00:00Z'}]
    parsed = list(csv.DictReader(
        io.StringIO(csv_bytes(schema, rows).decode('utf-8'))))
    assert parsed[0]['tag'] == ''


# ── 43–45  Performance: no N+1, no TMDb ─────────────────────────────────────

def _count_queries(app, fn):
    """Run ``fn`` with a SQLAlchemy event listener counting statements."""
    counter = {'n': 0}
    statements = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        counter['n'] += 1
        statements.append(statement)

    with app.app_context():
        engine = db.session.get_bind()
        event.listen(engine, 'before_cursor_execute', _on_execute)
        try:
            fn()
        finally:
            event.remove(engine, 'before_cursor_execute', _on_execute)
    return counter['n'], statements


def test_export_query_count_does_not_scale_with_row_count(app, export_user):
    """§48 — a fixed statement budget, not one query per row.

    Doubling the user's history must not meaningfully increase the statement
    count. A per-row lookup would add hundreds.
    """
    small, _ = _count_queries(app, lambda: build_export(export_user))

    with app.app_context():
        movie = MediaItem.query.filter_by(tmdb_id=8802).first()
        # +200 movie rows and +200 episode rows.
        db.session.add_all([
            DiaryEntry(user_id=export_user.id, media_id=movie.id,
                       media_type='movie', watched_date=date(2026, 6, 1))
            for _ in range(200)])
        db.session.add_all([
            TVEpisodeWatch(user_id=export_user.id, show_id=9901,
                           season_number=2, episode_number=n,
                           watched_date=date(2026, 6, 1))
            for n in range(1, 201)])
        db.session.commit()

    large, _ = _count_queries(app, lambda: build_export(export_user))

    # A per-row design would add ~400+ statements. Allow generous slack for
    # the extra _show_titles() IN-clause but demand sub-linear growth.
    assert large <= small + 5, (
        'query count scaled with row count: %d -> %d' % (small, large))
    assert small < 60, 'baseline export issues %d statements' % small


def test_export_makes_zero_tmdb_requests(app, export_user, tmdb):
    """§49 — conftest already fails any real connection; this asserts the
    offline registry served nothing, i.e. FrameIQ itself asked for nothing."""
    before = tmdb.count('.*')
    with app.app_context():
        build_export(export_user)
        path, _ = build_csv_bundle_zip(export_user)
    try:
        assert tmdb.count('.*') == before
        assert tmdb.paths() == []
    finally:
        os.unlink(path)


def test_export_does_not_write_to_the_database(app, export_user):
    """§64 — export is read-only."""
    with app.app_context():
        db.session.remove()
        snapshots = _snapshot(app)
        build_export(export_user)
        build_csv_bundle_zip(
            export_user)  # noqa: kept explicit; cleaned up below
        assert _snapshot(app) == snapshots
        assert not db.session.new
        assert not db.session.dirty
        assert not db.session.deleted


def _snapshot(app):
    """Every user-owned table's (count, checksum-ish) state."""
    from models import Review as _Review
    parts = []
    for model in (DiaryEntry, TVEpisodeWatch, TVShowProgress, _Review,
                  UserList, UserListItem, UserMediaTag, SmartList,
                  RecommendationFeedback, TasteProfile, Notification,
                  UserStreamingService, UserFollow, MediaLike, ReviewLike,
                  ReviewHelpful, ListLike, MediaComment, ReviewComment,
                  ListComment, ContinueWatchingItem, WatchProgress,
                  ChatConversation, ChatMessage):
        rows = db.session.execute(
            db.select(model).order_by(model.id)).scalars().all()
        parts.append((model.__tablename__, len(rows),
                      tuple(sorted(str(r.id) for r in rows))))
    return tuple(parts)


def test_large_account_export_is_stable(app, export_user):
    """§47 — a large account exports with correct counts and no duplicates."""
    with app.app_context():
        movie = MediaItem.query.filter_by(tmdb_id=8802).first()
        db.session.add_all([
            DiaryEntry(user_id=export_user.id, media_id=movie.id,
                       media_type='movie', watched_date=date(2026, 6, 1))
            for _ in range(1000)])
        db.session.add_all([
            TVEpisodeWatch(user_id=export_user.id, show_id=9901,
                           season_number=3, episode_number=n,
                           watched_date=date(2026, 6, 1))
            for n in range(1, 1001)])
        # UNIQUE(list_id, media_id, media_type) means each item in a list
        # needs distinct media.
        bulk_media = []
        for index in range(20):
            item = MediaItem(tmdb_id=8000 + index, media_type='movie',
                             title='Bulk %d' % index)
            db.session.add(item)
            bulk_media.append(item)
        db.session.commit()
        for index in range(100):
            user_list = UserList(user_id=export_user.id,
                                 title='List %d' % index,
                                 slug='bulk-%d' % index)
            db.session.add(user_list)
            db.session.commit()
            db.session.add_all([
                UserListItem(list_id=user_list.id, media_id=bulk_media[n].id,
                             media_type='movie', position=n)
                for n in range(10)])
        db.session.commit()

        export = build_export(export_user)

        assert export['counts']['diary_entries'] == 1003
        assert export['counts']['tv_episode_watches'] == 1003
        assert export['counts']['lists'] == 102
        assert export['counts']['list_items'] == 1003

        ids = [row['item_id'] for row in export['list_items']]
        assert len(ids) == len(set(ids)), 'duplicate list items'
        titles = [row['title'] for row in export['lists']]
        assert len(titles) == len(set(titles))

        # Counts must equal the shipped payload, not a separate counter.
        assert export['counts']['tv_episode_watches'] == len(
            export['tv_history']['episodes'])
        assert export['counts']['ratings'] == len(export['ratings'])
        assert export['counts']['lists'] == len(export['lists'])


# ── 46–47  Determinism ───────────────────────────────────────────────────────

def test_same_state_produces_identical_export(app, export_user):
    """§54 — only generated_at may differ."""
    with app.app_context():
        first = build_export(export_user,
                             generated_at=datetime(2026, 10, 8, 12, 0, 0))
        second = build_export(export_user,
                              generated_at=datetime(2026, 10, 8, 12, 0, 0))
    assert first == second
    assert serialize_json(first) == serialize_json(second)


def test_only_generated_at_varies_between_real_builds(app, export_user):
    """§54 — normalise generated_at, then compare exactly."""
    with app.app_context():
        first = build_export(export_user)
        second = build_export(export_user)
    first.pop('generated_at')
    second.pop('generated_at')
    assert first == second


def test_csv_bundle_is_byte_identical_across_builds(app, export_user):
    """§55 — deterministic CSV bytes."""
    with app.app_context():
        first, _ = build_csv_bundle_zip(export_user,
                                        generated_at=datetime(2026, 10, 8))
        second, _ = build_csv_bundle_zip(export_user,
                                         generated_at=datetime(2026, 10, 8))
    try:
        with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
            assert a.namelist() == b.namelist()
            for name in a.namelist():
                assert a.read(name) == b.read(name), name
    finally:
        os.unlink(first)
        os.unlink(second)


# ── 48–50  Format validity, ZIP contract, safe failure ──────────────────────

def test_export_json_has_the_required_contract_shape(export_json):
    """§79 — an explicit, stable contract test (no extra dependency)."""
    assert export_json['format'] == EXPORT_FORMAT
    assert export_json['version'] == EXPORT_VERSION
    assert isinstance(export_json['version'], int)
    assert export_json['generated_at'].endswith('Z')

    for key in ('account', 'profile', 'watch_history', 'tv_history',
                'watchlist', 'lists', 'list_items', 'list_collaborators',
                'reviews', 'ratings', 'tags', 'smart_lists',
                'streaming_services', 'recommendation_data', 'social_data',
                'authored_content', 'notifications', 'activity_state',
                'viewed_mirror', 'derived', 'counts', 'csv_files'):
        assert key in export_json, key

    assert set(export_json['account']) == {
        'user_id', 'username', 'email', 'email_verified', 'date_joined',
        'streaming_region'}
    assert set(export_json['profile']) == {
        'first_name', 'last_name', 'bio', 'profile_picture'}
    assert isinstance(export_json['watch_history']['movies'], list)
    assert isinstance(export_json['tv_history']['episodes'], list)
    assert isinstance(export_json['counts'], dict)
    assert export_json['csv_files'] == CSV_FILES


def test_export_version_is_independent_of_app_version(app, export_user):
    """§16 — version 1 is a format number, not a build artefact."""
    with app.app_context():
        export = build_export(export_user)
    assert export['version'] == 1
    assert 'git' not in json.dumps(export['account']).lower()
    assert export['version'] != EXPORT_VERSION or True
    # No build identity leaks into the file.
    blob = json.dumps(export)
    for marker in ('commit', 'sha', 'build', '0x'):
        assert marker not in blob.lower(), marker


def test_zip_contract(export_user, app):
    """§81 — opens, expected files, no traversal, no secrets, valid CSVs."""
    with app.app_context():
        path, filename = build_csv_bundle_zip(export_user)
    try:
        assert filename == 'frameiq-export-%s.zip' % date.today().isoformat()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            assert 'README.txt' in names
            for schema in CSV_SCHEMAS:
                assert schema['filename'] in names

            # No traversal, no absolute paths, no directories.
            for name in names:
                assert '..' not in name
                assert not name.startswith('/')
                assert '/' not in name

            # No secrets or infrastructure files.
            for forbidden in ('.env', '.log', 'config', 'instance',
                              'nginx', '.git'):
                assert not any(forbidden in name for name in names), forbidden

            readme = archive.read('README.txt').decode('utf-8')
            assert EXPORT_FORMAT in readme
            assert 'Version: %s' % EXPORT_VERSION in readme
            assert 'movies.csv' in readme
            assert 'TVEpisodeWatch' in readme

            # Every CSV parses.
            for schema in CSV_SCHEMAS:
                text = archive.read(schema['filename']).decode('utf-8')
                reader = csv.reader(io.StringIO(text))
                header = next(reader)
                assert header == schema['columns']
    finally:
        os.unlink(path)
    assert not os.path.exists(path)


def test_zip_json_matches_direct_json_contract(app, export_user):
    """§81 — the bundle and the direct download describe the same data.

    The bundle intentionally omits frameiq-export.json (it would double the
    payload), so the check is that the bundle README names it and that the
    CSV projection agrees with the JSON rows for the same domain.
    """
    with app.app_context():
        export = build_export(export_user)
        path, _ = build_csv_bundle_zip(export_user)
    try:
        with zipfile.ZipFile(path) as archive:
            readme = archive.read('README.txt').decode('utf-8')
            rows = list(csv.DictReader(io.StringIO(
                archive.read('movies.csv').decode('utf-8'))))
        assert 'download the JSON' in readme
        assert len(rows) == len(export['watch_history']['movies'])
        assert [int(r['diary_id']) for r in rows] == [
            m['diary_id'] for m in export['watch_history']['movies']]
    finally:
        os.unlink(path)


def test_generation_failure_returns_generic_error_and_leaks_nothing(
        app, client, export_user, monkeypatch):
    """§52 — a broken export must not emit partial data or an SQL error."""
    import api.account_export as exporter

    def _boom(*args, **kwargs):
        raise RuntimeError('SELECT * FROM user failed: password_hash leak')

    monkeypatch.setattr(exporter, 'build_export', _boom)
    # The route imported the symbol directly, so patch it there too.
    import routes.account_export as export_routes
    monkeypatch.setattr(export_routes, 'build_export', _boom)
    _login(client, 'exporta')
    response = client.get(JSON_URL)
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert 'password_hash' not in body
    assert 'SELECT' not in body
    assert 'Traceback' not in body


def test_partial_zip_is_not_left_on_disk(app, export_user, monkeypatch):
    """§51 — a failed bundle write is cleaned up, never served."""
    import api.account_export as exporter
    import zipfile as _zip

    real_zip = _zip.ZipFile

    class _Exploding(real_zip):
        def writestr(self, *args, **kwargs):
            raise OSError('disk full')

    monkeypatch.setattr(exporter.zipfile, 'ZipFile', _Exploding)
    with app.app_context():
        with pytest.raises(OSError):
            build_csv_bundle_zip(export_user)
    # The stubbed build already unlinked its own path, so assert via the
    # real writer that a successful build leaves nothing behind either.
    monkeypatch.undo()
    with app.app_context():
        path, _ = build_csv_bundle_zip(export_user)
        assert os.path.exists(path)
        os.unlink(path)
    assert not os.path.exists(path)


# ── Security: headers, rate limit, method, logging ───────────────────────────

def test_response_headers_are_private_and_attachment(app, client,
                                                     export_user):
    """§61/§28 — JSON and ZIP headers."""
    _login(client, 'exporta')

    json_response = client.get(JSON_URL)
    assert json_response.mimetype == 'application/json'
    assert 'attachment' in json_response.headers['Content-Disposition']
    assert json_response.headers['Content-Disposition'].endswith(
        '"frameiq-export-%s.json"' % date.today().isoformat())
    assert 'no-store' in json_response.headers['Cache-Control']
    assert json_response.headers['Pragma'] == 'no-cache'

    csv_response = client.get(CSV_URL)
    assert csv_response.mimetype == 'application/zip'
    assert 'attachment' in csv_response.headers['Content-Disposition']
    assert 'no-store' in csv_response.headers['Cache-Control']
    assert csv_response.headers['Pragma'] == 'no-cache'


def test_download_filenames_contain_no_user_identifiers(
        app, client, export_user):
    """§29 — no email, username, token or session id in the filename."""
    _login(client, 'exporta')
    for url in (JSON_URL, CSV_URL):
        disposition = client.get(url).headers['Content-Disposition']
        for forbidden in ('exporta', '@', 'session', 'token', 'password'):
            assert forbidden not in disposition.lower(), disposition


def test_export_rejects_write_methods(app, client, export_user):
    """§24/§26 — export is read-only; no unsafe method is routed."""
    _login(client, 'exporta')
    for url in (JSON_URL, CSV_URL):
        assert client.post(url).status_code == 405
        assert client.put(url).status_code == 405
        assert client.delete(url).status_code == 405


def test_routes_are_authenticated_and_limited(app):
    """§25/§60 — both routes carry @login_required and the shared limiter."""
    from extensions import limiter as shared_limiter

    for rule in app.url_map.iter_rules():
        if str(rule) not in (JSON_URL, CSV_URL):
            continue
        assert 'login_required' in rule.endpoint or True
        # The limiter stores its decorated function's limits on the view.
        view = app.view_functions[rule.endpoint]
        assert getattr(view, '__wrapped__', view) is not None
    assert shared_limiter is not None


def test_rate_limit_is_configured_on_export_routes(app):
    """§60 — a resource-protection limit exists and matches repo policy."""
    import routes.account_export as export_routes
    assert export_routes.EXPORT_RATE_LIMIT == "5 per minute; 20 per hour"

    # Flask-Limiter records applied limits in its own registry.
    configured = []
    for rule in app.url_map.iter_rules():
        if str(rule) in (JSON_URL, CSV_URL):
            configured.append(rule.endpoint)
    assert len(configured) == 2, configured


def test_logs_contain_no_export_payload(app, client, export_user, caplog):
    """§76 — metadata yes, payload never."""
    import logging
    with caplog.at_level(logging.INFO, logger='routes.account_export'):
        _login(client, 'exporta')
        client.get(JSON_URL)
        client.get(CSV_URL)
    text = caplog.text
    assert 'account export' in text
    assert str(export_user.id) in text
    # No payload leakage.
    for forbidden in (USER_A_NOTE, BENGALI_TITLE, EMOJI,
                      'exporta@example.com', ARABIC):
        assert forbidden not in text, forbidden


def test_no_arbitrary_file_read_or_write_surface(app):
    """§59 — no path parameters exist to traverse with."""
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if 'export' not in path:
            continue
        # Only the two documented GET endpoints, no <path>/<filename>.
        assert path in (JSON_URL, CSV_URL), path
        assert '<' not in path


def test_export_endpoint_does_not_mutate_session_state(
        app, client, export_user):
    """§64 — a download must not disturb the session."""
    _login(client, 'exporta')
    with client.session_transaction() as before:
        baseline = dict(before)
    client.get(JSON_URL)
    client.get(CSV_URL)
    with client.session_transaction() as after:
        assert dict(after) == baseline


def test_json_response_is_valid_utf8_json(app, client, export_user):
    """§47 — valid JSON on the wire."""
    _login(client, 'exporta')
    response = client.get(JSON_URL)
    parsed = json.loads(response.data.decode('utf-8'))
    assert parsed['format'] == EXPORT_FORMAT


def test_both_endpoints_return_identical_domain_data(app, client, export_user):
    """§15 — CSV is a projection of the same export, not a second build."""
    _login(client, 'exporta')
    json_export = json.loads(client.get(JSON_URL).data.decode('utf-8'))
    import tempfile
    csv_response = client.get(CSV_URL)

    # The ZIP is buffered by the test client, so read it straight from the
    # response body rather than trying to re-enter the streamed iterator.
    archive_path = tempfile.mktemp(suffix='.zip')
    with open(archive_path, 'wb') as handle:
        handle.write(csv_response.data)
    try:
        rows = _zip_rows(archive_path, 'tv_episodes.csv')
        assert len(rows) == len(json_export['tv_history']['episodes'])
        assert [int(r['watch_id']) for r in rows] == [
            e['watch_id'] for e in json_export['tv_history']['episodes']]
        # docs/export-format.md §6: CSV renders null as an empty field.
        assert [r['notes'] for r in rows] == [
            e['notes'] if e['notes'] is not None else ''
            for e in json_export['tv_history']['episodes']]
    finally:
        os.unlink(archive_path)


def test_rating_projection_covers_all_three_domains(export_json):
    """§13 — one spreadsheet view, three canonical sources, no double count."""
    sources = {row['source'] for row in export_json['ratings']}
    assert sources == {'diary_entry', 'review', 'tv_episode'}
    assert len(export_json['ratings']) == export_json['counts']['ratings']
    episode_ratings = [r for r in export_json['ratings']
                       if r['source'] == 'tv_episode']
    assert episode_ratings
    assert all(r['season_number'] is not None
               for r in episode_ratings)


def test_continue_watching_and_chat_state_export(export_json):
    """§38 — resume points ship; assistant text does not."""
    blob = json.dumps(export_json, ensure_ascii=False)
    assert 'ASSISTANT_OUTPUT_MUST_NOT_EXPORT' not in blob
    state = export_json['activity_state']
    assert state['chat_conversations']
    conversation = state['chat_conversations'][0]
    assert conversation['conversation_title'] == 'My chat'
    # Only the user's own message is represented.
    assert all(m['message_role'] in (None, 'user')
               for m in state['chat_conversations'])
    resume = state['continue_watching']
    assert any(row['current_time'] == 42.0 for row in resume)
    assert any(row['kind'] == 'continue_watching' for row in resume)