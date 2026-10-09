"""Task F7 — persistent import mappings, explicit resolution, review import.

Covers the three things F6 could not do:

  * a durable, user-owned mapping from an external title to a FrameIQ title,
    which outranks every automatic match;
  * explicit resolution for unresolved/ambiguous rows — candidates, an
    explicit local search, skipping, and one decision covering a whole show;
  * Letterboxd ``reviews.csv``, reduced to plain text, imported independently
    of watch history, and refused as a conflict when the user already has a
    review.

Everything runs offline. conftest's F3 network guard fails any real connection,
and the resolution tests assert that no external lookup happens while previewing
or resolving.
"""
import io
import json
import zipfile
from datetime import date

import pytest

try:  # tests/ is a package; direct execution still works.
    from tests import import_fixtures as fx
except ImportError:  # pragma: no cover - sys.path forms
    import import_fixtures as fx  # type: ignore[no-redef]

from api.imports import (AMBIGUOUS, CONFLICT, IMPORTED, UNRESOLVED,
                         UNSUPPORTED, MappingError, MediaIndex,
                         SelectionRejected, apply_import, delete_mapping,
                         encode_key, list_mappings, preview, resolution_key,
                         save_mapping, search_local, summary_payload)
from api.imports.mappings import decode_key, load_mapping_set
from api.imports.resolve import candidates_for, media_payload, resolve_movie
from api.imports.sources import (_html_to_text, _letterboxd_film_slug,
                                 parse_letterboxd)
from api.imports.writer import write_movie_review
from models import (DiaryEntry, ImportSourceMapping, MediaItem,
                    Review, User, db)

LETTERBOXD = 'letterboxd'
TVTIME = 'tvtime'

# Distinctive markers (see docs/conventions.md on why these are long strings).
B_MAPPING_NOTE = 'B_MAPPING_USER_ONLY_NOTE'
B_OTHER_TITLE = 'B Other User Only Title 700000003'
B_REVIEW_TEXT = 'B Imported Review Body 800000004'


@pytest.fixture(autouse=True)
def _wipe(app):
    def _wipe_now():
        with app.app_context():
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()
    _wipe_now()
    yield
    _wipe_now()


@pytest.fixture
def users(app):
    with app.app_context():
        owner = User(username='mapper', email='mapper@example.test',
                     email_verified=True)
        owner.set_password('pw')
        other = User(username='intruder', email='intruder@example.test',
                     email_verified=True)
        other.set_password('pw')
        db.session.add_all([owner, other])
        db.session.commit()
        return owner.id, other.id


@pytest.fixture
def media(app):
    """Local titles covering every resolution shape F7 must handle."""
    with app.app_context():
        rows = [
            # Same normalised title, different years -> genuinely ambiguous.
            MediaItem(title='Stalker', release_date=date(1979, 5, 17),
                      media_type='movie', tmdb_id=880_000_001),
            MediaItem(title='Stalker', release_date=date(2002, 1, 1),
                      media_type='movie', tmdb_id=880_000_002),
            # Only a SUBSTRING match for "Solaris".
            MediaItem(title='Solaris: A Space Odyssey',
                      release_date=date(1968, 4, 2), media_type='movie',
                      tmdb_id=880_000_003),
            # Uniquely resolvable.
            MediaItem(title='The Matrix', release_date=date(1999, 3, 31),
                      media_type='movie', tmdb_id=880_000_004),
            # A TV show, for the media_type guard.
            MediaItem(title='Stalker', release_date=date(1979, 5, 17),
                      media_type='tv', tmdb_id=880_000_005),
            MediaItem(title='Dark', release_date=date(2017, 12, 1),
                      media_type='tv', tmdb_id=880_000_006),
        ]
        db.session.add_all(rows)
        db.session.commit()
        return {r.title + '/' + str(r.release_date.year) + '/' +
                r.media_type: r.id for r in rows}


def movie_record(slug, title, year=None, reviewed=False):
    from api.imports.records import MovieImportRecord
    return MovieImportRecord(
        source=LETTERBOXD,
        source_key=(LETTERBOXD, 'watched', slug),
        title=title, release_year=year,
        watched_at=date(2023, 5, 1),
        rating=4.0,
        review_text=B_REVIEW_TEXT if reviewed else None)


def tv_record(show_key, season, episode, title='Mystery'):
    from api.imports.records import TVEpisodeImportRecord
    return TVEpisodeImportRecord(
        source=TVTIME,
        source_key=(TVTIME, show_key, str(season), str(episode)),
        show_title=title, show_external_id=show_key,
        season_number=season, episode_number=episode,
        watched_at=date(2023, 5, 1), rating=None)


def lb_zip(watched, reviews=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('watched.csv', watched)
        if reviews is not None:
            archive.writestr('reviews.csv', reviews)
    return buffer.getvalue()


# ═══ resolution keys ═════════════════════════════════════════════════════════

class TestResolutionKeys:
    """The mapping key must be the identity, not the row."""

    def test_movie_key_is_the_film_slug(self):
        assert resolution_key(movie_record('the-matrix', 'The Matrix')) == (
            LETTERBOXD, 'movie', 'the-matrix')

    def test_watched_and_review_rows_share_one_identity(self):
        """A review-only row must map the same way as the watched row."""
        from api.imports.records import MovieImportRecord
        review_only = MovieImportRecord(
            source=LETTERBOXD,
            source_key=(LETTERBOXD, 'review', 'the-matrix'),
            title='The Matrix', review_text='x')
        assert resolution_key(review_only) == resolution_key(
            movie_record('the-matrix', 'The Matrix'))

    def test_one_key_covers_every_episode_of_a_show(self):
        keys = {resolution_key(tv_record('999', s, e))
                for s, e in [(1, 1), (1, 2), (2, 3), (3, 10)]}
        assert keys == {(TVTIME, 'tv', '999')}

    def test_unknown_source_has_no_key(self):
        from api.imports.records import MovieImportRecord
        record = MovieImportRecord(source='trakt', source_key=('t', 'x', 'y'),
                                   title='X')
        assert resolution_key(record) is None

    def test_empty_source_key_has_no_key(self):
        from api.imports.records import MovieImportRecord
        record = MovieImportRecord(source=LETTERBOXD, source_key=(),
                                   title='X')
        assert resolution_key(record) is None

    def test_per_watch_ordinal_does_not_create_a_second_identity(self):
        """diary.csv URIs carry /film/slug/2 for the second watch."""
        assert _letterboxd_film_slug('/film/the-matrix/2') == 'the-matrix'
        assert _letterboxd_film_slug('/film/the-matrix') == 'the-matrix'
        assert _letterboxd_film_slug('/film/the-matrix/') == 'the-matrix'


# ═══ wire encoding ══════════════════════════════════════════════════════════

class TestKeyEncoding:
    def test_round_trip(self):
        key = (LETTERBOXD, 'movie', 'the-matrix')
        assert decode_key(encode_key(key), LETTERBOXD) == key

    @pytest.mark.parametrize(
        'bad', ['', None, 'nonsense', '{}', '[1]', '["movie", 5]',
                '["audio", "x"]', '["movie", ""]'])
    def test_rejects_malformed(self, bad):
        assert decode_key(bad, LETTERBOXD) is None

    def test_source_comes_from_the_url_not_the_body(self):
        """A client cannot mint a mapping into a namespace it does not own."""
        encoded = encode_key((LETTERBOXD, 'movie', 'the-matrix'))
        assert decode_key(encoded, TVTIME)[0] == TVTIME


# ═══ HTML reduction ═════════════════════════════════════════════════════════

class TestReviewHtmlReduction:
    """Letterboxd's Review column is documented as Text/HTML."""

    @pytest.mark.parametrize('raw,expected', [
        ('<p>One.</p><p>Two.</p>', 'One.\n\nTwo.'),
        ('Line one<br>Line two', 'Line one\nLine two'),
        ('<em>quiet</em> and <strong>loud</strong>', 'quiet and loud'),
        ('Tom &amp; Jerry &lt;3', 'Tom & Jerry <3'),
        ('<a href="http://x.test">link</a>', 'link'),
    ])
    def test_becomes_plain_text(self, raw, expected):
        assert _html_to_text(raw) == expected

    @pytest.mark.parametrize('raw', [None, '', '   ', '\n\t'])
    def test_empty_body_is_absent_not_blank(self, raw):
        assert _html_to_text(raw) is None

    def test_script_markup_is_not_preserved(self):
        out = _html_to_text('<script>alert(1)</script>text')
        assert out == 'text'
        assert 'script' not in out and 'alert' not in out

    @pytest.mark.parametrize('raw,expected', [
        # No-break space becomes an ordinary space.
        ('a\xa0b', 'a b'),
        # The whole zero-width family is removed outright.
        ('a\u200bb', 'ab'),   # zero-width space
        ('a\u200cb', 'ab'),   # zero-width non-joiner
        ('a\u200db', 'ab'),   # zero-width joiner
        ('a\u2060b', 'ab'),   # word joiner
        ('a\ufeffb', 'ab'),   # byte order mark
    ])
    def test_invisible_characters_are_normalised(self, raw, expected):
        assert _html_to_text(raw) == expected


# ═══ reviews.csv parsing ════════════════════════════════════════════════════

class TestLetterboxdReviewsParsing:
    def _members(self, payload):
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return [_Member(name, archive.read(name)) for name in archive.namelist()]

    def test_review_attaches_to_first_watch_without_double_counting(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        records, invalid = parse_letterboxd(self._members(payload))
        matrix = [r for r in records if r.title == 'The Matrix']
        assert len(matrix) == 2, 'both watches survive'
        assert matrix[0].review_text, 'first watch carries the review'
        assert matrix[1].review_text is None, 'the rewatch does not'

    def test_html_is_reduced(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        records, _ = parse_letterboxd(self._members(payload))
        matrix = [r for r in records if r.title == 'The Matrix'][0]
        assert '<p>' not in matrix.review_text
        assert matrix.review_text.startswith('Groundbreaking and still sharp.')

    def test_entities_decoded(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        records, _ = parse_letterboxd(self._members(payload))
        bengali = [r for r in records if r.title == fx.BENGALI_TITLE][0]
        assert bengali.review_text == 'Tom & Jerry <3'

    def test_review_without_watched_row_becomes_its_own_record(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        records, _ = parse_letterboxd(self._members(payload))
        orphan = [r for r in records if r.title == 'Review Only Film']
        assert len(orphan) == 1
        assert orphan[0].review_text
        assert orphan[0].source_metadata['from_reviews_csv'] is True

    def test_blank_review_body_creates_no_record(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        records, _ = parse_letterboxd(self._members(payload))
        assert not [r for r in records if r.title == 'Blank Review Film']

    def test_review_without_uri_is_reported_invalid(self):
        payload = fx.build_letterboxd_zip_with_reviews()
        _records, invalid = parse_letterboxd(self._members(payload))
        assert any('no usable Letterboxd URI' in row.reason for row in invalid)

    def test_missing_review_column_is_reported_not_ignored(self):
        payload = fx.build_letterboxd_reviews_without_body_column()
        records, invalid = parse_letterboxd(self._members(payload))
        assert records, 'watch history still imported'
        assert any('no "Review" column' in row.reason for row in invalid)

    def test_reviews_without_watched_csv_is_rejected(self):
        payload = fx.build_letterboxd_reviews_only()
        with pytest.raises(ValueError, match='No watched.csv'):
            parse_letterboxd(self._members(payload))

    def test_watched_only_export_is_unchanged(self):
        """F6 behaviour must not regress: no reviews means no review text."""
        payload = fx.build_letterboxd_zip()
        records, _ = parse_letterboxd(self._members(payload))
        assert records and all(r.review_text is None for r in records)


class _Member:
    def __init__(self, name, data):
        self.name = name
        self.data = data


# ═══ candidate generation ═══════════════════════════════════════════════════

class TestCandidates:
    def test_year_agreement_ranks_first(self, app, media):
        with app.app_context():
            index = MediaIndex()
            cands = candidates_for(index, 'Stalker', 'movie', 1979)
            assert cands[0].release_date.year == 1979

    def test_substring_only_still_offers_something(self, app, media):
        with app.app_context():
            index = MediaIndex()
            cands = candidates_for(index, 'Solaris', 'movie', 1972)
            assert [c.title for c in cands] == ['Solaris: A Space Odyssey']

    def test_unknown_title_yields_no_candidates(self, app, media):
        with app.app_context():
            assert candidates_for(MediaIndex(), 'Zzz', 'movie', None) == []

    def test_media_type_is_respected(self, app, media):
        with app.app_context():
            index = MediaIndex()
            movies = candidates_for(index, 'Stalker', 'movie')
            assert all(m.media_type == 'movie' for m in movies)

    def test_results_are_stable_across_calls(self, app, media):
        with app.app_context():
            index = MediaIndex()
            first = [m.id for m in candidates_for(index, 'Stalker', 'movie')]
            second = [m.id for m in candidates_for(index, 'Stalker', 'movie')]
            assert first == second

    def test_search_is_local_and_type_filtered(self, app, media):
        with app.app_context():
            index = MediaIndex()
            assert search_local(index, 'stalker', 'tv')[0].media_type == 'tv'
            assert search_local(index, 'nothing-like-this') == []

    def test_ambiguity_is_reported_never_auto_picked(self, app, media):
        with app.app_context():
            # 1985 matches neither local Stalker (1979, 2002), so the loose
            # bucket holds both => genuinely ambiguous.
            resolution = resolve_movie(
                movie_record('stlk', 'Stalker', 1985), MediaIndex())
            assert resolution.status == AMBIGUOUS
            assert resolution.media_item is None

    def test_media_payload_shape(self, app, media):
        with app.app_context():
            index = MediaIndex()
            payload = media_payload(search_local(index, 'The Matrix')[0])
            assert payload['url'] == '/movie/880000004'
            assert set(payload) == {'media_id', 'tmdb_id', 'media_type',
                                    'title', 'year', 'poster_path', 'url'}


# ═══ mapping repository ══════════════════════════════════════════════════════

class TestMappingRepository:
    def test_save_and_load(self, app, users, media):
        owner, _ = users
        with app.app_context():
            save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                         media['Stalker/1979/movie'], source_title='Stalker')
            db.session.commit()
            rows = list_mappings(owner)
            assert len(rows) == 1
            assert rows[0]['source_key'] == 'stlk'
            assert rows[0]['media_title'] == 'Stalker'

    def test_saving_the_same_identity_updates(self, app, users, media):
        owner, _ = users
        with app.app_context():
            save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                         media['Stalker/1979/movie'])
            save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                         media['Stalker/2002/movie'])
            db.session.commit()
            rows = list_mappings(owner)
            assert len(rows) == 1, 'one row per identity, not two'
            assert rows[0]['media_year'] == 2002

    def test_rejects_unknown_source(self, app, users, media):
        owner, _ = users
        with app.app_context():
            with pytest.raises(MappingError):
                save_mapping(owner, 'trakt', 'movie', 'k', 1)

    def test_rejects_media_type_mismatch(self, app, users, media):
        owner, _ = users
        with app.app_context():
            with pytest.raises(MappingError, match='is a tv'):
                save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                             media['Stalker/1979/tv'])

    def test_rejects_missing_media(self, app, users):
        owner, _ = users
        with app.app_context():
            with pytest.raises(MappingError, match='does not exist'):
                save_mapping(owner, LETTERBOXD, 'movie', 'k', 999_999)

    def test_rejects_empty_key(self, app, users, media):
        owner, _ = users
        with app.app_context():
            with pytest.raises(MappingError):
                save_mapping(owner, LETTERBOXD, 'movie', '   ', 1)

    def test_mappings_are_private_to_their_owner(self, app, users, media):
        owner, other = users
        with app.app_context():
            save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                         media['Stalker/1979/movie'])
            db.session.commit()
            assert list_mappings(other) == []
            assert load_mapping_set(other, LETTERBOXD).media_for(
                movie_record('stlk', 'Stalker')) is None

    def test_delete_is_scoped_to_the_owner(self, app, users, media):
        owner, other = users
        with app.app_context():
            mapping = save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                                   media['Stalker/1979/movie'])
            db.session.commit()
            mapping_id = mapping.id
            # Another user's delete attempt must match nothing.
            with pytest.raises(MappingError):
                delete_mapping(other, mapping_id)
            db.session.rollback()
            assert list_mappings(owner), 'row survived the foreign delete'
            delete_mapping(owner, mapping_id)
            db.session.commit()
            assert list_mappings(owner) == []

    def test_stale_mapping_is_listed_not_hidden(self, app, users, media):
        """A mapping the user cannot see is one they can never clean up."""
        owner, _ = users
        with app.app_context():
            mapping = save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                                   media['Stalker/1979/movie'])
            db.session.commit()
            MediaItem.query.filter_by(
                id=mapping.media_id).delete()
            db.session.commit()
            rows = list_mappings(owner)
            assert len(rows) == 1
            assert rows[0]['stale'] is True

    def test_deleting_a_mapping_keeps_history(self, app, users, media):
        owner, _ = users
        with app.app_context():
            mapping = save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                                   media['Stalker/1979/movie'])
            db.session.commit()
            db.session.add(DiaryEntry(user_id=owner, media_id=mapping.media_id,
                                      media_type='movie',
                                      watched_date=date(2023, 5, 1)))
            db.session.commit()
            delete_mapping(owner, mapping.id)
            db.session.commit()
            assert DiaryEntry.query.filter_by(user_id=owner).count() == 1


# ═══ preview / apply with mappings ══════════════════════════════════════════

class TestPreviewAndApply:
    def _payload(self):
        """Stalker 1985 — a year FrameIQ has no film for, so it is ambiguous."""
        return lb_zip('Date,Name,Year,Letterboxd URI,Rating\n'
                      '2023-05-01,Stalker,1985,/film/stlk/,4\n')

    def test_preview_writes_nothing_even_with_selections(
            self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            report = preview(owner, LETTERBOXD, 'x.zip', payload,
                             selections={key: media['Stalker/1979/movie']})
            assert report[IMPORTED] == 1, 'the choice is reflected'
            assert DiaryEntry.query.count() == 0
            assert ImportSourceMapping.query.count() == 0, 'no mapping yet'

    def test_candidates_offered_for_unresolved(self, app, users, media):
        owner, _ = users
        payload = lb_zip('Date,Name,Year,Letterboxd URI,Rating\n'
                         '2023-05-01,Solaris,1972,/film/sol/,4\n')
        with app.app_context():
            report = preview(owner, LETTERBOXD, 'x.zip', payload)
            assert report[UNRESOLVED] == 1
            row = report['details']['movies'][0]
            assert row['resolution_key']
            assert [c['title'] for c in row['candidates']] == [
                'Solaris: A Space Odyssey']

    def test_selection_resolves_and_applies(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload,
                                  selections={key: media['Stalker/1979/movie']})
            assert report[IMPORTED] == 1
            entry = DiaryEntry.query.one()
            assert entry.media_id == media['Stalker/1979/movie']

    def test_unresolved_rows_are_skipped_not_guessed(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        with app.app_context():
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload)
            assert report[AMBIGUOUS] == 1
            assert DiaryEntry.query.count() == 0

    def test_mappings_are_not_saved_by_default(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload,
                                  selections={key: media['Stalker/1979/movie']})
            assert report['mappings_saved'] == 0
            assert ImportSourceMapping.query.count() == 0

    def test_save_mappings_persists_the_choice(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            report = apply_import(
                owner, LETTERBOXD, 'x.zip', payload,
                selections={key: media['Stalker/1979/movie']},
                save_mappings=True)
            assert report['mappings_saved'] == 1
            rows = list_mappings(owner)
            assert rows[0]['source_key'] == 'stlk'

    def test_saved_mapping_resolves_without_a_selection(self, app, users,
                                                        media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', payload,
                         selections={key: media['Stalker/2002/movie']},
                         save_mappings=True)
            db.session.execute(DiaryEntry.__table__.delete())
            db.session.commit()
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload)
            assert report[AMBIGUOUS] == 0, 'the mapping resolved it'
            assert report[IMPORTED] == 1

    def test_mapping_outranks_a_conflicting_selection(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', payload,
                         selections={key: media['Stalker/2002/movie']},
                         save_mappings=True)
            db.session.execute(DiaryEntry.__table__.delete())
            db.session.commit()
            apply_import(owner, LETTERBOXD, 'x.zip', payload,
                         selections={key: media['Stalker/1979/movie']})
            entry = DiaryEntry.query.one()
            assert entry.media_id == media['Stalker/2002/movie'], (
                'the saved mapping outranks a later selection')

    def test_one_show_mapping_covers_every_episode(self, app, users, media):
        owner, _ = users
        # TV Time's show identity comes from tmdb_id (tvdb_id is the
        # fallback), not from the bare `id`, so the document needs one or the
        # adapter reports the row as unidentifiable. Pointing tmdb_id at the
        # local 'Dark' show resolves all three episodes from one identity.
        document = {'shows': [{
            'id': '4242', 'name': 'Dark',
            'tmdb_id': media['Dark/2017/tv'], 'tvdb_id': 999,
            'seasons': [{'number': 1, 'episodes': [
                {'number': 1, 'name': 'A', 'last_watched': '2023-05-01'},
                {'number': 2, 'name': 'B', 'last_watched': '2023-05-02'},
            ]}, {'number': 2, 'episodes': [
                {'number': 1, 'name': 'C', 'last_watched': '2023-05-03'},
            ]}]}]}
        payload = json.dumps(document).encode()
        with app.app_context():
            report = apply_import(owner, TVTIME, 'x.json', payload,
                                  save_mappings=True)
            assert report['mappings_saved'] == 1, (
                'one show identity, saved once, not once per episode'
            )
            mappings = list_mappings(owner, TVTIME)
            assert len(mappings) == 1
            assert mappings[0]['media_type'] == 'tv'
            assert mappings[0]['media_title'] == 'Dark'
            # Every episode of the show was written, not just the first.
            from models import TVEpisodeWatch
            assert TVEpisodeWatch.query.count() == 3

    def test_removing_a_mapping_makes_the_title_undecidable_again(
            self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            mapping = apply_import(
                owner, LETTERBOXD, 'x.zip', payload,
                selections={key: media['Stalker/1979/movie']},
                save_mappings=True)
            assert mapping['mappings_saved'] == 1
            stored = list_mappings(owner)[0]
            delete_mapping(owner, stored['id'])
            db.session.commit()
            report = preview(owner, LETTERBOXD, 'x.zip', payload)
            assert report[AMBIGUOUS] == 1

    def test_apply_is_idempotent_with_mappings(self, app, users, media):
        owner, _ = users
        payload = self._payload()
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', payload,
                         selections={key: media['Stalker/1979/movie']})
            second = apply_import(owner, LETTERBOXD, 'x.zip', payload,
                                  selections={key: media['Stalker/1979/movie']})
            assert second['already_present'] == 1
            assert DiaryEntry.query.count() == 1


# ═══ selection validation ═══════════════════════════════════════════════════

class TestSelectionValidation:
    def _payload(self):
        """Stalker 1985 — a year FrameIQ has no film for, so it is ambiguous."""
        return lb_zip('Date,Name,Year,Letterboxd URI,Rating\n'
                      '2023-05-01,Stalker,1985,/film/stlk/,4\n')

    def test_rejects_media_type_mismatch(self, app, users, media):
        owner, _ = users
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            with pytest.raises(SelectionRejected, match='is a tv'):
                preview(owner, LETTERBOXD, 'x.zip', self._payload(),
                        selections={key: media['Stalker/1979/tv']})

    def test_rejects_unknown_media(self, app, users):
        owner, _ = users
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            with pytest.raises(SelectionRejected, match='does not exist'):
                preview(owner, LETTERBOXD, 'x.zip', self._payload(),
                        selections={key: 999_999})

    def test_rejects_key_absent_from_the_file(self, app, users, media):
        """A client must not steer a row onto a title it invented."""
        owner, _ = users
        key = encode_key((LETTERBOXD, 'movie', 'never-in-this-file'))
        with app.app_context():
            with pytest.raises(SelectionRejected, match='any row'):
                preview(owner, LETTERBOXD, 'x.zip', self._payload(),
                        selections={key: media['Stalker/1979/movie']})

    def test_rejects_malformed_key(self, app, users, media):
        owner, _ = users
        with app.app_context():
            with pytest.raises(SelectionRejected):
                preview(owner, LETTERBOXD, 'x.zip', self._payload(),
                        selections={'nonsense': media['Stalker/1979/movie']})

    def test_rejects_non_object_selections(self, app, users):
        owner, _ = users
        with app.app_context():
            with pytest.raises(SelectionRejected):
                preview(owner, LETTERBOXD, 'x.zip', self._payload(),
                        selections=['a', 'b'])

    def test_a_rejected_selection_writes_nothing(self, app, users, media):
        owner, _ = users
        key = encode_key((LETTERBOXD, 'movie', 'stlk'))
        with app.app_context():
            with pytest.raises(SelectionRejected):
                apply_import(owner, LETTERBOXD, 'x.zip', self._payload(),
                             selections={key: 999_999},
                             save_mappings=True)
            db.session.rollback()
            assert DiaryEntry.query.count() == 0
            assert ImportSourceMapping.query.count() == 0


# ═══ review import ══════════════════════════════════════════════════════════

class TestReviewImport:
    def _payload(self):
        return fx.build_letterboxd_zip_with_reviews()

    def test_review_is_imported_as_plain_text(self, app, users, media):
        owner, _ = users
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            review = Review.query.one()
            assert '<p>' not in review.content
            assert 'Groundbreaking and still sharp.' in review.content
            assert review.rating == 4.5

    def test_review_imports_without_a_watch_event(self, app, users, media):
        """A review is independent of the diary row."""
        owner, _ = users
        # The review row carries NO date, so no watch event can be made from it
        # (FrameIQ never invents "today"). The body must still import.
        payload = lb_zip(
            'Date,Name,Year,Letterboxd URI,Rating\n',
            'Date,Name,Year,Letterboxd URI,Rating,Review\n'
            ',The Matrix,1999,/film/the-matrix/,4.5,'
            '"<p>Only a review.</p>"\n')
        with app.app_context():
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload)
            assert report[UNSUPPORTED] == 1, 'no date -> no watch event'
            assert Review.query.count() == 1, 'but the review still lands'

    def test_existing_user_review_wins_and_is_untouched(self, app, users,
                                                        media):
        owner, _ = users
        with app.app_context():
            matrix_id = media['The Matrix/1999/movie']
            db.session.add(Review(user_id=owner, media_id=matrix_id,
                                  media_type='movie',
                                  content=B_MAPPING_NOTE, rating=5.0))
            db.session.commit()
            report = apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            assert report[CONFLICT] >= 1
            kept = Review.query.filter_by(user_id=owner).all()
            assert any(r.content == B_MAPPING_NOTE for r in kept)
            assert not any(B_REVIEW_TEXT in (r.content or '') for r in kept)

    def test_conflict_does_not_duplicate_the_review(self, app, users, media):
        owner, _ = users
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            first = Review.query.count()
            apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            assert Review.query.count() == first == 1

    def test_review_without_rating_is_not_invented(self, app, users, media):
        owner, _ = users
        # Neither the watch row nor the review row carries a rating, so there is
        # nothing to import without fabricating a star rating.
        payload = lb_zip(
            'Date,Name,Year,Letterboxd URI,Rating\n'
            '2023-05-01,The Matrix,1999,/film/the-matrix/,\n',
            'Date,Name,Year,Letterboxd URI,Rating,Review\n'
            '2023-05-01,The Matrix,1999,/film/the-matrix/,,'
            '"<p>No rating given.</p>"\n')
        with app.app_context():
            report = apply_import(owner, LETTERBOXD, 'x.zip', payload)
            assert report[UNSUPPORTED] >= 1
            assert Review.query.count() == 0, (
                'Review.rating is NOT NULL; a rating was not fabricated')

    def test_writer_refuses_overwrite_directly(self, app, users, media):
        owner, _ = users
        with app.app_context():
            item = db.session.get(MediaItem,
                                  media['The Matrix/1999/movie'])
            db.session.add(Review(user_id=owner, media_id=item.id,
                                  media_type='movie', content=B_MAPPING_NOTE,
                                  rating=5.0))
            db.session.commit()
            result = write_movie_review(owner, item, B_REVIEW_TEXT, 4.0)
            assert result.status == CONFLICT
            db.session.rollback()
            assert Review.query.one().content == B_MAPPING_NOTE

    def test_other_users_review_does_not_conflict(self, app, users, media):
        """Reviews are per-user: a stranger's review is irrelevant."""
        owner, other = users
        with app.app_context():
            matrix_id = media['The Matrix/1999/movie']
            db.session.add(Review(user_id=other, media_id=matrix_id,
                                  media_type='movie',
                                  content=B_OTHER_TITLE, rating=1.0))
            db.session.commit()
            report = apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            assert report[CONFLICT] == 0
            assert Review.query.filter_by(user_id=owner).count() >= 1

    def test_review_import_is_idempotent(self, app, users, media):
        owner, _ = users
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            count = Review.query.count()
            apply_import(owner, LETTERBOXD, 'x.zip', self._payload())
            assert Review.query.count() == count


# ═══ offline guarantee ══════════════════════════════════════════════════════

class TestResolutionIsOffline:
    def test_preview_of_unresolved_makes_no_external_call(self, app, users,
                                                          media):
        """Resolution must not reach TMDb just because a title is unknown."""
        owner, _ = users
        payload = lb_zip('Date,Name,Year,Letterboxd URI,Rating\n'
                         '2023-05-01,Unknown Film,2021,/film/unk/,4\n')
        with app.app_context():
            report = preview(owner, LETTERBOXD, 'x.zip', payload)
            assert report[UNRESOLVED] == 1
            assert report['details']['movies'][0]['candidates'] == []

    def test_search_endpoint_source_is_local(self, app, users, media):
        with app.app_context():
            index = MediaIndex()
            assert search_local(index, 'the matrix')[0].title == 'The Matrix'


# ═══ summary payload ════════════════════════════════════════════════════════

class TestSummaryPayload:
    def test_apply_reports_a_denominator(self, app, users, media):
        """F6 set `details` only in preview, so apply reported 0 of 0."""
        owner, _ = users
        payload = lb_zip('Date,Name,Year,Letterboxd URI,Rating\n'
                         '2023-05-01,The Matrix,1999,/film/the-matrix/,4.5\n')
        with app.app_context():
            summary = summary_payload(
                apply_import(owner, LETTERBOXD, 'x.zip', payload))
            assert summary['records_detected'] == 1
            assert summary['imported'] == 1

    def test_conflict_is_surfaced_in_the_payload(self, app, users, media):
        owner, _ = users
        with app.app_context():
            apply_import(owner, LETTERBOXD, 'x.zip',
                         fx.build_letterboxd_zip_with_reviews())
            summary = summary_payload(
                apply_import(owner, LETTERBOXD, 'x.zip',
                             fx.build_letterboxd_zip_with_reviews()))
            assert CONFLICT in summary
            assert summary[CONFLICT] >= 1


# ═══ model wiring ══════════════════════════════════════════════════════════

class TestModelWiring:
    def test_unique_constraint_blocks_two_rows_per_identity(self, app, users,
                                                            media):
        owner, _ = users
        with app.app_context():
            db.session.add(ImportSourceMapping(
                user_id=owner, source=LETTERBOXD, media_type='movie',
                source_key='dup',
                media_id=media['Stalker/1979/movie']))
            db.session.commit()
            assert ImportSourceMapping.query.count() == 1
            db.session.add(ImportSourceMapping(
                user_id=owner, source=LETTERBOXD, media_type='movie',
                source_key='dup',
                media_id=media['Stalker/1979/movie']))
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_mapping_survives_its_user_being_deleted(self, app, users, media):
        owner, _ = users
        with app.app_context():
            save_mapping(owner, LETTERBOXD, 'movie', 'stlk',
                         media['Stalker/1979/movie'])
            db.session.commit()
            # ORM delete, so the relationship's delete-orphan cascade runs. A
            # bulk query delete bypasses ORM cascades entirely; on Postgres the
            # user_id FK is what prevents that from orphaning a mapping.
            db.session.delete(db.session.get(User, owner))
            db.session.commit()
            assert ImportSourceMapping.query.count() == 0, (
                'mappings cascade with the account that owns them'
            )