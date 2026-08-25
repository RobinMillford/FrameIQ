"""Browse & discovery routes: index, search, trending, news, genres, recommend."""
import calendar
from datetime import datetime

import feedparser
from flask import render_template, request, jsonify
from flask_login import current_user

from api.tmdb_client import (
    fetch_now_playing_movies, fetch_popular_movies, fetch_upcoming_movies,
    fetch_trending_people, fetch_airing_today_shows, fetch_on_the_air_shows,
    fetch_popular_shows, fetch_trending_movies, fetch_movies_by_genre,
    fetch_shows_by_genre, fetch_poster, fetch_tmdb_recommendations,
    search_media,
)
from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY
from models import User, Review, WatchProgress
from routes._main_bp import main
from routes.helpers import get_user_collection_ids

MOVIE_GENRES = {
    'romance': 10749, 'horror': 27, 'fantasy': 14, 'science_fiction': 878,
    'mystery': 9648, 'western': 37, 'drama': 18, 'action': 28, 'comedy': 35,
    'thriller': 53, 'adventure': 12, 'animation': 16, 'crime': 80,
    'family': 10751, 'history': 36, 'music': 10402, 'war': 10752,
    'documentary': 99, 'tv_movie': 10770,
}

TV_GENRES = {
    'action_adventure': 10759, 'animation': 16, 'comedy': 35, 'crime': 80,
    'documentary': 99, 'drama': 18, 'family': 10751, 'kids': 10762,
    'mystery': 9648, 'news': 10763, 'reality': 10764, 'sci_fi_fantasy': 10765,
    'soap': 10766, 'talk': 10767, 'war_politics': 10768, 'western': 37,
}

_NEWS_FEEDS = [
    ("Variety",                 "https://variety.com/feed/"),
    ("Deadline",                "https://deadline.com/feed/"),
    ("The Hollywood Reporter",  "https://www.hollywoodreporter.com/feed/"),
    ("Entertainment Weekly",    "https://ew.com/feed/"),
    ("Screen Rant",             "https://screenrant.com/feed/"),
    ("IGN Entertainment",       "https://feeds.feedburner.com/ign/movies-articles"),
    ("Collider",                "https://collider.com/feed/"),
]


IMG = "https://image.tmdb.org/t/p/"

# Genre names -> TMDb ids (movie discover). Covers the common taste words.
_GENRE_IDS = {
    "action": 28, "adventure": 12, "animation": 16, "comedy": 35,
    "crime": 80, "documentary": 99, "drama": 18, "family": 10751,
    "fantasy": 14, "history": 36, "horror": 27, "music": 10402,
    "mystery": 9648, "romance": 10749, "sci-fi": 878, "science fiction": 878,
    "thriller": 53, "war": 10752, "western": 37,
}


def _user_top_genre(user_id):
    """Return (genre_name, top_rated_title, top_score) or (None, None, None)."""
    reviews = (
        Review.query.filter_by(user_id=user_id)
        .order_by(Review.created_at.desc()).limit(25).all()
    )
    if not reviews:
        return None, None, None

    from collections import Counter
    counts, best = Counter(), None
    for r in reviews:
        if not (r.media and r.media.genres):
            continue
        for g in str(r.media.genres).split(","):
            g = g.strip().lower()
            if g in _GENRE_IDS:
                counts[g] += 1
        if r.rating and (best is None or r.rating > best[1]):
            best = (r.media.title, r.rating * 2)
    if not counts:
        return None, None, None
    top = counts.most_common(1)[0][0]
    return top.title(), (best or (None, None))[0], (best or (None, None))[1]


def _tonights_picks(current_user, count=5):
    """Return a list of hero picks: taste-matched first, then top trending.
    Each pick is a dict with backdrop, poster, title, reason, etc."""
    try:
        trending = cached_tmdb_request(
            f"https://api.themoviedb.org/3/trending/movie/week"
            f"?api_key={TMDB_API_KEY}"
        ).get("results", [])
    except Exception:
        return []
    candidates = [t for t in trending
                  if t.get("backdrop_path") and t.get("poster_path")
                  and t.get("overview")]
    if not candidates:
        return []

    top_genre = top_title = top_score = None
    if current_user.is_authenticated:
        try:
            top_genre, top_title, top_score = _user_top_genre(current_user.id)
        except Exception:
            pass

    picks = []

    # Pick 1: taste-matched (if user has history)
    if top_genre:
        gid = _GENRE_IDS.get(top_genre.lower())
        matches = [t for t in candidates if gid in (t.get("genre_ids") or [])]
        if matches:
            t = matches[0]
            bits = [f"matches your taste for {top_genre}"]
            if top_title and top_score:
                bits.append(f"you rated {top_title} {top_score:.1f}/10")
            bits.append("it's trending this week")
            picks.append(_make_hero(t, " · ".join(bits), candidates))
            candidates = [c for c in candidates if c["id"] != t["id"]]

    # Fill remaining slots with top trending
    for t in candidates[:count - len(picks)]:
        rank = trending.index(t) + 1 if t in trending else 1
        picks.append(_make_hero(t, f"Trending #{rank} this week", candidates))

    return picks[:count]


def _make_hero(t, reason, all_candidates):
    """Format one trending result as a hero slide dict."""
    genre_names = sorted({
        _GENRE_ID_NAMES.get(g, "") for g in (t.get("genre_ids") or [])
    } - {""})[:3]
    date = t.get("release_date") or ""
    return {
        "id": t["id"],
        "title": t.get("title", ""),
        "year": date[:4],
        "overview": (t.get("overview", "") or "")[:260],
        "rating": round(t.get("vote_average", 0), 1),
        "poster": IMG + "w500" + t["poster_path"],
        "backdrop": IMG + "original" + t["backdrop_path"],
        "genres": genre_names,
        "reason": reason,
    }

_GENRE_ID_NAMES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 53: "Thriller",
    10752: "War", 37: "Western",
}


def _trending_rail():
    """Trending movies with posters (raw trending endpoint, cached)."""
    try:
        results = cached_tmdb_request(
            f"https://api.themoviedb.org/3/trending/movie/week"
            f"?api_key={TMDB_API_KEY}"
        ).get("results", [])
    except Exception:
        return []
    return [
        {
            "id": m["id"], "media_type": "movie",
            "title": m.get("title"), "name": m.get("title"),
            "poster_path": m.get("poster_path"),
            "release_date": m.get("release_date"),
            "first_air_date": m.get("release_date"),
            "vote_average": m.get("vote_average", 0),
        }
        for m in results[:14] if m.get("poster_path")
    ]


@main.route('/')
def index():
    trending_backdrops = fetch_trending_movies()
    now_playing = fetch_now_playing_movies()
    popular = fetch_popular_movies()
    # Get IDs from now_playing and popular to exclude
    exclude_ids = {movie['id'] for movie in now_playing + popular}
    upcoming = fetch_upcoming_movies(exclude_ids=exclude_ids)
    airing_today = fetch_airing_today_shows()
    on_the_air = fetch_on_the_air_shows()
    popular_shows = fetch_popular_shows()
    trending_people = fetch_trending_people()

    watchlist_ids, wishlist_ids, viewed_ids = get_user_collection_ids(current_user)

    # ── Tonight's Pick ──
    picks = _tonights_picks(current_user)

    # ── Rails (normalized item shape for the rail card partial) ──
    rails = []

    # Continue watching (authed, in-progress streams)
    if current_user.is_authenticated:
        try:
            progress = (
                WatchProgress.query.filter_by(user_id=current_user.id)
                .filter(WatchProgress.duration > 60,
                        WatchProgress.current_time < WatchProgress.duration * 0.9)
                .order_by(WatchProgress.updated_at.desc()).limit(12).all()
            )
            if progress:
                rails.append({
                    "key": "continue", "title": "Continue Watching",
                    "subtitle": "pick up where you left off",
                    "kind": "continue",
                    "entries": [{
                        "id": p.tmdb_id,
                        "title": p.title or "Untitled",
                        "poster": p.poster_path,
                        "progress": p.progress_pct,
                        "watch_url": p.watch_url,
                        "media_type": p.media_type,
                    } for p in progress],
                })
        except Exception:
            pass

    # Because you rated {genre} (authed with history) else Trending
    top_genre = top_title = top_score = None
    if current_user.is_authenticated:
        try:
            top_genre, top_title, top_score = _user_top_genre(current_user.id)
        except Exception:
            pass

    if top_genre:
        genre_films = fetch_movies_by_genre(_GENRE_IDS.get(top_genre.lower(), 18))
        genre_films = [g for g in genre_films if g.get("poster_path")][:14]
        if genre_films:
            sub = f"because you rated {top_title} {top_score:.1f}/10" if top_title else f"more {top_genre}"
            rails.append({
                "key": "because", "title": f"More {top_genre}",
                "subtitle": sub, "kind": "media", "entries": genre_films,
            })

    trending_items = _trending_rail()
    if trending_items:
        rails.append({
            "key": "trending", "title": "Trending This Week",
            "subtitle": "what everyone is watching", "kind": "media",
            "entries": trending_items,
        })

    rails.append({
        "key": "now_playing", "title": "Now Playing",
        "subtitle": "in cinemas this week", "kind": "media",
        "entries": now_playing,
    })
    rails.append({
        "key": "popular", "title": "Popular Movies",
        "subtitle": "crowd favourites", "kind": "media", "entries": popular,
    })

    # On TV this week: merge airing today + on the air, dedupe by id
    seen_ids = set()
    on_tv = []
    for show in (airing_today + on_the_air):
        if show["id"] not in seen_ids:
            seen_ids.add(show["id"])
            on_tv.append(show)
    rails.append({
        "key": "on_tv", "title": "On TV This Week",
        "subtitle": "airing now", "kind": "media", "entries": on_tv,
    })
    rails.append({
        "key": "popular_tv", "title": "Popular Shows",
        "subtitle": "binge-worthy picks", "kind": "media",
        "entries": popular_shows,
    })
    rails.append({
        "key": "upcoming", "title": "Coming Soon",
        "subtitle": "add to your watchlist before release", "kind": "media",
        "entries": upcoming,
    })

    return render_template(
        'index.html',
        picks=picks,
        rails=[r for r in rails if r["entries"]],
        trending_people=trending_people,
        user_watchlist_ids=watchlist_ids,
        user_wishlist_ids=wishlist_ids,
        user_viewed_ids=viewed_ids,
    )


def _format_search_results(results, kind):
    """Format TMDb search results, dropping items without images."""
    if kind == 'movie':
        return [
            {'id': m['id'], 'title': m['title'],
             'release_date': m.get('release_date', 'N/A'),
             'poster_path': m.get('poster_path')}
            for m in results if m.get('poster_path')
        ]
    if kind == 'tv':
        return [
            {'id': s['id'], 'name': s['name'],
             'first_air_date': s.get('first_air_date', 'N/A'),
             'poster_path': s.get('poster_path')}
            for s in results if s.get('poster_path')
        ]
    return [
        {'id': p['id'], 'name': p['name'],
         'known_for': p.get('known_for_department', 'N/A'),
         'profile_path': p.get('profile_path')}
        for p in results if p.get('profile_path')
    ]


@main.route('/search', methods=['GET', 'POST'])
def search():
    # Support both form data (POST) and query parameters (GET)
    query = request.form.get('query') or request.args.get('query') or request.args.get('q')
    if not query:
        return render_template('index.html', error="Please enter a search term.")

    movies = _format_search_results(search_media(query, 'movie'), 'movie')
    shows = _format_search_results(search_media(query, 'tv'), 'tv')
    people = _format_search_results(search_media(query, 'person'), 'person')

    # Search community members (Local DB)
    community_members = User.query.filter(User.username.ilike(f'%{query}%')).all()

    watchlist_ids, wishlist_ids, viewed_ids = get_user_collection_ids(current_user)
    following_ids = (
        {f.following_id for f in current_user.following if f.is_active}
        if current_user.is_authenticated else set()
    )

    return render_template(
        'search_results.html', query=query, movies=movies, shows=shows,
        people=people, community_members=community_members,
        user_watchlist_ids=watchlist_ids, user_wishlist_ids=wishlist_ids,
        user_viewed_ids=viewed_ids, following_ids=following_ids)


@main.route('/autocomplete')
def autocomplete():
    query = request.args.get('query', '')
    if not query:
        return jsonify({'movies': [], 'shows': [], 'people': []})

    movies = _format_search_results(search_media(query, 'movie'), 'movie')[:5]
    shows = _format_search_results(search_media(query, 'tv'), 'tv')[:5]
    people = _format_search_results(search_media(query, 'person'), 'person')[:5]

    return jsonify({'movies': movies, 'shows': shows, 'people': people})


@main.route('/trending')
def trending_page():
    """Render the trending page"""
    return render_template('trending.html')


@main.route('/news')
def news():
    articles = []
    for source_name, feed_url in _NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get('title', '').strip()
                url = entry.get('link', '')
                if not (title and url):
                    continue
                articles.append({
                    'title': title,
                    'description': entry.get('summary', 'No description available')[:300],
                    'url': url,
                    **_extract_news_image(entry),
                    **_extract_news_date(entry),
                    'source': source_name,
                })
        except Exception:
            continue

    articles.sort(key=lambda a: a['publishedAt'], reverse=True)
    return render_template('news.html', articles=articles[:60])


def _extract_news_image(entry):
    """Pull best thumbnail from RSS entry media fields."""
    image = None
    if entry.get('media_thumbnail'):
        image = entry['media_thumbnail'][0].get('url')
    elif entry.get('media_content'):
        image = entry['media_content'][0].get('url')
    elif entry.get('enclosures'):
        enc = entry['enclosures'][0]
        if enc.get('type', '').startswith('image/'):
            image = enc.get('href') or enc.get('url')
    return {'urlToImage': image}


def _extract_news_date(entry):
    """Parse published timestamp into ISO string."""
    if not entry.get('published_parsed'):
        return {'publishedAt': ''}
    ts = calendar.timegm(entry['published_parsed'])
    return {'publishedAt': datetime.utcfromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%SZ')}


@main.route('/movies')
def movies():
    return render_template('movies.html')


@main.route('/tv_shows')
def tv_shows():
    return render_template('tv_shows.html')


@main.route('/genre/<genre_name>')
def genre_page(genre_name):
    genre_id = MOVIE_GENRES.get(genre_name)
    if genre_id:
        movies = fetch_movies_by_genre(genre_id)
        watchlist_ids, wishlist_ids, viewed_ids = get_user_collection_ids(current_user)
        return render_template(
            'genre.html', genre_name=genre_name.capitalize(), movies=movies,
            user_watchlist_ids=watchlist_ids,
            user_wishlist_ids=wishlist_ids,
            user_viewed_ids=viewed_ids)
    return render_template('genre_not_found.html',
                           genre_name=genre_name.capitalize())


@main.route('/tv_genre/<genre_name>')
def tv_genre_page(genre_name):
    genre_id = TV_GENRES.get(genre_name)
    if genre_id:
        shows = fetch_shows_by_genre(genre_id)
        watchlist_ids, wishlist_ids, viewed_ids = get_user_collection_ids(current_user)
        return render_template(
            'tv_genre.html',
            genre_name=genre_name.replace('_', ' ').capitalize(), shows=shows,
            user_watchlist_ids=watchlist_ids,
            user_wishlist_ids=wishlist_ids,
            user_viewed_ids=viewed_ids)
    return render_template('genre_not_found.html',
                           genre_name=genre_name.replace('_', ' ').capitalize())


def _recommend_page(form_field, media_type, template, no_results_ctx):
    """Shared logic for /recommend and /tv_recommend."""
    name = request.form[form_field]
    results = search_media(name, media_type, include_adult=True)

    if not results:
        return render_template('no_results.html', **no_results_ctx(name))

    searched = results[0]
    is_movie = media_type == 'movie'
    searched_poster = fetch_poster(searched['id'], is_movie=is_movie)
    recs = fetch_tmdb_recommendations(searched['id'], is_movie=is_movie)

    titles, posters, ids = [], [], []
    for rec in recs:
        if rec['id'] != searched['id']:
            titles.append(rec['title'] if is_movie else rec['name'])
            posters.append(fetch_poster(rec['id'], is_movie=is_movie))
            ids.append(rec['id'])

    watchlist_ids, wishlist_ids, viewed_ids = get_user_collection_ids(current_user)

    ctx = {
        'searched_movie' if is_movie else 'searched_show': name,
        'searched_movie_poster' if is_movie else 'searched_show_poster':
            searched_poster,
        'recommend_movie' if is_movie else 'recommend_show': titles,
        'recommend_poster': posters,
        'recommend_ids': ids,
        'user_watchlist_ids': watchlist_ids,
        'user_wishlist_ids': wishlist_ids,
        'user_viewed_ids': viewed_ids,
    }
    return render_template(template, **ctx)


@main.route('/recommend', methods=['POST'])
def recommend():
    return _recommend_page(
        'movie_name', 'movie', 'recommend.html',
        lambda n: {'searched_movie': n})


@main.route('/tv_recommend', methods=['POST'])
def tv_recommend():
    return _recommend_page(
        'show_name', 'tv', 'tv_recommend.html',
        lambda n: {'searched_show': n})
