"""Deterministic import fixtures (Task F6).

These build the *bytes* a real export would contain — a Letterboxd ZIP of CSVs
and a TV Time GDPR JSON document — so the parsers are exercised against real
container formats (ZIP headers, encodings, a member file list) rather than
against hand-fed dicts.

Everything is fixed data: no clock, no randomness, no network. The same fixture
must parse identically on every run, which is what makes the import idempotency
tests meaningful.
"""
import csv
import io
import json
import zipfile
from datetime import date, timedelta

# TMDb ids deliberately far outside any autoincrement range the test suite
# reaches. A short probe value can collide with a row id and turn an absence
# assertion into a phantom failure (see docs/conventions.md).
LETTERBOXD_MOVIE_TMDB = 600_000_001
TVTIME_SHOW_TMDB = 600_100_001
TVTIME_MOVIE_TMDB = 600_200_001

BENGALI_TITLE = 'আবার দেখা'
ARABIC_TITLE = 'فيلم رائع'
ACCENTED_TITLE = 'Amélie · Le Fabuleux Destin'


# ── Letterboxd ───────────────────────────────────────────────────────────────

LETTERBOXD_HEADER = ['Date', 'Name', 'Year', 'Letterboxd URI', 'Rating']


def letterboxd_watched_rows():
    """``watched.csv`` rows covering every case the suite asserts.

    Includes: a normal watch, a rewatch of the same film on a later date, a
    zero rating (which must be treated as "no rating"), a full 5.0, a
    Bengali/Arabic/accented title, a row with no name, and a row with no URI
    (which cannot be identified, so it must be reported invalid rather than
    risking a duplicate on re-import).
    """
    return [
        ['2021-03-04 21:00', 'The Matrix', '1999', '/film/the-matrix/', '4.5'],
        ['2021-04-10 20:30', 'The Matrix', '1999', '/film/the-matrix/', '4.0'],
        ['2022-01-02 19:00', BENGALI_TITLE, '2019', '/film/abar-dekha/',
         '4.5'],
        ['2022-02-03 18:00', ARABIC_TITLE, '2020', '/film/film-raeh/',
         '3.5'],
        ['2022-03-04 17:00', ACCENTED_TITLE, '2001',
         '/film/amelie-le-fabuleux-destin/', '5'],
        ['2022-04-05 16:00', 'Unrated Film', '2018', '/film/unrated-film/',
         '0'],
        ['2022-05-06 15:00', '', '2017', '/film/no-name/', '3.0'],
        ['2022-06-07 14:00', 'No URI Film', '2016', '', '3.0'],
    ]


def build_letterboxd_zip(rows=None, include_bad_member=True):
    """A Letterboxd-style data export ZIP."""
    rows = letterboxd_watched_rows() if rows is None else rows
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        text = io.StringIO(newline='')
        writer = csv.writer(text, lineterminator='\r\n')
        writer.writerow(LETTERBOXD_HEADER)
        writer.writerows(rows)
        archive.writestr('watched.csv', text.getvalue())
        # A real Letterboxd export contains several other CSVs. They must be
        # IGNORED, not merged: diary.csv overlaps watched.csv, so reading it
        # would double-count every film.
        archive.writestr('diary.csv', text.getvalue())
        archive.writestr('ratings.csv', text.getvalue())
        archive.writestr('watchlist.csv',
                         'Letterboxd URI,Film Name,Year\r\n'
                         '/film/unwatched/,Unwatched Film,2021\r\n')
        archive.writestr('account-data.csv', 'email,other\r\nx@y.z,1\r\n')
        if include_bad_member:
            # A stray directory entry, which must be skipped, not rejected.
            archive.writestr('subdir/', '')
    return buffer.getvalue()


def build_letterboxd_zip_traversal(payload=b'evil',
                                   name='../escaped.csv'):
    """A ZIP whose member tries to escape the extraction root."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('watched.csv',
                         'Date,Name,Year,Letterboxd URI,Rating\r\n')
        archive.writestr(name, payload)
    return buffer.getvalue()


def build_letterboxd_zip_unsafe_name(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('watched.csv',
                         'Date,Name,Year,Letterboxd URI,Rating\r\n')
        archive.writestr(name, 'x')
    return buffer.getvalue()


def build_zip_with_bomb(size=80 * 1024 * 1024):
    """A genuine zip bomb: a tiny archive that expands enormously.

    80 MB of zeros deflates to a few KB, so the UPLOAD passes the small
    in-memory size check while the uncompressed content does not. This is the
    real attack the uncompressed-size guard exists for — a rewritten header
    would not be, because zipfile reads sizes from the central directory.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('watched.csv',
                         'Date,Name,Year,Letterboxd URI,Rating\r\n')
        archive.writestr('huge.bin', b'\0' * size)
    return buffer.getvalue()


# ── TV Time ──────────────────────────────────────────────────────────────────


def tvtime_document(episodes_per_season=3, seasons=(1,),
                    include_special=False, include_future=True,
                    unwatched_episode=False, show_tmdb=TVTIME_SHOW_TMDB,
                    show_name='F6 Verify Show', movies=True):
    """A TV Time GDPR export document.

    Every season in ``seasons`` carries ``episodes_per_season`` aired+watched
    episodes. Optional extras cover specials (must be refused), an unaired
    future episode (must be refused by F4's gate), and an episode that is
    present but never watched (is not history at all).
    """
    season_entries = []
    for number in seasons:
        eps = []
        for index in range(1, episodes_per_season + 1):
            eps.append({
                'id': number * 1000 + index,
                'number': index,
                'name': 'Episode %d' % index,
                'aired': True,
                'first_aired': '2020-01-%02dT00:00:00.000Z'
                               % min(index, 28),
                'last_watched': '2021-05-%02dT20:00:00.000Z'
                                % min(index, 28),
                'is_specials': False,
            })
        if unwatched_episode:
            eps.append({
                'id': 9001, 'number': episodes_per_season + 1,
                'name': 'Unwatched', 'aired': True,
                'first_aired': '2020-02-01T00:00:00.000Z',
                'last_watched': None, 'is_specials': False,
            })
        season_entries.append({'number': number, 'episodes': eps})

    if include_special:
        season_entries.append({
            'number': 0,
            'episodes': [{
                'id': 5000, 'number': 1, 'name': 'A Special', 'aired': True,
                'first_aired': '2019-01-01T00:00:00.000Z',
                'last_watched': '2021-01-01T10:00:00.000Z',
                'is_specials': True,
            }],
        })

    if include_future:
        # Season 9 sits far beyond the show's aired anchor, so F4's gate must
        # refuse it no matter what the source claims.
        season_entries.append({
            'number': 9,
            'episodes': [{
                'id': 9900, 'number': 99, 'name': 'Far Future',
                'aired': False, 'first_aired': '2099-01-01T00:00:00.000Z',
                'last_watched': '2099-01-02T00:00:00.000Z',
                'is_specials': False,
            }],
        })

    document = {
        'profile': {'id': 1, 'name': 'F6 Verify'},
        'shows': [{
            'id': 4242,
            'name': show_name,
            'tmdb_id': show_tmdb,
            'tvdb_id': 999,
            'seasons': season_entries,
        }],
        'movies': [],
    }
    if movies:
        document['movies'] = [{
            'id': 777, 'name': 'F6 Movie', 'tmdb_id': TVTIME_MOVIE_TMDB,
            'year': 2018, 'last_watched': '2021-07-07T00:00:00.000Z',
            'rating': 8,
        }]
    return document


def build_tvtime_json(**kwargs):
    return json.dumps(tvtime_document(**kwargs)).encode('utf-8')


def build_tvtime_zip(**kwargs):
    """The same document wrapped in a ZIP, as GDPR exports commonly are."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('tvtime.json', build_tvtime_json(**kwargs))
        archive.writestr('README.txt', 'TV Time GDPR export\n')
    return buffer.getvalue()


def build_tvtime_zip_traversal():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('tvtime.json', build_tvtime_json())
        archive.writestr('../../etc/passwd', 'root:x:0:0')
    return buffer.getvalue()


def tvtime_tmdb_shape(show_tmdb=TVTIME_SHOW_TMDB, episodes_per_season=3,
                      seasons=(1,), status='Ended', name='F6 Verify Show'):
    """A TMDb ``tv_show`` payload whose aired set is fully deterministic.

    Registered with the offline TMDb registry so eligibility resolves with no
    network access. ``seasons`` maps season number -> aired episode count,
    which is what F4's ``aired_positions_for_show`` derives its universe from.
    """
    return {
        'id': show_tmdb, 'name': name,
        'overview': '', 'tagline': '',
        'status': status,
        'first_air_date': '2020-01-01', 'last_air_date': '2020-06-01',
        'number_of_seasons': len(seasons),
        'number_of_episodes': sum(seasons.values()),
        'last_episode_to_air': {'season_number': max(seasons),
                                'episode_number': max(seasons.values())},
        'seasons': [
            {'season_number': sn, 'episode_count': ec,
             'air_date': '2020-01-01', 'name': 'Season %s' % sn,
             'overview': '', 'poster_path': ''}
            for sn, ec in sorted(seasons.items())],
        'poster_path': '', 'backdrop_path': '',
        'genres': ['Drama'], 'vote_average': 0, 'vote_count': 0,
        'creator': None, 'cast': [], 'trailer_url': None,
        'recommendations': [], 'reviews': [],
    }


def days_ago(count):
    return date.today() - timedelta(days=count)