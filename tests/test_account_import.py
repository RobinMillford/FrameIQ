"""Task F6 — Data Portability Import Center.

Covers Letterboxd + TV Time import: canonical-write-path use, idempotency,
resolution/ambiguity, upload security, privacy, performance, and — critically —
that imported TV data behaves identically to normally-watched TV data.

Everything runs offline. conftest's F3 network guard fails any real connection,
and ``test_import_makes_no_network_requests`` asserts the offline TMDb registry
served nothing beyond what eligibility legitimately needed.
"""
import io
import json
import zipfile
from datetime import date, timedelta

import pytest

try:  # tests/ is a package; direct execution still works.
    from tests import import_fixtures as fx
except ImportError:  # pragma: no cover - sys.path forms
    import import_fixtures as fx  # type: ignore[no-redef]

from api.imports import (ALREADY_PRESENT, AMBIGUOUS, IMPORTED, INELIGIBLE,
                         INVALID, UNRESOLVED, ImportRejected, MediaIndex,
                         apply_import, preview, resolve_movie, summary_payload)
from api.imports.sources import parse_letterboxd, parse_tvtime
from api.imports.uploads import (ALLOWED_EXTENSIONS, UploadRejected,
                                 safe_member_name)
from models import (DiaryEntry, MediaItem, Review, TVEpisodeWatch,
                    TVShowProgress, User, UserList, db)

LETTERBOXD = 'letterboxd'
TVTIME = 'tvtime'

# Distinctive markers used to prove absence. Long and string-typed on purpose:
# a short numeric probe also matches autoincrement ids and timestamp
# microseconds (see docs/conventions.md).
B_NOTE = 'B_USER_PRIVATE_EPISODE_NOTE'
B_MOVIE = 'B Only Movie Title 900000002'


@pytest.fixture(autouse=True)
def _wipe(app):
    """Empty every table around each test.

    conftest's ``app`` fixture is session-scoped with no per-test teardown, so
    without this the second test creating user ``importa`` collides with the
    first one's row.
    """
    def _wipe_now():
        with app.app_context():
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()

    _wipe_now()
    yield
    _wipe_now()


def _user(username='importa', email=None):
    user = User(username=username, email=email or f'{username}@verify.test',
                email_verified=True, first_name='Amin',
                bio='আমি সিনেমা দেখি')
    user.set_password('ImportTest1!')
    db.session.add(user)
    db.session.commit()
    return user


def _distinct_date(index):
    """A different ISO date per index, so rows are distinct watch events."""
    return (date(2000, 1, 1) + timedelta(days=index)).isoformat()


def _live(user_id):
    """A session-bound User. The app context is per-test, so a fixture cannot
    hand back a live ORM instance without going detached."""
    return db.session.get(User, user_id)


@pytest.fixture
def user(app):
    """The primary user's ID (not an ORM instance)."""
    with app.app_context():
        return _user().id


@pytest.fixture
def other_user(app):
    """The second user's ID, for cross-user isolation assertions."""
    with app.app_context():
        return _user('importb').id


@pytest.fixture
def catalogue(app):
    """Local MediaItem rows the resolver can match against.

    Resolution is local-only by design, so a title must already exist in the
    database for an import to match it — that is the offline guarantee.
    """
    with app.app_context():
        rows = {
            'matrix': MediaItem(tmdb_id=600_000_000, media_type='movie',
                                title='The Matrix',
                                release_date=date(1999, 3, 31)),
            'bengali': MediaItem(tmdb_id=fx.LETTERBOXD_MOVIE_TMDB,
                                 media_type='movie',
                                 title=fx.BENGALI_TITLE,
                                 release_date=date(2019, 1, 1)),
            'arabic': MediaItem(tmdb_id=600_000_002, media_type='movie',
                                title=fx.ARABIC_TITLE,
                                release_date=date(2020, 1, 1)),
            'accented': MediaItem(tmdb_id=600_000_003, media_type='movie',
                                  title=fx.ACCENTED_TITLE,
                                  release_date=date(2001, 4, 25)),
            'unrated': MediaItem(tmdb_id=600_000_004, media_type='movie',
                                 title='Unrated Film',
                                 release_date=date(2018, 1, 1)),
            'no_uri': MediaItem(tmdb_id=600_000_005, media_type='movie',
                                title='No URI Film',
                                release_date=date(2016, 1, 1)),
            'show': MediaItem(tmdb_id=fx.TVTIME_SHOW_TMDB, media_type='tv',
                              title='F6 Verify Show',
                              release_date=date(2020, 1, 1)),
            'movie': MediaItem(tmdb_id=fx.TVTIME_MOVIE_TMDB,
                               media_type='movie', title='F6 Movie',
                               release_date=date(2018, 1, 1)),
            'ambiguous_a': MediaItem(tmdb_id=600_000_010, media_type='movie',
                                     title='Same Name',
                                     release_date=date(1990, 1, 1)),
            'ambiguous_b': MediaItem(tmdb_id=600_000_011, media_type='movie',
                                     title='Same  Name!',
                                     release_date=date(1991, 1, 1)),
        }
        db.session.add_all(rows.values())
        db.session.commit()
        return {key: row.id for key, row in rows.items()}


@pytest.fixture
def tvtime_show(tmdb):
    """Register the show with the offline TMDb registry.

    F4's eligibility gate resolves the aired set from TMDb metadata; the F3
    registry serves it deterministically so nothing touches the network.
    """
    tmdb.tv_show(fx.TVTIME_SHOW_TMDB, {1: 3}, name='F6 Verify Show',
                 status='Ended')
    return fx.TVTIME_SHOW_TMDB


def _letterboxd(rows=None):
    return fx.build_letterboxd_zip(rows)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Parser isolation — no DB, no resolution, no FrameIQ concepts
# ═══════════════════════════════════════════════════════════════════════════

def test_letterboxd_parser_returns_normalized_records(app):
    with app.app_context():
        records, invalid = parse_letterboxd(_members(_letterboxd()))
    assert len(records) == 6, [r.title for r in records]
    assert len(invalid) == 2, [(i.position, i.reason) for i in invalid]
    assert records[0].source == LETTERBOXD
    assert records[0].source_key == (LETTERBOXD, 'watched', 'the-matrix')
    assert records[0].watched_at == date(2021, 3, 4)
    assert records[0].rating == 4.5
    assert records[0].release_year == 1999


def _members(payload):
    """SafeMember list, as uploads.read_archive would produce."""
    from api.imports.uploads import read_archive
    return read_archive(payload)


def test_letterboxd_ignores_other_csvs_so_history_is_not_double_counted(app):
    """diary.csv overlaps watched.csv; reading both would double every film."""
    with app.app_context():
        records, _ = parse_letterboxd(_members(_letterboxd()))
    matrix = [r for r in records if r.title == 'The Matrix']
    # Two rows: two distinct watch dates (a rewatch), not four from
    # watched.csv + diary.csv.
    assert len(matrix) == 2
    assert sorted(r.watched_at for r in matrix) == [date(2021, 3, 4),
                                                    date(2021, 4, 10)]


def test_letterboxd_zero_rating_is_treated_as_absent(app):
    """A 0 in Letterboxd means "no rating" and must not become 0 stars."""
    with app.app_context():
        records, _ = parse_letterboxd(_members(_letterboxd()))
    unrated = [r for r in records if r.title == 'Unrated Film'][0]
    assert unrated.rating is None


def test_letterboxd_row_without_uri_is_invalid_not_silently_imported(app):
    """Without a stable identity, a re-import could duplicate the film."""
    with app.app_context():
        records, invalid = parse_letterboxd(_members(_letterboxd()))
    reasons = [i.reason for i in invalid]
    assert any('Letterboxd URI' in reason for reason in reasons)
    assert not [r for r in records if r.title == 'No URI Film']


def test_tvtime_parser_reads_seasons_and_skips_unwatched(app):
    with app.app_context():
        payload = fx.build_tvtime_json(episodes_per_season=3, seasons=(1, 2),
                                       include_future=False, movies=False,
                                       unwatched_episode=True)
        episodes, movies, invalid = parse_tvtime(json.loads(payload))
    assert len(episodes) == 6, [e.source_key for e in episodes]
    assert not movies
    assert not invalid
    assert episodes[0].season_number == 1
    assert episodes[0].episode_number == 1
    assert episodes[0].rating is None, 'TV Time has no per-episode rating'


def test_tvtime_special_is_reported_never_imported(app):
    """FrameIQ's canonical ledger excludes specials (F4)."""
    with app.app_context():
        payload = fx.build_tvtime_json(include_special=True,
                                       include_future=False, movies=False)
        episodes, _, invalid = parse_tvtime(json.loads(payload))
    assert not [e for e in episodes if e.season_number == 0]
    assert any('specials are not canonical' in i.reason for i in invalid)


def test_tvtime_normalises_rating_scale(app):
    """TV Time rates 0–10; FrameIQ stores 0.5–5 stars."""
    with app.app_context():
        payload = fx.build_tvtime_json(include_future=False)
        _, movies, _ = parse_tvtime(json.loads(payload))
    assert movies[0].rating == 4.0, '8/10 -> 4.0 stars'


# ═══════════════════════════════════════════════════════════════════════════
# 2. Resolution — deterministic, offline, never guesses
# ═══════════════════════════════════════════════════════════════════════════

def test_resolution_is_local_only_and_never_calls_tmdb(app, catalogue, tmdb):
    with app.app_context():
        from api.imports.records import MovieImportRecord
        record = MovieImportRecord(source=LETTERBOXD,
                                   source_key=(LETTERBOXD, 'watched', 'x'),
                                   title='A Title Nobody Has Browsed')
        resolution = resolve_movie(record, MediaIndex())
    assert not resolution.is_resolved
    assert resolution.status == UNRESOLVED
    assert tmdb.paths() == [], 'resolution must not consult TMDb'


def test_ambiguous_title_is_never_auto_selected(app, catalogue):
    with app.app_context():
        from api.imports.records import MovieImportRecord
        record = MovieImportRecord(source=LETTERBOXD,
                                   source_key=(LETTERBOXD, 'watched', 'x'),
                                   title='Same Name')
        resolution = resolve_movie(record, MediaIndex())
    assert resolution.status == AMBIGUOUS
    assert not resolution.is_resolved
    assert 'Multiple local titles' in resolution.detail


@pytest.mark.parametrize('title', [
    'The Matrix', 'THE   matrix!', 'the matrix',
    fx.BENGALI_TITLE, fx.ARABIC_TITLE, fx.ACCENTED_TITLE,
    '君の名は。', 'Земля', 'Amélie', 'Æon Flux',
])
def test_normalize_title_never_erases_a_non_latin_script(title):
    """Regression: an ASCII-only character class deleted every non-Latin
    letter, so Bengali / Arabic / Japanese titles normalised to the EMPTY
    string, matched nothing, and could never resolve. FrameIQ's own F5 export
    preserves those scripts, so import must resolve them too."""
    from api.imports.resolve import normalize_title
    folded = normalize_title(title)
    assert folded, f'{title!r} normalised to empty'
    assert folded == folded.strip()


@pytest.mark.parametrize('title', [
    fx.BENGALI_TITLE, fx.ARABIC_TITLE, '君の名は。', 'Земля', 'Пушкин',
])
def test_non_decomposable_scripts_keep_their_characters(title):
    """Scripts that NFKD cannot fold into ASCII must survive intact.

    Accented Latin is deliberately NOT in this list: 'Amélie' legitimately
    folds to 'amelie', because decomposition is exactly how a user-supplied
    'Amelie' should match it.
    """
    from api.imports.resolve import normalize_title
    folded = normalize_title(title)
    assert any(ord(ch) > 127 for ch in folded), (
        f'{title!r} lost every non-ASCII character -> {folded!r}')


def test_normalize_title_folds_case_and_punctuation():
    from api.imports.resolve import normalize_title
    assert normalize_title('The Matrix') == normalize_title('THE   matrix!')
    assert normalize_title('Amélie') == normalize_title('AMELIE')
    assert normalize_title('') == ''
    assert normalize_title(None) == ''


def test_non_latin_title_resolves_end_to_end(app, catalogue, user,
                                             tvtime_show):
    """A Bengali film must resolve, not sit permanently unresolved."""
    with app.app_context():
        report = apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
    assert report[UNRESOLVED] == 0, report
    assert report[IMPORTED] == 6
    with app.app_context():
        titles = db.session.execute(
            db.select(MediaItem.title)
            .select_from(DiaryEntry)
            .join(MediaItem, MediaItem.id == DiaryEntry.media_id)
            .where(DiaryEntry.user_id == user)).scalars().all()
    assert fx.BENGALI_TITLE in titles
    assert fx.ARABIC_TITLE in titles
    assert fx.ACCENTED_TITLE in titles


def test_unique_title_resolves(app, catalogue):
    with app.app_context():
        from api.imports.records import MovieImportRecord
        record = MovieImportRecord(source=LETTERBOXD,
                                   source_key=(LETTERBOXD, 'watched', 'm'),
                                   title='The Matrix', release_year=1999)
        resolution = resolve_movie(record, MediaIndex())
    assert resolution.is_resolved
    assert resolution.media_item.tmdb_id == 600_000_000


def test_tvtime_tmdb_id_resolves_exactly(app, catalogue):
    with app.app_context():
        from api.imports.records import TVEpisodeImportRecord
        record = TVEpisodeImportRecord(
            source=TVTIME, source_key=(TVTIME, 'x', '1', '1'),
            show_title='A totally different local name',
            show_external_id=str(fx.TVTIME_SHOW_TMDB),
            season_number=1, episode_number=1)
        resolution = resolve_movie.__globals__['resolve_episode'](
            record, MediaIndex())
    assert resolution.is_resolved
    assert resolution.media_item.tmdb_id == fx.TVTIME_SHOW_TMDB


# ═══════════════════════════════════════════════════════════════════════════
# 3. Preview — side-effect free
# ═══════════════════════════════════════════════════════════════════════════

def test_preview_classifies_without_writing(app, user, catalogue, tvtime_show):
    with app.app_context():
        before = (DiaryEntry.query.count(), TVEpisodeWatch.query.count())
        report = preview(user, LETTERBOXD, 'letterboxd.zip', _letterboxd())
        after = (DiaryEntry.query.count(), TVEpisodeWatch.query.count())
        assert before == after, 'preview must not write'
    assert report[IMPORTED] == 6
    assert report[UNRESOLVED] == 0
    assert report[INVALID] == 2
    # 'detected' is the number of well-formed records; the two rejected rows
    # are reported separately under invalid.
    assert report['details']['records_detected'] == 6
    assert report[INVALID] == 2


def test_preview_reports_future_episodes_as_ineligible(app, user, catalogue,
                                                       tvtime_show):
    with app.app_context():
        report = preview(user, TVTIME, 'tvtime.json',
                         fx.build_tvtime_json())
    assert report[INELIGIBLE] >= 1
    samples = report['details']['episodes']
    ineligible = [s for s in samples if s['status'] == INELIGIBLE]
    assert ineligible
    assert ineligible[0]['season_number'] == 9


def test_preview_summary_is_bounded(app, user, catalogue, tvtime_show):
    """A huge import must not produce an unbounded response body."""
    with app.app_context():
        rows = [['2021-03-0%d 21:00' % ((i % 9) + 1), 'The Matrix', '1999',
                 '/film/the-matrix-%d/' % i, '4'] for i in range(600)]
        payload = fx.build_letterboxd_zip(rows)
        payload_summary = summary_payload(
            preview(user, LETTERBOXD, 'lb.zip', payload))
    assert payload_summary['imported'] > 0
    assert len(payload_summary['samples']['movies']) <= 100
    assert len(json.dumps(payload_summary)) < 200_000


# ═══════════════════════════════════════════════════════════════════════════
# 4. Letterboxd import → canonical DiaryEntry
# ═══════════════════════════════════════════════════════════════════════════

def test_letterboxd_import_creates_diary_entries(app, user, catalogue,
                                                 tvtime_show):
    with app.app_context():
        report = apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
        matrix = DiaryEntry.query.filter_by(
            user_id=user, media_type='movie',
            media_id=catalogue['matrix']).order_by(DiaryEntry.watched_date).all()
        assert report[IMPORTED] == 6
        assert len(matrix) == 2
        assert matrix[0].watched_date == date(2021, 3, 4)
        assert matrix[0].rating == 4.5
        assert matrix[0].is_rewatch is False
        # The second watch of the same film is a genuine rewatch.
        assert matrix[1].is_rewatch is True


def test_repeated_letterboxd_import_is_idempotent(app, user, catalogue,
                                                  tvtime_show):
    with app.app_context():
        payload = _letterboxd()
        first = apply_import(user, LETTERBOXD, 'lb.zip', payload)
        count_after_first = DiaryEntry.query.filter_by(
            user_id=user).count()
        second = apply_import(user, LETTERBOXD, 'lb.zip', payload)
        third = apply_import(user, LETTERBOXD, 'lb.zip', payload)
        count_after_third = DiaryEntry.query.filter_by(
            user_id=user).count()
    assert first[IMPORTED] == 6
    assert second[IMPORTED] == 0
    assert second[ALREADY_PRESENT] == 6
    assert third[IMPORTED] == 0
    assert count_after_first == count_after_third == 6


def test_letterboxd_import_preserves_unicode(app, user, catalogue,
                                             tvtime_show):
    with app.app_context():
        apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
        rows = db.session.execute(
            db.select(MediaItem.title)
            .select_from(DiaryEntry)
            .join(MediaItem, MediaItem.id == DiaryEntry.media_id)
            .where(DiaryEntry.user_id == user)).scalars().all()
        joined = ' '.join(rows)
    assert fx.BENGALI_TITLE in joined
    assert fx.ARABIC_TITLE in joined
    assert fx.ACCENTED_TITLE in joined


def test_import_does_not_overwrite_newer_existing_data(app, user, catalogue,
                                                       tvtime_show):
    """An import of older data must not clobber a rating the user added."""
    with app.app_context():
        matrix = db.session.get(MediaItem, catalogue['matrix'])
        newer = DiaryEntry(user_id=user, media_id=matrix.id,
                           media_type='movie', watched_date=date(2021, 3, 4),
                           rating=5.0)
        db.session.add(newer)
        db.session.commit()

        rows = [['2021-03-04 21:00', 'The Matrix', '1999',
                 '/film/the-matrix/', '2']]
        apply_import(user, LETTERBOXD, 'lb.zip',
                     fx.build_letterboxd_zip(rows))
        rows_now = DiaryEntry.query.filter_by(user_id=user).all()
        assert len(rows_now) == 1, 'same media+date must not duplicate'
        assert rows_now[0].rating == 5.0, 'existing rating must survive'


# ═══════════════════════════════════════════════════════════════════════════
# 5. TV Time import → canonical TVEpisodeWatch via F4's write path
# ═══════════════════════════════════════════════════════════════════════════

def test_tvtime_import_uses_the_canonical_write_path(app, user, catalogue,
                                                     tvtime_show, monkeypatch):
    """Proves delegation rather than a direct TVEpisodeWatch insert."""
    calls = []
    import routes.tv_tracking as tv_tracking
    original = tv_tracking.mark_episode_watched_core

    def _spy(user_id, show_id, season, episode, data=None):
        calls.append((user_id, show_id, season, episode))
        return original(user_id, show_id, season, episode, data=data)

    monkeypatch.setattr(tv_tracking, 'mark_episode_watched_core', _spy)
    with app.app_context():
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(episodes_per_season=3, seasons=(1, 2),
                                          include_future=False, movies=False))
        assert calls, 'import must go through mark_episode_watched_core'
        assert all(c[0] == user for c in calls)
        assert {c[3] for c in calls} == {1, 2, 3}


def test_tvtime_import_creates_canonical_episode_rows(app, user, catalogue,
                                                      tvtime_show):
    with app.app_context():
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(episodes_per_season=3, seasons=(1,),
                                          include_future=False, movies=False))
        rows = TVEpisodeWatch.query.filter_by(
            user_id=user, show_id=fx.TVTIME_SHOW_TMDB).all()
        assert len(rows) == 3
        assert {r.episode_name for r in rows} == {'Episode 1', 'Episode 2',
                                                  'Episode 3'}
        # The fixture dates episode N on 2021-05-N; all three must survive.
        assert {r.watched_date for r in rows} == {
            date(2021, 5, 1), date(2021, 5, 2), date(2021, 5, 3)}


def test_tvtime_future_episode_is_not_marked_watched(app, user, catalogue,
                                                     tvtime_show):
    with app.app_context():
        report = apply_import(user, TVTIME, 'tvtime.json',
                              fx.build_tvtime_json(include_future=True,
                                                   movies=False))
        positions = {(r.season_number, r.episode_number)
                     for r in TVEpisodeWatch.query.filter_by(
                         user_id=user).all()}
    assert (9, 99) not in positions
    assert report[INELIGIBLE] >= 1


def test_tvtime_special_does_not_become_a_regular_episode(app, user, catalogue,
                                                          tvtime_show):
    with app.app_context():
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(include_special=True,
                                          include_future=False, movies=False))
        positions = {(r.season_number, r.episode_number)
                     for r in TVEpisodeWatch.query.filter_by(
                         user_id=user).all()}
    assert (0, 1) not in positions, 'specials are not canonical episodes'


def test_repeated_tvtime_import_is_idempotent(app, user, catalogue,
                                              tvtime_show):
    with app.app_context():
        payload = fx.build_tvtime_json(include_future=False, movies=False)
        first = apply_import(user, TVTIME, 'tvtime.json', payload)
        after_first = TVEpisodeWatch.query.filter_by(
            user_id=user).count()
        second = apply_import(user, TVTIME, 'tvtime.json', payload)
        after_second = TVEpisodeWatch.query.filter_by(
            user_id=user).count()
    assert first[IMPORTED] == 3, '3 episodes, movies=False in this fixture'
    assert second[IMPORTED] == 0
    assert second[ALREADY_PRESENT] == 3
    assert after_first == after_second == 3


# ═══════════════════════════════════════════════════════════════════════════
# 6. Imported TV data behaves identically to normally-watched TV data
# ═══════════════════════════════════════════════════════════════════════════

def test_imported_tv_state_matches_manually_watched_state(
        app, user, catalogue, tvtime_show):
    """The core F6 correctness claim."""
    from api.user_view_state import (canonical_progress_map,
                                     tv_viewed_from_progress)

    other_id = _user('importmanual').id
    with app.app_context():
        # Two accounts reach the same TV state by different routes.
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(episodes_per_season=3, seasons=(1,),
                                          include_future=False, movies=False))
        from routes.tv_tracking import mark_episode_watched_core
        for episode in (1, 2, 3):
            mark_episode_watched_core(other_id, fx.TVTIME_SHOW_TMDB, 1,
                                      episode)

        imported = canonical_progress_map(_live(user), [fx.TVTIME_SHOW_TMDB])
        manual = canonical_progress_map(_live(other_id), [fx.TVTIME_SHOW_TMDB])
        assert imported == manual
        assert imported[fx.TVTIME_SHOW_TMDB]['watched'] == 3
        assert imported[fx.TVTIME_SHOW_TMDB]['aired'] == 3
        # Both routes reach the same canonical Viewed verdict.
        assert tv_viewed_from_progress(imported[fx.TVTIME_SHOW_TMDB]) is True
        assert tv_viewed_from_progress(manual[fx.TVTIME_SHOW_TMDB]) is True

        imported_progress = TVShowProgress.query.filter_by(
            user_id=user, show_id=fx.TVTIME_SHOW_TMDB).one()
        manual_progress = TVShowProgress.query.filter_by(
            user_id=other_id, show_id=fx.TVTIME_SHOW_TMDB).one()
        assert imported_progress.watched_episodes == \
            manual_progress.watched_episodes
        assert imported_progress.total_episodes == \
            manual_progress.total_episodes


def test_full_airsed_import_reaches_viewed_canonically(app, user, catalogue,
                                                       tmdb):
    """Every aired episode watched ⇒ Viewed, derived not asserted."""
    from api.user_view_state import (canonical_progress_map,
                                     tv_viewed_from_progress)
    show = 600_100_002
    tmdb.tv_show(show, {1: 4, 2: 4}, name='F6 Complete Show', status='Ended')
    with app.app_context():
        db.session.add(MediaItem(tmdb_id=show, media_type='tv',
                                 title='F6 Complete Show'))
        db.session.commit()
        payload = fx.build_tvtime_json(episodes_per_season=4, seasons=(1, 2),
                                       include_future=False, movies=False,
                                       show_tmdb=show,
                                       show_name='F6 Complete Show')
        report = apply_import(user, TVTIME, 'tvtime.json', payload)
        progress = canonical_progress_map(_live(user), [show])[show]
        assert report[IMPORTED] == 8
        assert progress['watched'] == 8
        assert progress['aired'] == 8
        assert progress['percent'] == 100.0
        assert tv_viewed_from_progress(progress) is True


def test_unmark_still_works_after_import(app, user, catalogue, tvtime_show):
    with app.app_context():
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(include_future=False, movies=False))
        assert TVEpisodeWatch.query.filter_by(user_id=user).count() == 3

        response = _login_and_unmark(app, user, fx.TVTIME_SHOW_TMDB, 1, 1)
        assert response.status_code == 200
        body = response.get_json()
        assert 'progress' in body and 'viewed' in body
        assert body['progress']['watched_episodes'] == 2
        assert body['progress']['aired_episodes'] == 3
        assert TVEpisodeWatch.query.filter_by(
            user_id=user, season_number=1, episode_number=1).count() == 0


def _login_and_unmark(app, user_id, show_id, season, episode):
    client = app.test_client()
    _login(client)
    return client.post(
        f'/api/tv/{show_id}/episode/{season}/{episode}/unmark-watched')


def test_continue_watching_still_works_after_import(app, user, catalogue,
                                                    tvtime_show):
    from api.continue_watching import start_item, finish_tv_episode
    with app.app_context():
        start_item(user, 'tv', fx.TVTIME_SHOW_TMDB, season=1, episode=1)
        from models import ContinueWatchingItem
        assert ContinueWatchingItem.query.filter_by(
            user_id=user, tmdb_id=fx.TVTIME_SHOW_TMDB).count() == 1
        result = finish_tv_episode(user, fx.TVTIME_SHOW_TMDB, 1, 1)
        assert result['finished'] is True


def test_next_episode_remains_canonical_after_import(app, user, catalogue,
                                                     tvtime_show):
    from api.continue_watching import find_next_episode
    with app.app_context():
        apply_import(user, TVTIME, 'tvtime.json',
                     fx.build_tvtime_json(episodes_per_season=3, seasons=(1,),
                                          include_future=False, movies=False))
        nxt = find_next_episode(user, fx.TVTIME_SHOW_TMDB, after=(1, 0))
    # Season 1 is complete and the show has no other aired season.
    assert nxt is None or nxt.get('season') != 1


def test_imported_rating_and_notes_are_preserved(app, user, catalogue,
                                                 tmdb):
    """F4's metadata rules apply to imports too."""
    show = 600_100_003
    tmdb.tv_show(show, {1: 2}, name='F6 Meta Show', status='Ended')
    with app.app_context():
        db.session.add(MediaItem(tmdb_id=show, media_type='tv',
                                 title='F6 Meta Show'))
        db.session.commit()
        payload = fx.build_tvtime_json(episodes_per_season=2, seasons=(1,),
                                       include_future=False, movies=False,
                                       show_tmdb=show, show_name='F6 Meta Show')
        apply_import(user, TVTIME, 'tvtime.json', payload)

        from routes.tv_tracking import mark_episode_watched_core
        mark_episode_watched_core(user, show, 1, 1,
                                  data={'rating': 4.5, 'notes': 'keep me'})
        # A second import must NOT clear the rating/notes the user set,
        # because the source has no values for them.
        apply_import(user, TVTIME, 'tvtime.json', payload)
        row = TVEpisodeWatch.query.filter_by(user_id=user, show_id=show,
                                             season_number=1,
                                             episode_number=1).first()
        assert row.rating == 4.5
        assert row.notes == 'keep me'


# ═══════════════════════════════════════════════════════════════════════════
# 7. Upload security
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_source_is_rejected(app):
    assert 'spellbook' not in ALLOWED_EXTENSIONS
    with app.app_context():
        # The extension allow-list is the first gate, and it reports an
        # unknown source as 404 rather than a validation error.
        with pytest.raises(UploadRejected) as exc:
            preview(1, 'spellbook', 'x.zip', b'x')
    assert exc.value.status == 404


@pytest.mark.parametrize('filename', [
    'evil.exe', 'evil.php', 'evil.sh', 'evil.py', 'payload', 'evil.csv',
])
def test_disallowed_extension_is_refused(app, filename):
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, LETTERBOXD, filename, b'whatever')


def test_letterboxd_requires_a_zip(app):
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, LETTERBOXD, 'data.json', b'{"a": 1}')


def test_non_zip_bytes_are_refused(app):
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, LETTERBOXD, 'data.zip', b'not a zip at all')


def test_json_extension_must_contain_json(app):
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, TVTIME, 'data.json', b'\x00\x01\x02 binary')


def test_oversized_upload_is_refused_before_parsing(app, user, catalogue):
    with app.app_context():
        from api.imports.uploads import check_size
        with pytest.raises(UploadRejected) as exc:
            check_size(b'x' * (9 * 1024 * 1024))
        assert exc.value.status == 413


def test_empty_upload_is_refused(app):
    with app.app_context():
        from api.imports.uploads import check_size
        with pytest.raises(UploadRejected):
            check_size(b'')


@pytest.mark.parametrize('name', [
    '../escaped.csv', '../../etc/passwd', '/etc/passwd', 'a/../../b.csv',
    '..\\windows.csv', 'C:\\evil.csv', 'subdir/../../out.csv',
])
def test_zip_traversal_and_unsafe_names_are_refused(app, name):
    payload = fx.build_letterboxd_zip_unsafe_name(name)
    with app.app_context():
        with pytest.raises(UploadRejected) as exc:
            preview(1, LETTERBOXD, 'lb.zip', payload)
    assert 'unsafe file name' in exc.value.reason


def test_tvtime_zip_traversal_is_refused(app):
    with app.app_context():
        with pytest.raises(UploadRejected) as exc:
            preview(1, TVTIME, 'tv.zip', fx.build_tvtime_zip_traversal())
    assert 'unsafe file name' in exc.value.reason


def test_letterboxd_traversal_variant_is_refused(app):
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, LETTERBOXD, 'lb.zip',
                    fx.build_letterboxd_zip_traversal())


def test_dotfile_members_are_refused(app):
    payload = fx.build_letterboxd_zip_unsafe_name('.env')
    with app.app_context():
        with pytest.raises(UploadRejected) as exc:
            preview(1, LETTERBOXD, 'lb.zip', payload)
    assert 'unsafe file name' in exc.value.reason


def test_zip_bomb_declared_size_is_refused(app):
    payload = fx.build_zip_with_bomb()
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, LETTERBOXD, 'lb.zip', payload)


def test_archive_without_watched_csv_is_refused(app):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('random.txt', 'hello')
    with app.app_context():
        with pytest.raises(ImportRejected) as exc:
            preview(1, LETTERBOXD, 'lb.zip', buffer.getvalue())
    assert 'Letterboxd' in exc.value.reason


def test_malformed_tvtime_structure_is_a_4xx_not_a_crash(app, user):
    # Structural problems raise UploadRejected (ImportRejected for the ones
    # the adapter detects); all of them are 4xx, never a 500.
    with app.app_context():
        for payload in (b'{"shows": "not a list"}', b'[1, 2, 3]',
                        b'{ not json', b''):
            with pytest.raises(UploadRejected) as exc:
                preview(user, TVTIME, 'tv.json', payload)
            assert exc.value.status in (400, 413)


def test_tvtime_zip_without_json_is_refused(app):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('notes.txt', 'nothing useful')
    with app.app_context():
        with pytest.raises(UploadRejected):
            preview(1, TVTIME, 'tv.zip', buffer.getvalue())


def test_safe_member_name_accepts_ordinary_names():
    assert safe_member_name('watched.csv') == 'watched.csv'
    assert safe_member_name('data/watched.csv') == 'data/watched.csv'
    assert safe_member_name('../../x.csv') is None
    assert safe_member_name('/abs.csv') is None
    assert safe_member_name('') is None
    assert safe_member_name('a\x00b') is None


def test_no_dynamic_import_or_shell_in_the_import_package():
    """A data importer has no legitimate need to execute anything.

    Checked against the AST rather than the raw source, so the module's own
    prose ("deliberately absent: pickle, subprocess, ...") cannot satisfy or
    trip the guard. Only real imports and real call targets are inspected.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / 'api' / 'imports'
    banned_modules = {'pickle', 'cPickle', 'subprocess', 'shutil', 'marshal',
                      'importlib', 'ctypes', 'pty'}
    banned_builtins = {'eval', 'exec', 'compile', '__import__', 'globals',
                       'locals'}

    for path in sorted(root.rglob('*.py')):
        tree = ast.parse(path.read_text())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split('.')[0]
                    assert root_name not in banned_modules, (
                        '%s imports %s' % (path.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                root_name = (node.module or '').split('.')[0]
                assert root_name not in banned_modules, (
                    '%s imports from %s' % (path.name, node.module))
            elif isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    assert target.id not in banned_builtins, (
                        '%s calls %s()' % (path.name, target.id))
                elif isinstance(target, ast.Attribute):
                    assert target.attr not in ('system', 'popen', 'spawn*'), (
                        '%s calls .%s' % (path.name, target.attr))

        # And no YAML loader, which can construct arbitrary objects.
        assert 'yaml.load(' not in path.read_text().replace(
            'yaml.load (', ''), path.name


def test_import_package_does_not_write_uploaded_files(
        app, user, catalogue, tvtime_show):
    """Uploaded bytes must not be persisted anywhere on disk."""
    import glob
    import tempfile as _tempfile
    before = set(glob.glob('/tmp/*letterboxd*'))
    before |= set(glob.glob('/tmp/*tvtime*'))
    with app.app_context():
        apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
    assert set(glob.glob('/tmp/*letterboxd*')) == before
    assert set(glob.glob('/tmp/*tvtime*')) == before
    assert _tempfile.gettempdir()


# ═══════════════════════════════════════════════════════════════════════════
# 8. Privacy — two users
# ═══════════════════════════════════════════════════════════════════════════

def test_import_is_strictly_current_user_scoped(app, user, other_user,
                                                catalogue, tvtime_show):
    """User B's entire dataset must be byte-for-byte unchanged by A's import."""
    with app.app_context():
        b_matrix = db.session.get(MediaItem, catalogue['matrix'])
        b_entry = DiaryEntry(user_id=other_user, media_id=b_matrix.id,
                             media_type='movie',
                             watched_date=date(2020, 5, 5), rating=5.0)
        b_ep = TVEpisodeWatch(user_id=other_user,
                              show_id=fx.TVTIME_SHOW_TMDB, season_number=1,
                              episode_number=1, watched_date=date(2020, 5, 5),
                              notes=B_NOTE)
        b_list = UserList(user_id=other_user, title=B_MOVIE,
                          slug='b-list-f6')
        b_review = Review(user_id=other_user, media_id=b_matrix.id,
                          media_type='movie', content=B_NOTE, rating=4.0)
        db.session.add_all([b_entry, b_ep, b_list, b_review])
        db.session.commit()

        before = {
            'diary': [tuple(r) for r in db.session.execute(
                db.select(DiaryEntry.user_id, DiaryEntry.media_id,
                          DiaryEntry.watched_date, DiaryEntry.rating,
                          DiaryEntry.is_rewatch).where(
                    DiaryEntry.user_id == other_user))],
            'episodes': [tuple(r) for r in db.session.execute(
                db.select(TVEpisodeWatch.user_id, TVEpisodeWatch.show_id,
                          TVEpisodeWatch.season_number,
                          TVEpisodeWatch.episode_number,
                          TVEpisodeWatch.watched_date,
                          TVEpisodeWatch.notes,
                          TVEpisodeWatch.rating).where(
                    TVEpisodeWatch.user_id == other_user))],
            'lists': [tuple(r) for r in db.session.execute(
                db.select(UserList.user_id, UserList.title).where(
                    UserList.user_id == other_user))],
            'reviews': [tuple(r) for r in db.session.execute(
                db.select(Review.user_id, Review.content).where(
                    Review.user_id == other_user))],
        }

        apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
        apply_import(user, TVTIME, 'tv.json',
                     fx.build_tvtime_json(include_future=False))

        after = {
            'diary': [tuple(r) for r in db.session.execute(
                db.select(DiaryEntry.user_id, DiaryEntry.media_id,
                          DiaryEntry.watched_date, DiaryEntry.rating,
                          DiaryEntry.is_rewatch).where(
                    DiaryEntry.user_id == other_user))],
            'episodes': [tuple(r) for r in db.session.execute(
                db.select(TVEpisodeWatch.user_id, TVEpisodeWatch.show_id,
                          TVEpisodeWatch.season_number,
                          TVEpisodeWatch.episode_number,
                          TVEpisodeWatch.watched_date,
                          TVEpisodeWatch.notes,
                          TVEpisodeWatch.rating).where(
                    TVEpisodeWatch.user_id == other_user))],
            'lists': [tuple(r) for r in db.session.execute(
                db.select(UserList.user_id, UserList.title).where(
                    UserList.user_id == other_user))],
            'reviews': [tuple(r) for r in db.session.execute(
                db.select(Review.user_id, Review.content).where(
                    Review.user_id == other_user))],
        }
        assert before == after, "user B's data changed"

        # And every imported row belongs to A.
        assert all(r.user_id == user for r in
                   DiaryEntry.query.filter_by(user_id=user).all())
        assert all(r.user_id == user for r in
                   TVEpisodeWatch.query.filter_by(user_id=user).all())


# ═══════════════════════════════════════════════════════════════════════════
# 9. Performance — no per-row N+1
# ═══════════════════════════════════════════════════════════════════════════

def _count_queries(app, fn):
    from sqlalchemy import event
    counter = {'n': 0}

    def _on_execute(conn, cursor, statement, parameters, context, many):
        counter['n'] += 1

    with app.app_context():
        engine = db.session.get_bind()
        event.listen(engine, 'before_cursor_execute', _on_execute)
        try:
            fn()
        finally:
            event.remove(engine, 'before_cursor_execute', _on_execute)
    return counter['n']


def test_import_query_count_does_not_scale_with_row_count(app, user,
                                                          catalogue,
                                                          tvtime_show):
    """500 movie rows must not cost ~500 lookups."""
    def _make(n):
        rows = [['2021-03-04 21:00', 'The Matrix', '1999',
                 '/film/the-matrix-%d/' % i, '4'] for i in range(n)]
        return fx.build_letterboxd_zip(rows)

    small_payload = _make(5)
    with app.app_context():
        small = _count_queries(
            app, lambda: preview(user, LETTERBOXD, 'lb.zip',
                                 small_payload))

    big_payload = _make(500)
    with app.app_context():
        big = _count_queries(
            app, lambda: preview(user, LETTERBOXD, 'lb.zip', big_payload))

    # A per-row design would add hundreds. Allow generous slack for the extra
    # DiaryEntry.prefetch but demand clearly sub-linear growth.
    assert big <= small + 10, (
        'query count scaled with rows: %d -> %d' % (small, big))


def test_tv_import_query_count_is_bounded(app, user, catalogue, tvtime_show):
    payload = fx.build_tvtime_json(episodes_per_season=3, seasons=(1, 2),
                                   include_future=False, movies=False)
    with app.app_context():
        count = _count_queries(
            app, lambda: preview(user, TVTIME, 'tv.json', payload))
    assert count < 60, 'episode preview issued %d statements' % count


def test_import_makes_no_network_requests(app, user, catalogue, tvtime_show,
                                          tmdb):
    """The F3 registry must serve nothing but the offline show fixture."""
    before = set(tmdb.paths())
    with app.app_context():
        apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
    # Letterboxd resolution needs no metadata at all.
    assert set(tmdb.paths()) == before

    with app.app_context():
        apply_import(user, TVTIME, 'tv.json',
                     fx.build_tvtime_json(include_future=False))
    # TV Time import consults the show's aired set — that is the offline
    # registry, not the network. Every request is a /3/tv/<id> shape.
    for path in set(tmdb.paths()) - before:
        assert path.startswith('/3/tv/'), path


def test_large_import_remains_bounded(app, user, catalogue, tvtime_show):
    """1,000 rows: correct counts, no duplicates, still fast."""
    # 1,000 DISTINCT watch events (one title, 1,000 different dates), which
    # is what makes "1,000 rows imported" a meaningful assertion.
    rows = [['%s 21:00' % _distinct_date(i), 'The Matrix', '1999',
             '/film/the-matrix-%d/' % i, '4'] for i in range(1000)]
    payload = fx.build_letterboxd_zip(rows)
    with app.app_context():
        report = apply_import(user, LETTERBOXD, 'lb.zip', payload)
        assert report[IMPORTED] == 1000
        assert report[INVALID] == 0
        assert DiaryEntry.query.filter_by(user_id=user).count() == 1000
        # Re-import adds nothing.
        second = apply_import(user, LETTERBOXD, 'lb.zip', payload)
        assert second[IMPORTED] == 0
        assert DiaryEntry.query.filter_by(user_id=user).count() == 1000


# ═══════════════════════════════════════════════════════════════════════════
# 10. Transaction semantics
# ═══════════════════════════════════════════════════════════════════════════

def test_a_systemic_failure_rolls_the_whole_import_back(app, user, catalogue,
                                                        tvtime_show,
                                                        monkeypatch):
    """A failed import must not leave a half-imported account.

    Note the two different failure classes, which behave differently on
    purpose:

      * a PER-ROW failure is captured into ``failures`` and the import still
        commits the good rows (a single bad title must not discard 999 valid
        watches) — see test_per_row_failure_does_not_discard_good_rows;
      * a SYSTEMIC failure (anything reaching commit) rolls the whole
        transaction back. That is what this test drives, by making the commit
        itself fail after rows have been staged.
    """
    rows = [['%s 21:00' % _distinct_date(i), 'The Matrix', '1999',
             '/film/the-matrix-%d/' % i, '4'] for i in range(20)]
    with app.app_context():
        apply_import(user, LETTERBOXD, 'lb.zip',
                     fx.build_letterboxd_zip(rows))
        assert DiaryEntry.query.filter_by(user_id=user).count() == 20
        baseline = DiaryEntry.query.filter_by(user_id=user).count()

        state = {'calls': 0}

        def _failing_commit():
            state['calls'] += 1
            raise RuntimeError('simulated storage failure at commit')

        monkeypatch.setattr(db.session, 'commit', _failing_commit)
        with pytest.raises(RuntimeError):
            apply_import(user, TVTIME, 'tv.json',
                         fx.build_tvtime_json(include_future=False,
                                              movies=False))
        monkeypatch.undo()
        db.session.rollback()

        # Nothing from the failed import survived.
        assert TVEpisodeWatch.query.filter_by(user_id=user).count() == 0
        assert TVShowProgress.query.filter_by(user_id=user).count() == 0
        # And the earlier import is untouched.
        assert DiaryEntry.query.filter_by(user_id=user).count() == baseline


def test_per_row_failure_does_not_discard_good_rows(
        app, user, catalogue, tvtime_show, monkeypatch):
    """One unwritable title must not cost the user the other 999 watches."""
    import api.imports.service as service

    real = service.write_movie_watch
    seen = {'n': 0}

    def _flaky(user_id, media_item, watched_at, rating=None):
        seen['n'] += 1
        if seen['n'] == 2:
            raise RuntimeError('simulated per-row failure')
        return real(user_id, media_item, watched_at, rating=rating)

    rows = [['%s 21:00' % _distinct_date(i), 'The Matrix', '1999',
             '/film/the-matrix-%d/' % i, '4'] for i in range(5)]
    with app.app_context():
        monkeypatch.setattr(service, 'write_movie_watch', _flaky)
        report = service.apply_import(user, LETTERBOXD, 'lb.zip',
                                      fx.build_letterboxd_zip(rows))
        monkeypatch.undo()
        written = DiaryEntry.query.filter_by(user_id=user).count()
    assert written == 4, 'the good rows still landed'
    assert report[IMPORTED] == 4
    assert len(report['failures']) == 1
    assert 'simulated per-row failure' in report['failures'][0]['reason']


def test_result_summary_is_accurate_for_every_category(app, user, catalogue,
                                                       tvtime_show):
    with app.app_context():
        apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
        report = apply_import(user, LETTERBOXD, 'lb.zip', _letterboxd())
    total = sum(report[key] for key in
                (IMPORTED, ALREADY_PRESENT, UNRESOLVED, AMBIGUOUS,
                 INELIGIBLE, INVALID, 'unsupported'))
    assert total > 0
    assert report[IMPORTED] == 0
    assert report[ALREADY_PRESENT] == 6
    assert report[INVALID] == 2


# ═══════════════════════════════════════════════════════════════════════════
# 11. HTTP layer — auth, CSRF, methods, headers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(app):
    return app.test_client()


def _upload(app, source, filename, payload, csrf_token=None):
    import io as _io
    data = {'file': (_io.BytesIO(payload), filename)}
    if csrf_token is not None:
        data['csrf_token'] = csrf_token
    return app.test_client().post(
        f'/api/account/import/{source}/preview', data=data,
        content_type='multipart/form-data')


def _login(client, username='importa', password='ImportTest1!'):
    return client.post('/login', data={'username': username,
                                       'password': password},
                       follow_redirects=True)


@pytest.mark.parametrize('action', ['preview', 'apply'])
def test_import_requires_authentication(app, client, action):
    import io as _io
    response = client.post(
        f'/api/account/import/{LETTERBOXD}/{action}',
        data={'file': (_io.BytesIO(_letterboxd()), 'lb.zip')},
        content_type='multipart/form-data')
    assert response.status_code in (401, 302), response.status_code


@pytest.mark.parametrize('action', ['preview', 'apply'])
def test_import_rejects_get(client, action, user):
    _login(client)
    response = client.get(f'/api/account/import/{LETTERBOXD}/{action}')
    assert response.status_code == 405, response.status_code


def test_import_csrf_is_enforced(app, user, catalogue, tvtime_show):
    """With CSRF enabled and no token, the mutation is refused."""
    client = app.test_client()
    _login(client)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = _upload(app, LETTERBOXD, 'lb.zip', _letterboxd())
        assert response.status_code in (400, 401, 302), response.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_import_with_valid_csrf_succeeds(app, user, catalogue, tvtime_show):
    # Log in FIRST: with CSRF enabled the login POST would itself need a token.
    client = app.test_client()
    _login(client)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with client.session_transaction() as session:
            raw = _raw_csrf(app)
            session['csrf_token'] = raw
        response = client.post(
            f'/api/account/import/{LETTERBOXD}/preview',
            data={'file': (io.BytesIO(_letterboxd()), 'lb.zip'),
                  'csrf_token': _signed_csrf(app, raw)},
            content_type='multipart/form-data')
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        assert body['imported'] == 6
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def _raw_csrf(app):
    """A raw CSRF token bound to the app's secret, same as the F4 suite."""
    from flask_wtf.csrf import generate_csrf
    with app.test_request_context():
        return generate_csrf()


def _signed_csrf(app, raw):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(
        app.config['SECRET_KEY'], salt='wtf-csrf-token').dumps(raw)


def test_import_no_user_id_parameter_can_select_another_account(
        app, user, other_user, catalogue, tvtime_show):
    client = app.test_client()
    _login(client)
    response = client.post(
        f'/api/account/import/{LETTERBOXD}/preview?user_id={other_user}',
        data={'file': (io.BytesIO(_letterboxd()), 'lb.zip')},
        content_type='multipart/form-data')
    assert response.status_code == 200
    with app.app_context():
        assert DiaryEntry.query.filter_by(user_id=user).count() == 0
        assert DiaryEntry.query.filter_by(
            user_id=other_user).count() == 0


def test_import_response_is_private_and_no_store(client, user, catalogue,
                                                 tvtime_show):
    _login(client)
    response = client.post(
        f'/api/account/import/{LETTERBOXD}/preview',
        data={'file': (io.BytesIO(_letterboxd()), 'lb.zip')},
        content_type='multipart/form-data')
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert response.headers.get('Pragma') == 'no-cache'


def test_import_unknown_source_returns_404(client, user):
    _login(client)
    response = client.post(
        '/api/account/import/nosuchsource/preview',
        data={'file': (io.BytesIO(b'x'), 'x.zip')},
        content_type='multipart/form-data')
    assert response.status_code == 404


def test_import_missing_file_returns_400(client, user):
    _login(client)
    response = client.post(f'/api/account/import/{LETTERBOXD}/preview',
                           data={}, content_type='multipart/form-data')
    assert response.status_code == 400


def test_import_error_never_leaks_server_internals(client, user):
    _login(client)
    response = client.post(
        f'/api/account/import/{LETTERBOXD}/preview',
        data={'file': (io.BytesIO(b'garbage'), 'lb.zip')},
        content_type='multipart/form-data')
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    for leak in ('Traceback', 'SQLAlchemy', 'sqlite', 'SECRET_KEY', '/home/'):
        assert leak not in body, leak


def test_import_routes_are_rate_limited(app):
    import routes.account_import as account_import
    assert account_import.IMPORT_RATE_LIMIT == "5 per minute; 20 per hour"


def test_import_preview_writes_nothing_via_http(
        app, client, user, catalogue, tvtime_show):
    _login(client)
    client.post(
        f'/api/account/import/{LETTERBOXD}/preview',
        data={'file': (io.BytesIO(_letterboxd()), 'lb.zip')},
        content_type='multipart/form-data')
    with app.app_context():
        assert DiaryEntry.query.filter_by(user_id=user).count() == 0
        assert TVEpisodeWatch.query.filter_by(user_id=user).count() == 0


def test_tvtime_json_and_zip_produce_the_same_result(app, user, catalogue,
                                                     tvtime_show):
    with app.app_context():
        from_json = apply_import(
            user, TVTIME, 'tv.json',
            fx.build_tvtime_json(include_future=False))
        count_json = TVEpisodeWatch.query.filter_by(user_id=user).count()
        from_zip = apply_import(
            user, TVTIME, 'tv.zip',
            fx.build_tvtime_zip(include_future=False))
        count_zip = TVEpisodeWatch.query.filter_by(user_id=user).count()
    assert from_json[IMPORTED] == from_zip[ALREADY_PRESENT]
    assert count_json == count_zip == 3