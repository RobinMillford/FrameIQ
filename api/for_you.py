"""For You V1 (Feature #6/#7 Phase 4) — deterministic personalized candidates.

The recommendation engine that consumes the PERSISTED TasteProfile
(api/taste_profile.py) and produces a bounded, explainable card list:

    TasteProfile (persisted, feedback-aware)
        ↓  bounded candidate generation (TMDb budget: 5 requests, hard cap)
    candidate merge / dedupe by (media_type, tmdb_id)
        ↓  exclusions (watched / watchlist / wishlist / posterless / adult)
    deterministic ranking (inspectable components, no hidden score)
        ↓  diversity (≤1 per director, ≤7 per dominant genre, media mix)
    small bounded availability bonus (≤12 probes)
        ↓  structured reasons (pure, deterministic — never an LLM)
    ≤14 personalized results (no fabricated placeholders)

Hard budgets, enforced in code:
    TMDb requests      ≤ 5   (2 genre discover + 2 seed recs + 1 trending)
    availability probes ≤ 12 (top-ranked candidates only, ranking bonus)
    DB reads            ≤ 2 major reads (exclusion sets + profile)

Design invariants:

  DETERMINISTIC — same (user, profile state, region, candidate data) →
  same order. No shuffle/randomization of any kind, no request-time
  randomness, no reliance on Python hash randomization. Ties break by
  source priority, then tmdb_id.

  FLASK-INDEPENDENT — this module never touches `request`/`session`;
  routes/ (routes/recommendation_feedback-style conventions) expose it.

  COLD START IS A STATE, NOT AN ERROR — zero-signal users get
  personalized=False and the caller chooses the fallback rail.

  EXCLUSIONS USE EXISTING IDENTITY — user_viewed/user_watchlist/
  user_wishlist junction tables store MediaItem.id, so watched-state
  exclusion happens via ONE joined query on MediaItem(tmdb_id) — no
  MediaItem rows are created, and TMDb-only candidates need none.

  NO LEGACY REWRITE — routes/recommendations.py, the profile page
  builder, browse rails and Taste Match are untouched; this engine runs
  alongside them until the homepage rail adopts it.
"""
import logging
import threading
import time
from collections import defaultdict, namedtuple

from models import (
    db, User, MediaItem, DiaryEntry, Review, TasteProfile,
    user_watchlist, user_wishlist, user_viewed,
)
from api.taste_profile import describe_profile

logger = logging.getLogger(__name__)

# ── Hard budgets (enforced in code, proven in tests) ─────────────────────────
MAX_TMDB_CALLS = 5
MAX_AVAILABILITY_PROBES = 12

# ── Result size ──────────────────────────────────────────────────────────────
MAX_RESULTS = 14
MIN_DESIRED_RESULTS = 12

# ── Personalization gate ─────────────────────────────────────────────────────
FULL_PERSONALIZED_MIN_TITLES = 5
FULL_PERSONALIZED_MIN_CONFIDENCE = 0.4

# ── Candidate generation bounds ──────────────────────────────────────────────
CANDIDATES_PER_DISCOVER = 40     # per genre-discover response slice
CANDIDATES_PER_SEED = 30         # per TMDb recommendations response slice
CANDIDATES_FALLBACK = 30         # trending fallback slice
MIN_VOTE_COUNT = 200             # audited quality floor (where supported)
SEED_MIN_QUALITY = 4.0           # local rating floor for a strong seed
# (app ratings are 0.5–5.0, so 4.0+ = loved it)

# ── Ranking weights (audited V1 — inspectable, single source) ────────────────
W_GENRE_AFFINITY = 1.0
W_DIRECTOR_AFFINITY = 0.5
W_TMDB_QUALITY = 0.3
W_FRESHNESS = 0.2
W_WATCHLIST_INTENT = 0.2
W_AVAILABILITY = 0.2
W_FRIEND_SIGNAL = 0.15           # reserved: always 0 in V1

# ── Diversity ────────────────────────────────────────────────────────────────
MAX_PER_DIRECTOR = 1             # where director data exists
MAX_PER_DOMINANT_GENRE = 7

# ── Deterministic bounded cache (per-process; optional but cheap) ───────────
_CACHE_TTL_SECONDS = 45 * 60
_CACHE_MAX_ENTRIES = 500
_cache = {}
_cache_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ════════════════════════════════════════════════════════════════════════════

def tmdb_quality(vote_average, vote_count):
    """Deterministic 0..1 quality from TMDb votes.

    Bayesian-style shrink toward the 6.5 prior so 9.0-with-3-votes does not
    beat 8.3-with-100k-votes; shrunk rating maps [5.0, 9.0] → [0, 1].
    """
    try:
        va = float(vote_average or 0.0)
        vc = int(vote_count or 0)
    except (TypeError, ValueError):
        return 0.0
    if va <= 0 or vc <= 0:
        return 0.0
    prior = 6.5
    prior_weight = 200.0  # matches the vote_count candidate floor
    shrunk = (vc * va + prior_weight * prior) / (vc + prior_weight)
    return round(max(0.0, min(1.0, (shrunk - 5.0) / 4.0)), 4)


def freshness(release_date, today):
    """Deterministic 0..1 recency of the release date.

    ≤2 years old → 1.0, linear decay to 0 at 22 years. Missing → 0.
    Future releases are clamped to 1.0 (upcoming ≠ better, just new).
    """
    if not release_date or not today:
        return 0.0
    try:
        if hasattr(release_date, 'year'):
            year = release_date.year
        else:
            year = int(str(release_date)[:4])
    except (TypeError, ValueError):
        return 0.0
    age = max(0, today.year - year)  # future releases: age 0
    return round(max(0.0, 1.0 - max(0, age - 2) / 20.0), 4)


def genre_affinity(candidate_genre_names, profile_genres):
    """Mean of the candidate's positive profile genre weights (0..1 scale).

    Profile genre weights are L2-normalized (typically |w| ≤ 1), so the mean
    is naturally bounded. Negative weights are clamped to 0 — absence of
    affinity is neutral here; dislike is expressed by ranking others higher.
    """
    if not candidate_genre_names or not profile_genres:
        return 0.0
    weights = [max(0.0, float(profile_genres.get(g, 0.0)))
               for g in candidate_genre_names]
    return round(sum(weights) / len(weights), 4)


def rank_candidates(candidates, today):
    """Deterministic scoring. Returns [(candidate, score, components)].

    score = 1.0*genre + 0.5*director + 0.3*quality + 0.2*freshness
          + 0.2*watchlist_intent + 0.2*availability + 0.15*friend(0)
          - popularity_penalty
    Components are inspectable; unavailable → 0 (never fabricated).
    Popularity penalty: tiny scale-shape correction (|pop| ≤ 1000 → ≤0.05)
    so a hyper-popular title cannot dominate, and a 0-vote unknown gains a
    negligible nudge over bloated popular titles.
    """
    scored = []
    for c in candidates:
        components = {
            'genre_affinity': genre_affinity(
                c.get('genres') or [], c.get('profile_genres') or {}),
            'director_affinity': 0.0 if not c.get('director') else
            round(max(0.0, float((c.get('profile_directors') or {})
                      .get(c['director'], 0.0))), 4),
            'tmdb_quality': tmdb_quality(c.get('vote_average'),
                                         c.get('vote_count')),
            'freshness': freshness(c.get('release_date'), today),
            'watchlist_intent': 1.0 if c.get('watchlisted') else 0.0,
            'availability': 1.0 if c.get('available') else 0.0,
            'friend_signal': 0.0,  # reserved
        }
        pop = c.get('popularity')
        try:
            popularity_penalty = round(min(0.05, abs(float(pop or 0.0)) / 20000.0), 6)
        except (TypeError, ValueError):
            popularity_penalty = 0.0
        score = round(
            W_GENRE_AFFINITY * components['genre_affinity']
            + W_DIRECTOR_AFFINITY * components['director_affinity']
            + W_TMDB_QUALITY * components['tmdb_quality']
            + W_FRESHNESS * components['freshness']
            + W_WATCHLIST_INTENT * components['watchlist_intent']
            + W_AVAILABILITY * components['availability']
            + W_FRIEND_SIGNAL * components['friend_signal']
            - popularity_penalty,
            6)
        scored.append((c, score, components))
    # Stable tie-breakers: score → source priority → tmdb_id. Never hash.
    scored.sort(key=_rank_sort_key)
    return scored


def _rank_sort_key(scored_item):
    """score → source priority → tmdb_id (stable, never hash-based)."""
    c, score, _ = scored_item
    return (-score, min(c.get('_source_priority', (99,))),
            c['tmdb_id'], c['media_type'])


_DiversityState = namedtuple(
    '_DiversityState',
    ['chosen', 'chosen_keys', 'director_counts', 'genre_counts',
     'source_counts'])


def _admit_candidate(state, c):
    """True when the candidate fits all diversity constraints."""
    if (c['media_type'], c['tmdb_id']) in state.chosen_keys:
        return False
    if c.get('director') and \
            state.director_counts[c['director']] >= MAX_PER_DIRECTOR:
        return False
    for g in c.get('genres') or []:
        if state.genre_counts[g] >= MAX_PER_DOMINANT_GENRE:
            return False
    if state.source_counts[c['source']] >= MAX_RESULTS // 2 + 1:
        return False
    return True


def _take_candidate(state, c):
    """Admit the candidate and update all diversity counters."""
    state.chosen.append(c)
    state.chosen_keys.add((c['media_type'], c['tmdb_id']))
    if c.get('director'):
        state.director_counts[c['director']] += 1
    for g in c.get('genres') or []:
        state.genre_counts[g] += 1
    state.source_counts[c['source']] += 1


def _diversity_pass_alternating(state, pools, limit):
    """Pass 1: alternate movie/tv while both pools have candidates left."""
    active = [mt for mt in ('movie', 'tv') if pools[mt]]
    while len(state.chosen) < limit and any(pools[mt] for mt in active):
        progressed = False
        for mt in list(active):
            if len(state.chosen) >= limit:
                break
            while pools[mt]:
                c, _, _ = pools[mt].pop(0)
                if _admit_candidate(state, c):
                    _take_candidate(state, c)
                    progressed = True
                    break
        if not progressed:
            break


def _diversity_pass_backfill(state, scored, limit):
    """Pass 2: fill leftover slots from the remaining ranked order."""
    for c, _, _ in scored:
        if len(state.chosen) >= limit:
            break
        if (c['media_type'], c['tmdb_id']) in state.chosen_keys:
            continue
        if _admit_candidate(state, c):
            _take_candidate(state, c)


def apply_diversity(scored, limit=MAX_RESULTS):
    """Diversity-constrained selection over a ranked list.

    ≤1 per director (where known), ≤7 per dominant genre, no single source
    fills the rail, movie/TV mix preserved when candidates support both.
    Underfilled slots are backfilled from the remainder (never fabricate).
    """
    state = _DiversityState(
        chosen=[], chosen_keys=set(),
        director_counts=defaultdict(int), genre_counts=defaultdict(int),
        source_counts=defaultdict(int))
    pools = {mt: [t for t in scored if t[0]['media_type'] == mt]
             for mt in ('movie', 'tv')}
    _diversity_pass_alternating(state, pools, limit)
    _diversity_pass_backfill(state, scored, limit)
    return state.chosen


def build_reason(candidate):
    """Pure structured reason from the candidate's own merged evidence.

    Never fabricates: the claimed kind must be backed by the evidence that
    actually produced the candidate, and availability claims only appear
    when the availability service verified a stream/free match.
    """
    matched = candidate.get('matched_genres') or []
    seeds = candidate.get('seed_titles') or []
    if seeds:
        return {
            'kind': 'similar_title',
            'text': f"Because you rated {seeds[0]} highly",
            'evidence': [{'seed_title': s} for s in seeds]
            + ([{'genre': g} for g in matched]),
        }
    # Director reason ONLY when the profile actually carries positive
    # affinity for THIS candidate's persisted director — never fabricated
    # from a bare name match.
    affinity = candidate.get('profile_directors') or {}
    if (candidate.get('director')
            and float(affinity.get(candidate['director']) or 0.0) > 0.0):
        return {
            'kind': 'director_affinity',
            'text': f"Because you liked films by {candidate['director']}",
            'evidence': [{'director': candidate['director'],
                          'affinity': affinity[candidate['director']]}]
            + ([{'genre': g} for g in matched]),
        }
    if matched:
        return {
            'kind': 'genre_affinity',
            'text': f"Because you like {matched[0].lower()}",
            'evidence': [{'genre': g} for g in matched],
        }
    if candidate.get('available'):
        return {
            'kind': 'availability',
            'text': 'Available on one of your services',
            'evidence': [{'provider': p} for p in candidate['providers']],
        }
    return {
        'kind': 'trending',
        'text': 'Popular right now',
        'evidence': [],
    }


# ════════════════════════════════════════════════════════════════════════════
# Local state (≤2 major reads)
# ════════════════════════════════════════════════════════════════════════════

def _resolve_local_items(local_ids):
    """One batched MediaItem lookup: MediaItem.id → identity/title row."""
    if not local_ids:
        return {}
    return {item.id: item for item in MediaItem.query.filter(
        MediaItem.id.in_(local_ids)).with_entities(
        MediaItem.id, MediaItem.tmdb_id, MediaItem.media_type,
        MediaItem.title).all()}


def _collect_watched_exclusions(watched_rows, wishlist_rows, watchlist_rows,
                                id_map):
    """Junction-table rows → (excluded keys, watchlisted keys)."""
    exclude_keys, watchlisted_keys = set(), set()
    for r in list(watched_rows) + list(wishlist_rows):
        item = id_map.get(r.media_id)
        if item:
            exclude_keys.add((item.media_type, item.tmdb_id))
    for r in watchlist_rows:
        item = id_map.get(r.media_id)
        if item:
            key = (item.media_type, item.tmdb_id)
            exclude_keys.add(key)
            watchlisted_keys.add(key)
    return exclude_keys, watchlisted_keys


def _collect_history_seeds(diary_rows, review_rows, id_map):
    """Diary/review rows → (watched exclusion keys, strong-seed pool)."""
    exclude_keys, seed_pool = set(), []
    for d in diary_rows:
        item = id_map.get(d.media_id)
        if item:
            exclude_keys.add((item.media_type, item.tmdb_id))
            if d.rating is not None and d.rating >= SEED_MIN_QUALITY:
                seed_pool.append((d.rating, d.media_type, item.tmdb_id,
                                  item.title, 'diary'))
    for r in review_rows:
        item = id_map.get(r.media_id)
        if item:
            exclude_keys.add((item.media_type, item.tmdb_id))
            if r.rating >= SEED_MIN_QUALITY:
                seed_pool.append((r.rating, r.media_type, item.tmdb_id,
                                  item.title, 'review'))
    return exclude_keys, seed_pool


def _select_seeds(seed_pool):
    """Deterministic seed selection: rating desc, stable key order, ≤2."""
    seed_pool.sort(key=lambda s: (-s[0], s[2], s[1]))
    seeds, seen = [], set()
    for rating, media_type, tmdb_id, title, source_label in seed_pool:
        key = (media_type, tmdb_id)
        if key in seen:
            continue
        seen.add(key)
        seeds.append({'media_type': media_type, 'tmdb_id': tmdb_id,
                      'title': title, 'rating': rating,
                      'source': source_label})
    return seeds[:2]


def _load_local_state(user_id):
    """One joined exclusion read + one seeds read.

    Read 1: exclusion state — watched/watchlist/wishlist junction rows and
    diary entries, all joined to MediaItem ONCE, yielding (media_type,
    tmdb_id) exclusion keys plus watchlisted keys for the intent bonus.
    Read 2: seed selection — top positive diary/review evidence (TMDb ids
    via the same join, plus title names for reasons).

    No N+1: two major queries total, joined, bounded.
    """
    # ── Read 1: exclusions (watched ∪ watchlist ∪ wishlist ∪ diary) ──
    watched_rows = db.session.execute(
        user_viewed.select().where(user_viewed.c.user_id == user_id)).all()
    watchlist_rows = db.session.execute(
        user_watchlist.select().where(user_watchlist.c.user_id == user_id)).all()
    wishlist_rows = db.session.execute(
        user_wishlist.select().where(user_wishlist.c.user_id == user_id)).all()
    diary_rows = (
        DiaryEntry.query.with_entities(
            DiaryEntry.media_id, DiaryEntry.media_type, DiaryEntry.rating,
            DiaryEntry.is_rewatch)
        .filter_by(user_id=user_id)
        .order_by(DiaryEntry.watched_date.desc()).limit(50).all()
    )
    review_rows = (
        Review.query.with_entities(
            Review.media_id, Review.media_type, Review.rating)
        .filter(Review.user_id == user_id, Review.rating.isnot(None))
        .order_by(Review.rating.desc()).limit(50).all()
    )

    all_local_ids = {r.media_id for r in watched_rows} \
        | {r.media_id for r in watchlist_rows} \
        | {r.media_id for r in wishlist_rows} \
        | {r.media_id for r in diary_rows} \
        | {r.media_id for r in review_rows}
    id_map = _resolve_local_items(all_local_ids)

    exclude_keys, watchlisted_keys = _collect_watched_exclusions(
        watched_rows, wishlist_rows, watchlist_rows, id_map)
    history_excludes, seed_pool = _collect_history_seeds(
        diary_rows, review_rows, id_map)
    exclude_keys |= history_excludes

    return {
        'exclude_keys': exclude_keys,
        'watchlisted_keys': watchlisted_keys,
        'seeds': _select_seeds(seed_pool),
    }


# ════════════════════════════════════════════════════════════════════════════
# TMDb candidate generation (hard budget: MAX_TMDB_CALLS)
# ════════════════════════════════════════════════════════════════════════════

# Profile genre labels are stored exactly as MediaItem stores them (TMDb
# genre names). This map covers the discover API's with_genres ids for the
# common genres; genres outside it fall back to the trending source.
_GENRE_IDS = {
    'action': 28, 'adventure': 12, 'animation': 16, 'comedy': 35,
    'crime': 80, 'documentary': 99, 'drama': 18, 'family': 10751,
    'fantasy': 14, 'history': 36, 'horror': 27, 'music': 10402,
    'mystery': 9648, 'romance': 10749, 'science fiction': 878,
    'thriller': 53, 'war': 10752, 'western': 37,
}


class _TmdbBudget:
    """Hard call-cap enforcement — raises when exhausted (callers catch)."""

    def __init__(self, max_calls=MAX_TMDB_CALLS):
        self.remaining = max_calls

    def spend(self):
        if self.remaining <= 0:
            raise _BudgetExhausted()
        self.remaining -= 1


class _BudgetExhausted(Exception):
    pass


def _normalize_candidate(raw, media_type, source, source_priority,
                         profile, matched_genres=None, seed=None):
    """TMDb result dict → internal candidate (in-memory only, never stored)."""
    genres = []
    for gid in raw.get('genre_ids') or []:
        for name, num in _GENRE_IDS.items():
            if num == gid and name not in genres:
                genres.append(name)
    matched = [g for g in (matched_genres or []) if g in genres] or \
        ([matched_genres[0]] if matched_genres else [])
    return {
        'tmdb_id': raw.get('id'),
        'media_type': media_type,
        'title': raw.get('title') or raw.get('name'),
        'poster_path': raw.get('poster_path'),
        'release_date': raw.get('release_date') or raw.get('first_air_date'),
        'genres': genres,
        'vote_average': raw.get('vote_average'),
        'vote_count': raw.get('vote_count'),
        'popularity': raw.get('popularity'),
        'source': source,
        '_source_priority': (source_priority,),
        'matched_genres': matched,
        'profile_genres': profile.genre_weights if profile else {},
        'profile_directors': profile.director_affinity if profile else {},
        'seed_titles': ([seed['title']] if seed else []),
        'watchlisted': False,
        'available': False,
        'providers': [],
        'director': None,  # set locally from persisted capture (Phase 10)
    }


def _discover_candidates(profile, budget, described):
    """S1: TMDb discover for the top ≤2 profile genres (≤2 calls)."""
    from api.tmdb.cache import cached_tmdb_request
    from api.tmdb.config import TMDB_API_KEY

    candidates = []
    for genre in [g['genre'] for g in described['top_positive_genres'][:2]]:
        genre_id = _GENRE_IDS.get(genre.lower())
        if genre_id is None or budget.remaining <= 0:
            continue
        media_type = 'movie'  # discover mixes via popularity; movies dominate V1
        try:
            budget.spend()
            url = (
                f"https://api.themoviedb.org/3/discover/{media_type}"
                f"?api_key={TMDB_API_KEY}&with_genres={genre_id}"
                f"&sort_by=popularity.desc&vote_count.gte={MIN_VOTE_COUNT}"
                "&include_adult=false&page=1"
            )
            data = cached_tmdb_request(url)
            for raw in (data.get('results') or [])[:CANDIDATES_PER_DISCOVER]:
                candidates.append(_normalize_candidate(
                    raw, media_type, f'genre:{genre}', 1, profile,
                    matched_genres=[genre]))
        except _BudgetExhausted:
            break
        except Exception:
            logger.warning("For You genre discovery failed for %s", genre,
                           exc_info=True)
    return candidates


def _seed_candidates(profile, local, budget):
    """S2: TMDb recommendations from ≤2 strong local seeds (≤2 calls)."""
    from api.tmdb.search import fetch_tmdb_recommendations

    candidates = []
    for seed in local['seeds']:
        if budget.remaining <= 0:
            break
        try:
            budget.spend()
            recs = fetch_tmdb_recommendations(
                seed['tmdb_id'], is_movie=(seed['media_type'] == 'movie'),
                max_recommendations=CANDIDATES_PER_SEED)
            for raw in recs:
                candidates.append(_normalize_candidate(
                    raw, seed['media_type'], 'similar_title', 2, profile,
                    seed=seed))
        except _BudgetExhausted:
            break
        except Exception:
            logger.warning("For You seed recommendations failed for %s",
                           seed['tmdb_id'], exc_info=True)
    return candidates


def _trending_candidates(profile, budget):
    """S0: trending fallback — only when candidate volume is insufficient."""
    from api.tmdb.cache import cached_tmdb_request
    from api.tmdb.config import TMDB_API_KEY

    candidates = []
    if budget.remaining <= 0:
        return candidates
    try:
        budget.spend()
        url = (
            f"https://api.themoviedb.org/3/trending/movie/day"
            f"?api_key={TMDB_API_KEY}"
        )
        data = cached_tmdb_request(url)
        for raw in (data.get('results') or [])[:CANDIDATES_FALLBACK]:
            candidates.append(_normalize_candidate(
                raw, 'movie', 'trending', 3, profile))
    except _BudgetExhausted:
        pass
    except Exception:
        logger.warning("For You trending fallback failed", exc_info=True)
    return candidates


def _generate_candidates(profile, local, budget):
    """S1 genre discovery → S2 seeds → (S3 director: n/a) → S0 fallback.

    Every TMDb touch goes through budget.spend(); once the budget is
    exhausted generation stops cleanly and ranking proceeds with what
    exists. Individual source failures degrade to empty lists.
    """
    described = describe_profile(profile)
    candidates = _discover_candidates(profile, budget, described)
    candidates += _seed_candidates(profile, local, budget)

    # ── S3: director probe — superseded by Phase 10 local tagging ──
    # Director affinity now comes from persisted evidence (Phase 10) and
    # candidates are tagged locally via _attach_local_directors(); there is
    # still NO TMDb person/credits call in this engine. A separate S3
    # discovery source can be revisited in a later phase if needed.

    if len(candidates) < MIN_DESIRED_RESULTS:
        candidates += _trending_candidates(profile, budget)
    return candidates


# ════════════════════════════════════════════════════════════════════════════
# Merge / dedupe / exclusions
# ════════════════════════════════════════════════════════════════════════════

def _merge_candidates(candidates):
    """Dedupe by (media_type, tmdb_id) — movie/tv ids are separate spaces.

    Multi-source candidates merge their sources (strongest priority wins for
    the primary source label; all labels kept in `sources`) and preserve the
    strongest explanation evidence (matched genres + seed titles union).
    """
    merged = {}
    for c in candidates:
        key = (c['media_type'], c['tmdb_id'])
        existing = merged.get(key)
        if existing is None:
            c['sources'] = [c['source']]
            merged[key] = c
            continue
        for field in ('matched_genres', 'seed_titles'):
            for v in c.get(field) or []:
                if v not in existing.get(field) or []:
                    existing.setdefault(field, []).append(v)
        existing.setdefault('sources', [existing['source']])
        if c['source'] not in existing['sources']:
            existing['sources'].append(c['source'])
        if c['_source_priority'] < existing['_source_priority']:
            existing['source'] = c['source']
            existing['_source_priority'] = c['_source_priority']
    return list(merged.values())


def _attach_local_directors(candidates):
    """Tag movie candidates with persisted director names (one batched read).

    Phase 10: scripts/enrich_directors.py populates Director/MediaDirector
    offline; this lookup is LOCAL only — a director name here can never
    trigger a TMDb credits call. Skipped entirely when no candidate can
    benefit (empty profile director map), so cold/no-director users keep
    the exact previous query count and behavior. TV has no persisted
    series-level director evidence (see models/director.py), so TV
    candidates are never tagged.
    """
    want = any(
        c['media_type'] == 'movie' and (c.get('profile_directors') or {})
        for c in candidates)
    if not want:
        return candidates
    tmdb_ids = {c['tmdb_id'] for c in candidates
                if c['media_type'] == 'movie' and c.get('tmdb_id') is not None}
    if not tmdb_ids:
        return candidates
    from models.director import Director, MediaDirector
    item_ids = {
        m.id for m in MediaItem.query.filter(
            MediaItem.media_type == 'movie',
            MediaItem.tmdb_id.in_(tmdb_ids)).with_entities(
            MediaItem.id, MediaItem.tmdb_id).all()}
    if not item_ids:
        return candidates
    rows = (db.session.query(MediaItem.tmdb_id, Director.name)
            .join(MediaDirector, MediaDirector.media_item_id == MediaItem.id)
            .join(Director, MediaDirector.director_id == Director.id)
            .filter(MediaItem.id.in_(item_ids)).all())
    names = defaultdict(list)
    for tmdb_id, name in rows:
        names[tmdb_id].append(name)
    for c in candidates:
        if c['media_type'] != 'movie':
            continue
        local = sorted(names.get(c['tmdb_id']) or [])
        c['directors'] = local or None
        # Engine contract: `director` is ONE name — the locally credited
        # director with the strongest POSITIVE profile affinity (ties →
        # alphabetical). None when no local evidence or no positive
        # affinity, so scoring/diversity/reasons never act on weak or
        # negative evidence.
        affinity = c.get('profile_directors') or {}
        positive = [(float(affinity.get(n) or 0.0), n) for n in local]
        positive = [p for p in positive if p[0] > 0.0]
        c['director'] = (min(positive, key=lambda p: (-p[0], p[1]))[1]
                         if positive else None)
    return candidates


def _apply_exclusions(candidates, local):
    """Drop watched/watchlisted/wishlisted/posterless candidates.

    Adult exclusion follows the existing application policy: candidate
    generation already requests include_adult=false, and TMDb list results
    never flag adult content inline — so nothing further is filtered here
    beyond that policy. Unfamiliar titles and TMDb-only candidates are NOT
    excluded, and no MediaItem rows are created for exclusion purposes.
    """
    kept = []
    for c in candidates:
        if not c.get('poster_path'):
            continue  # posterless excluded
        if c.get('tmdb_id') is None:
            continue
        key = (c['media_type'], c['tmdb_id'])
        if key in local['exclude_keys']:
            continue
        if key in local['watchlisted_keys']:
            c['watchlisted'] = True  # bonus flag, NOT an exclusion
        kept.append(c)
    return kept


# ════════════════════════════════════════════════════════════════════════════
# Availability (bounded: ≤12 probes, bonus only)
# ════════════════════════════════════════════════════════════════════════════

def _apply_availability_bonus(candidates, user_region, selected_provider_ids):
    """Probe ≤MAX_AVAILABILITY_PROBES top candidates (list is pre-ranked).

    Only stream/free matches against the user's OWN services give the bonus
    (rent/buy never count). Unknown/failed availability → no bonus, no
    exclusion. Failures degrade to 'unknown' silently.
    """
    from api.availability import get_availability, match_my_services

    if not selected_provider_ids:
        return 0
    probes = 0
    for c in candidates[:MAX_AVAILABILITY_PROBES]:
        try:
            probes += 1
            availability = get_availability(
                c['media_type'], c['tmdb_id'], region=user_region)
            match = match_my_services(availability, selected_provider_ids)
            if match['available']:
                c['available'] = True
                c['providers'] = match['matches']
        except Exception:
            logger.warning("Availability probe failed for %s/%s",
                           c['media_type'], c['tmdb_id'], exc_info=True)
    return probes


# ════════════════════════════════════════════════════════════════════════════
# Cold-start classification
# ════════════════════════════════════════════════════════════════════════════

def _classify(profile):
    """(mode, reason) from the persisted profile — never recomputes."""
    if profile is None:
        return 'cold', 'no_taste_profile_yet'
    described = describe_profile(profile)
    titles = described.get('distinct_title_count') or 0
    confidence = described.get('confidence') or 0.0
    signals = described.get('signal_count') or 0
    if titles == 0 or signals == 0:
        return 'cold', 'no_meaningful_signals'
    if titles >= FULL_PERSONALIZED_MIN_TITLES \
            and confidence >= FULL_PERSONALIZED_MIN_CONFIDENCE:
        return 'full', 'learned_taste'
    if titles >= 2:
        return 'hedged', 'exploring_preferences'
    return 'hedged', 'few_signals'


# ════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════

def _user_services(user_id, region):
    """The user's My Services provider ids (one bounded local query)."""
    from models.streaming import UserStreamingService
    rows = (UserStreamingService.query
            .filter_by(user_id=user_id, region=region)
            .with_entities(UserStreamingService.provider_id).all())
    return [r.provider_id for r in rows]


def _region_for(user):
    return (user.streaming_region or 'US').upper()[:2]


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[1] < _CACHE_TTL_SECONDS:
            return hit[0]
        if hit:
            del _cache[key]
    return None


def _cache_put(key, value):
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[:max(1, _CACHE_MAX_ENTRIES // 10)]:
                del _cache[k]
        _cache[key] = (value, time.time())


def get_for_you(user_id, region=None, limit=MAX_RESULTS):
    """Bounded, deterministic personalized candidates for one user.

    Returns a plain dict (never SQLAlchemy models, never raw internals):
        {'personalized': bool, 'mode': str, 'confidence': float,
         'reason_state': str, 'items': [...]}
    Cold-start users get personalized=False and no items — the CALLER picks
    the fallback rail. Anonymous access is impossible: user_id is required.
    """
    limit = max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))

    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} not found")
    region = (region or _region_for(user) or 'US').upper()[:2]

    profile = TasteProfile.query.filter_by(user_id=user_id).first()
    mode, reason_state = _classify(profile)

    cache_key = (user_id, region, limit, mode,
                 profile.profile_version if profile else 0,
                 profile.updated_at.isoformat() if profile else None)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if mode == 'cold':
        result = {'personalized': False, 'mode': mode, 'confidence': 0.0,
                  'reason_state': reason_state, 'items': []}
        _cache_put(cache_key, result)
        return result

    from datetime import datetime
    today = datetime.utcnow().date()
    local = _load_local_state(user_id)
    budget = _TmdbBudget()
    candidates = _generate_candidates(profile, local, budget)
    candidates = _merge_candidates(candidates)
    candidates = _apply_exclusions(candidates, local)
    candidates = _attach_local_directors(candidates)

    scored = rank_candidates(candidates, today)
    ranked = [c for c, _, _ in scored]
    probes = _apply_availability_bonus(
        ranked, region, _user_services(user_id, region))
    if probes:
        # Availability may change the order — re-rank deterministically.
        scored = rank_candidates(candidates, today)
        ranked = [c for c, _, _ in scored]

    selected = apply_diversity(scored, limit=limit)
    items = [_item(c) for c in selected]
    result = {
        'personalized': True,
        'mode': mode,
        'confidence': describe_profile(profile)['confidence'],
        'reason_state': reason_state,
        'items': items,
    }
    _cache_put(cache_key, result)
    return result


def _item(candidate):
    """Final card shape: identity, display, structured reason. No scores."""
    return {
        'tmdb_id': candidate['tmdb_id'],
        'media_type': candidate['media_type'],
        'title': candidate['title'],
        'poster_path': candidate['poster_path'],
        'release_date': candidate['release_date'],
        'source': candidate['source'],
        'reason': build_reason(candidate),
    }
