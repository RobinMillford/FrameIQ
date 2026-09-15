"""Canonical taste profile computation (Feature #6, Phase 2).

The SINGLE source of truth for turning a user's first-party behavioral
data into the persisted, explainable TasteProfile (models/taste_profile.py):

    local persisted signals (reviews, diary, likes, tags, watchlist,
                             episode ratings, recommendation feedback)
        ↓  compute_profile(user_id)
    weighted evidence (quality factor × signal weight × recency decay)
        ↓  per-dimension aggregation + L2 normalization
        ↓  (genre / decade / director / media type / runtime)
    persisted TasteProfile row
        ↓  (For You rail, CineBot context, Smart List filters)

Design invariants:

  LOCAL DATA ONLY — zero network/TMDb calls. compute_profile() reads a
  bounded number of queries and must stay callable from a nightly batch
  without becoming an external-request fanout.

  PURE CORE — rating_quality, recency_decay, normalize_l2, decade_from_
  release_date, weighted_percentile and _confidence are deterministic,
  independently testable functions.

  DERIVED STATE — profiles are recomputed from source tables at any time;
  nothing here mutates source data, and repeated runs are idempotent
  (same inputs → same stored profile, no duplicate rows).

  NO DUPLICATE COUNTING — each observed event contributes through exactly
  ONE signal. A diary entry with a rating contributes via diary_rating
  (0.9); its is_rewatch flag additionally contributes via diary_rewatch
  (0.7) because a rewatch is genuinely a second viewing event, but the
  rating itself is never also counted as a like/watchlist signal, and
  vice versa.

Dimension-specific notes (verified against the current data model):

  director_affinity — deliberately EMPTY in this phase. No persisted
  director evidence exists (directors are fetched from TMDb credits at
  request time, never stored), and this service must not make external
  calls or invent new persistence. The field is ready for the later
  director-capture phase.

  runtime_pref — computed ONLY from MediaItem.runtime that already exists
  locally; titles without a persisted runtime are skipped (no backfill,
  no TMDb fetch).

  episode ratings — TVEpisodeWatch.show_id is a TMDb show id; evidence is
  mapped to the PARENT SHOW's dimensions via MediaItem (tmdb_id, 'tv'),
  never treated as a movie id.
"""
import logging
import math
from collections import defaultdict
from datetime import date, datetime

from models import (
    db, User, MediaItem, Review, DiaryEntry, MediaLike, UserMediaTag,
    TVEpisodeWatch, user_watchlist, TasteProfile,
)

logger = logging.getLogger(__name__)

# ── Signal weights (audited V1 model — do not add signal types here) ─────────
W_EPISODE_RATING = 1.0
W_REVIEW_RATING = 1.0
W_DIARY_RATING = 0.9
W_MEDIA_LIKE = 0.8
W_DIARY_REWATCH = 0.7
W_TAG = 0.3
W_WATCHLIST_ADD = 0.2

# Recommendation-feedback signals (Feature #7 Phase 3) — a SEPARATE, weaker
# evidence family than explicit ratings. These are recommendation-interaction
# signals, not star ratings: a click/save is weaker than any rating, and
# impression/already_watched are exposure/bookkeeping events with ZERO taste
# evidence. Constant map (not scattered literals) so tests and future
# analytics read one table. Surface/source deliberately do NOT modify the
# weight (no surface weighting, no source weighting in V1).
W_FEEDBACK_CLICK = 0.25
W_FEEDBACK_SAVED = 0.35
W_FEEDBACK_RATED = 1.0
W_FEEDBACK_NOT_INTERESTED = -0.75
W_FEEDBACK_IMPRESSION = 0.0        # exposure, never preference
W_FEEDBACK_ALREADY_WATCHED = 0.0   # real viewing is captured by canonical signals

FEEDBACK_EVENT_WEIGHTS = {
    'click': W_FEEDBACK_CLICK,
    'saved': W_FEEDBACK_SAVED,
    'rated': W_FEEDBACK_RATED,
    'not_interested': W_FEEDBACK_NOT_INTERESTED,
    'impression': W_FEEDBACK_IMPRESSION,
    'already_watched': W_FEEDBACK_ALREADY_WATCHED,
}
assert set(FEEDBACK_EVENT_WEIGHTS) == set(
    __import__('models.recommendation_feedback', fromlist=['EVENTS']).EVENTS), \
    'FEEDBACK_EVENT_WEIGHTS must cover every RecommendationFeedback event'

# Bounded feedback read: the most recent per-user feedback rows. Non-zero-
# weight events are once-per-day per (user, media, surface, event) by the
# model's partial unique index, so real evidence grows slowly — 300 rows is
# far beyond a user's monthly interactive feedback while keeping the nightly
# job's per-user query bounded (full history stays available for analytics).
_FEEDBACK_ROW_LIMIT = 300

# Phase 10: top-N directors kept in director_affinity_json (audited V1
# target). The dimension reuses existing evidence events — see
# _director_affinity().
_DIRECTOR_TOP_N = 8

# Watchlist is weak intent: it may inform genre/decade/media-type but must
# never overwhelm explicit ratings. Hard cap on titles drawn from it.
_WATCHLIST_CAP = 30

# Per-title cap on tag contributions so many tags on one title cannot
# dominate the profile (bounded additive, never multiplicative).
_MAX_TAG_EVIDENCE_PER_TITLE = 3

# Evidence half-life: weight halves every DECAY_HALF_LIFE_DAYS, floored so
# very old evidence fades but never disappears.
_DECAY_HALF_LIFE_DAYS = 365.0
_DECAY_FLOOR = 0.1

# Evidence observations contributing at least this magnitude count toward
# signal_count (tiny residual weights from negative ratings still count as
# observations of taste).
_MIN_SIGNAL_MAGNITUDE = 0.01

# Confidence: signal_count / 30, gated on breadth of evidence.
_CONFIDENCE_SIGNAL_DIVISOR = 30.0
_CONFIDENCE_MIN_DISTINCT_TITLES = 5

# JSON float precision: enough for stable ranking, no fp noise.
_ROUND_DIGITS = 4

# Timezone-naive "now" — matches the project's datetime.utcnow conventions.

# rating_quality anchors: piecewise-linear through (0.5,-1.0), (2.5,+0.4),
# (5.0,+1.0). Lower segment slope 0.7/star, upper segment 0.24/star.
_Q_LO_X, _Q_LO_Y = 0.5, -1.0
_Q_MID_X, _Q_MID_Y = 2.5, 0.4
_Q_LO_SLOPE = (_Q_MID_Y - _Q_LO_Y) / (_Q_MID_X - _Q_LO_X)  # 0.7
_Q_HI_SLOPE = (1.0 - _Q_MID_Y) / (5.0 - _Q_MID_X)          # 0.24


# ══════════════════════════════════════════════════════════════════════════
# Pure helpers (individually tested)
# ══════════════════════════════════════════════════════════════════════════

def rating_quality(rating):
    """Map a 0.5..5.0 star rating to [-1.0, +1.0] evidence polarity.

    Audited anchors, piecewise-linear between them and clamped outside:
        5.0 → +1.0   strong love
        2.5 → +0.4   mild positive
        0.5 → -1.0   strong dislike
    The steeper lower segment keeps negative evidence prominent: it crosses
    zero at ≈1.93, so ratings below ~2 stars contribute negatively while
    mid-scale ratings count as mild positive evidence. None/unknown → 0.0.
    """
    if rating is None:
        return 0.0
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return 0.0
    if r >= _Q_MID_X:
        q = _Q_MID_Y + (r - _Q_MID_X) * _Q_HI_SLOPE
    else:
        q = _Q_LO_Y + (r - _Q_LO_X) * _Q_LO_SLOPE
    return round(max(-1.0, min(1.0, q)), _ROUND_DIGITS)


def recency_decay(event_time, now):
    """Exponential recency weight: 0.5^(age_days/365), floored at 0.1.

    - today            → 1.0
    - one year old     → 0.5
    - very old         → approaches but never drops below 0.1
    - future timestamps are clamped to age 0 (decay ≤ 1.0, never > 1)
    Accepts datetime or date; returns a rounded float.
    """
    if event_time is None or now is None:
        return 1.0
    if isinstance(event_time, date) and not isinstance(event_time, datetime):
        event_dt = datetime(event_time.year, event_time.month, event_time.day)
    elif isinstance(event_time, datetime):
        event_dt = event_time
    else:
        return 1.0
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime(now.year, now.month, now.day)

    age_days = (now - event_dt).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    weight = 0.5 ** (age_days / _DECAY_HALF_LIFE_DAYS)
    return round(max(_DECAY_FLOOR, weight), _ROUND_DIGITS)


def normalize_l2(weights):
    """L2-normalize {key: weight}, preserving signs and zero-input keys.

    Negative evidence stays negative after normalization; an all-zero (or
    empty) map normalizes to {} so cold-start profiles stay neutral.
    """
    norm = math.sqrt(sum(w * w for w in weights.values()))
    if norm <= 0:
        return {}
    return {k: round(v / norm, _ROUND_DIGITS) for k, v in weights.items()}


def decade_from_release_date(release_date):
    """'1987-06-04' / date(1987,6,4) → '1980s'; None for missing/malformed."""
    if release_date is None:
        return None
    year = None
    try:
        if isinstance(release_date, datetime):
            year = release_date.year
        elif isinstance(release_date, date):
            year = release_date.year
        else:
            year = int(str(release_date)[:4])
    except (TypeError, ValueError):
        return None
    if year is None or year < 1888 or year > datetime.utcnow().year + 1:
        return None  # malformed or implausible (incl. far-future)
    return f"{(year // 10) * 10}s"


def weighted_percentile(values, weights, percentile):
    """Deterministic weighted percentile over (value, weight) pairs.

    Sorts by value, walks the cumulative weight, and returns the first
    value where the cumulative share reaches `percentile` (0..1). Equal
    values aggregate stably; weights ≤ 0 are ignored. None when no valid
    samples exist.
    """
    if not values or not weights:
        return None
    if len(values) != len(weights):
        return None
    pairs = [(v, w) for v, w in zip(values, weights) if w > 0]
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p[0], p[1]))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative / total >= percentile:
            return value
    return pairs[-1][0]


def _confidence(signal_count, distinct_title_count):
    """min(1, signal_count/30), requiring ≥5 distinct titles for >0."""
    if distinct_title_count < _CONFIDENCE_MIN_DISTINCT_TITLES:
        return 0.0
    return round(min(1.0, signal_count / _CONFIDENCE_SIGNAL_DIVISOR),
                 _ROUND_DIGITS)


def feedback_event_weight(event):
    """Pure: RecommendationFeedback event → signed base taste weight.

    click +0.25 · saved +0.35 · rated +1.0 · not_interested −0.75 ·
    impression 0.0 · already_watched 0.0. Zero-weight events are exposure
    or bookkeeping, never preference (see FEEDBACK_EVENT_WEIGHTS). Unknown
    event names → 0.0 (fail-safe: never invent taste evidence)."""
    return FEEDBACK_EVENT_WEIGHTS.get(event, 0.0)


# ══════════════════════════════════════════════════════════════════════════
# Evidence collection — one event, one signal, one accumulator contribution
# ══════════════════════════════════════════════════════════════════════════

def _title_key(media_type, media_id):
    return (media_type, media_id)


class _Accumulator:
    """Weighted evidence accumulator for one user's computation.

    genre/decade accumulate signed weighted sums; media_type tracks movie
    vs tv weight; runtime collects (value, weight) samples for percentiles.
    """

    def __init__(self):
        self.genre = defaultdict(float)
        self.decade = defaultdict(float)
        self.director = defaultdict(float)
        self.media_type = defaultdict(float)
        self.runtime_values = []
        self.runtime_weights = []
        self.signal_count = 0
        self.titles = set()

    def add(self, evidence, weight, title_key, signal_name,
            genre=None, decade=None, media_type=None, runtime=None,
            directors=None):
        """Record one evidence observation.

        evidence: signed quality factor (positive/negative/neutral)
        weight:   signal weight × recency decay
        """
        magnitude = abs(evidence * weight)
        if magnitude < _MIN_SIGNAL_MAGNITUDE:
            return
        self.signal_count += 1
        if title_key is not None:
            self.titles.add(title_key)

        contribution = evidence * weight
        if genre:
            for g in genre:
                self.genre[g] += contribution
        if decade:
            self.decade[decade] += contribution
        if directors:
            for d in directors:
                self.director[d] += contribution
        if media_type:
            self.media_type[media_type] += abs(contribution)
        if runtime is not None:
            self.runtime_values.append(runtime)
            self.runtime_weights.append(abs(contribution))


def _media_meta_map(media_type, ids, by='tmdb_id'):
    """One batched MediaItem lookup → {(media_type, source_id): item}.

    Identity spaces in FrameIQ differ by source table (verified in models/):
      review.media_id / diary_entry.media_id / user_watchlist.media_id
          → FK to media_item.id          (by='pk')
      media_like.media_id / user_media_tag.media_id /
      tv_episode_watch.show_id
          → TMDb ids                     (by='tmdb_id')

    The returned map is keyed by the SOURCE row's identity space so callers
    can look up with the raw row id; the canonical cross-source title key
    (tmdb-based) is produced by _lookup() from the resolved item.
    """
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    id_col = MediaItem.id if by == 'pk' else MediaItem.tmdb_id
    items = MediaItem.query.filter(
        id_col.in_(ids),
        MediaItem.media_type == media_type,
    ).all()
    if by == 'pk':
        return {_title_key(media_type, m.id): m for m in items}
    return {_title_key(media_type, m.tmdb_id): m for m in items}


def _lookup(meta, media_type, raw_id):
    """Resolve one source row → (media_item, canonical_title_key).

    The canonical key uses tmdb_id when the MediaItem resolved (unifying
    evidence for the same title across different identity spaces), and
    falls back to the raw row id for orphaned rows (still counted as a
    distinct title, never silently dropped).
    """
    item = meta.get(_title_key(media_type, raw_id))
    key = _title_key(media_type,
                     item.tmdb_id if item is not None else raw_id)
    return item, key


def _genres_list(media_item):
    """MediaItem.genres is a comma-separated label string ('Drama, Crime')."""
    if not media_item or not media_item.genres:
        return []
    return [g.strip() for g in str(media_item.genres).split(',') if g.strip()]


def _director_names_map(items):
    """Batched local director lookup: {MediaItem.id: [name, ...]}.

    Phase 10: persisted Director/MediaDirector evidence (models/director.py,
    populated by the offline scripts/enrich_directors.py batch — NEVER at
    request time). One join over exactly the resolved MediaItems; names are
    sorted for deterministic aggregation. Empty map when a MediaItem has no
    persisted director evidence (enrichment not run, or 'enriched and
    empty') — callers simply skip the director dimension.
    """
    ids = {m.id for m in items if m is not None}
    if not ids:
        return {}
    from models.director import Director, MediaDirector
    rows = (
        MediaDirector.query
        .filter(MediaDirector.media_item_id.in_(ids))
        .join(MediaDirector.director)
        .with_entities(MediaDirector.media_item_id, Director.name)
        .all()
    )
    names = defaultdict(list)
    for media_item_id, name in rows:
        names[media_item_id].append(name)
    for media_item_id in names:
        names[media_item_id].sort()
    return dict(names)


def _collect_review_ratings(acc, user_id, now):
    """review_rating signal (1.0) — explicit, signed, strongest per-title."""
    reviews = (
        Review.query.filter_by(user_id=user_id)
        .order_by(Review.created_at.desc()).limit(200).all()
    )
    if not reviews:
        return
    meta = _media_meta_map(
        'movie', {r.media_id for r in reviews if r.media_type == 'movie'},
        by='pk')
    meta.update(_media_meta_map(
        'tv', {r.media_id for r in reviews if r.media_type == 'tv'},
        by='pk'))
    directors_map = _director_names_map(meta.values())
    for r in reviews:
        item, key = _lookup(meta, r.media_type, r.media_id)
        event_time = r.created_at or (r.watched_date if r.watched_date else now)
        weight = W_REVIEW_RATING * recency_decay(event_time, now)
        acc.add(
            evidence=rating_quality(r.rating),
            weight=weight,
            title_key=key,
            signal_name='review_rating',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type=r.media_type,
            runtime=item.runtime if item else None,
            directors=directors_map.get(item.id) if item else None,
        )


def _collect_diary_entries(acc, user_id, now):
    """diary_rating (0.9) + diary_rewatch (0.7, only when is_rewatch).

    One diary event contributes its rating once (never also as a like or
    watchlist signal); a rewatch adds a separate bounded viewing signal.
    """
    entries = (
        DiaryEntry.query.filter_by(user_id=user_id)
        .order_by(DiaryEntry.watched_date.desc()).limit(200).all()
    )
    if not entries:
        return
    meta = _media_meta_map(
        'movie', {e.media_id for e in entries if e.media_type == 'movie'},
        by='pk')
    meta.update(_media_meta_map(
        'tv', {e.media_id for e in entries if e.media_type == 'tv'},
        by='pk'))
    directors_map = _director_names_map(meta.values())

    for e in entries:
        item, key = _lookup(meta, e.media_type, e.media_id)
        event_time = e.watched_date or e.created_at or now

        if e.rating is not None:
            weight = W_DIARY_RATING * recency_decay(event_time, now)
            acc.add(
                evidence=rating_quality(e.rating),
                weight=weight,
                title_key=key,
                signal_name='diary_rating',
                genre=_genres_list(item),
                decade=decade_from_release_date(item.release_date) if item else None,
                media_type=e.media_type,
                runtime=item.runtime if item else None,
                directors=directors_map.get(item.id) if item else None,
            )

        if e.is_rewatch:
            weight = W_DIARY_REWATCH * recency_decay(event_time, now)
            acc.add(
                evidence=1.0,  # choosing to rewatch is positive behavior
                weight=weight,
                title_key=key,
                signal_name='diary_rewatch',
                genre=_genres_list(item),
                decade=decade_from_release_date(item.release_date) if item else None,
                media_type=e.media_type,
                directors=directors_map.get(item.id) if item else None,
            )


def _collect_media_likes(acc, user_id, now):
    """media_like (0.8) — explicit appreciation without a rating.

    media_id here is a TMDb id (models/social.py), so metadata is joined on
    MediaItem.tmdb_id — the same identity used by the tag/like system.
    """
    likes = (
        MediaLike.query.filter_by(user_id=user_id)
        .order_by(MediaLike.created_at.desc()).limit(200).all()
    )
    if not likes:
        return
    meta = _media_meta_map(
        'movie', {lk.media_id for lk in likes if lk.media_type == 'movie'})
    meta.update(_media_meta_map(
        'tv', {lk.media_id for lk in likes if lk.media_type == 'tv'}))
    directors_map = _director_names_map(meta.values())

    for lk in likes:
        item, key = _lookup(meta, lk.media_type, lk.media_id)
        weight = W_MEDIA_LIKE * recency_decay(lk.created_at or now, now)
        acc.add(
            evidence=1.0,
            weight=weight,
            title_key=key,
            signal_name='media_like',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type=lk.media_type,
            runtime=item.runtime if item else None,
            directors=directors_map.get(item.id) if item else None,
        )


def _collect_tags(acc, user_id, now):
    """tag (0.3) — weak signal, capped per title (≤3 tag evidences/title)."""
    tags = (
        UserMediaTag.query.filter_by(user_id=user_id)
        .order_by(UserMediaTag.created_at.desc()).limit(300).all()
    )
    if not tags:
        return
    meta = _media_meta_map(
        'movie', {t.media_id for t in tags if t.media_type == 'movie'})
    meta.update(_media_meta_map(
        'tv', {t.media_id for t in tags if t.media_type == 'tv'}))
    directors_map = _director_names_map(meta.values())

    # Resolve tag names in ONE query instead of per-row lazy loads.
    from models import Tag
    tag_ids = {t.tag_id for t in tags}
    tag_names = {}
    if tag_ids:
        for tag in Tag.query.filter(Tag.id.in_(tag_ids)).all():
            tag_names[tag.id] = tag.name

    per_title = defaultdict(int)
    for t in tags:
        key = _title_key(t.media_type, t.media_id)
        if per_title[key] >= _MAX_TAG_EVIDENCE_PER_TITLE:
            continue
        per_title[key] += 1
        item, key = _lookup(meta, t.media_type, t.media_id)
        weight = W_TAG * recency_decay(t.created_at or now, now)
        acc.add(
            evidence=1.0,  # tagging is a neutral-positive act of curation
            weight=weight,
            title_key=key,
            signal_name='tag',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type=t.media_type,
            directors=directors_map.get(item.id) if item else None,
        )


def _collect_watchlist(acc, user_id, now):
    """watchlist_add (0.2) — weak intent, capped titles, no date semantics.

    Junction rows have date_added but no rating; the presence of the intent
    is the signal. Deliberately does NOT feed runtime (no runtime evidence
    exists for intent-only rows without extra assumptions).
    """
    rows = db.session.execute(
        user_watchlist.select()
        .where(user_watchlist.c.user_id == user_id)
        .limit(_WATCHLIST_CAP)
    ).all()
    if not rows:
        return
    meta = _media_meta_map(
        'movie', {r.media_id for r in rows if r.media_type == 'movie'},
        by='pk')
    meta.update(_media_meta_map(
        'tv', {r.media_id for r in rows if r.media_type == 'tv'},
        by='pk'))
    directors_map = _director_names_map(meta.values())

    for r in rows:
        item, key = _lookup(meta, r.media_type, r.media_id)
        acc.add(
            evidence=1.0,
            weight=W_WATCHLIST_ADD,
            title_key=key,
            signal_name='watchlist_add',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type=r.media_type,
            directors=directors_map.get(item.id) if item else None,
        )


def _collect_feedback_events(acc, user_id, now):
    """Recommendation feedback (Feature #7 Phase 3) — weak/medium signals.

    RecommendationFeedback.media_id is a TMDb id (models/
    recommendation_feedback.py): metadata is resolved ONLY via batched
    MediaItem(tmdb_id, media_type) lookups — never MediaItem.id, never an
    external call. Events referencing titles with no local MediaItem still
    contribute their generic signal/count (documented V1 rule); dimensions
    needing metadata (genre/decade/runtime) are skipped for those.

    Weights come from feedback_event_weight(); impression and
    already_watched weigh 0.0 and therefore contribute NO signal and NO
    distinct title (the accumulator's _MIN_SIGNAL_MAGNITUDE gate handles
    this). Recency uses RecommendationFeedback.created_at through the
    canonical decay. Surface and source are deliberately NOT weighted.

    NOTE on `rated`: the feedback row is only an event record — its payload
    is NEVER treated as an authoritative star rating (no fabricated stars
    from client data). It contributes its generic +1.0 weight here; when
    the user also has a persisted rating for the same title, that explicit
    review/diary/episode rating contributes separately through its own
    (dominant) signal.

    `not_interested` produces SIGNED NEGATIVE genre/decade evidence (the
    accumulator preserves signs end-to-end; L2 normalization never discards
    them). It is pure evidence — it mutates no watchlist/wishlist/diary/
    viewed/like state.
    """
    from models.recommendation_feedback import RecommendationFeedback

    events = (
        RecommendationFeedback.query.filter_by(user_id=user_id)
        .order_by(RecommendationFeedback.created_at.desc())
        .limit(_FEEDBACK_ROW_LIMIT)
        .all()
    )
    if not events:
        return
    meta = _media_meta_map(
        'movie', {e.media_id for e in events if e.media_type == 'movie'})
    meta.update(_media_meta_map(
        'tv', {e.media_id for e in events if e.media_type == 'tv'}))
    directors_map = _director_names_map(meta.values())

    for e in events:
        item, key = _lookup(meta, e.media_type, e.media_id)
        weight = feedback_event_weight(e.event) \
            * recency_decay(e.created_at or now, now)
        acc.add(
            evidence=1.0,  # polarity lives in the weight's sign
            weight=weight,
            title_key=key,
            signal_name=f'feedback_{e.event}',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type=e.media_type,
            runtime=item.runtime if item else None,
            directors=directors_map.get(item.id) if item else None,
        )


def _collect_episode_ratings(acc, user_id, now):
    """episode_rating (1.0) — sparse but strong; mapped to the PARENT SHOW.

    TVEpisodeWatch.show_id is a TMDb show id; metadata comes from
    MediaItem(tmdb_id=show_id, media_type='tv'). Episode rows are never
    treated as movie ids and never trigger external lookups.
    """
    episodes = (
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.rating.isnot(None),
        )
        .order_by(TVEpisodeWatch.watched_date.desc()).limit(200).all()
    )
    if not episodes:
        return
    meta = _media_meta_map('tv', {e.show_id for e in episodes})
    # TV director evidence is deliberately absent today (no reliable
    # series-level director source — see models/director.py), so this map
    # is empty for shows; wiring is future-proof if TV capture ever lands.
    directors_map = _director_names_map(meta.values())

    for e in episodes:
        item, key = _lookup(meta, 'tv', e.show_id)
        event_time = e.watched_date or e.created_at or now
        weight = W_EPISODE_RATING * recency_decay(event_time, now)
        acc.add(
            evidence=rating_quality(e.rating),
            weight=weight,
            title_key=key,
            signal_name='episode_rating',
            genre=_genres_list(item),
            decade=decade_from_release_date(item.release_date) if item else None,
            media_type='tv',
            runtime=item.runtime if item else None,
            directors=directors_map.get(item.id) if item else None,
        )


def _runtime_pref(acc):
    """Weighted p25/p75 over locally persisted MediaItem.runtime samples."""
    if not acc.runtime_values:
        return None
    p25 = weighted_percentile(acc.runtime_values, acc.runtime_weights, 0.25)
    p75 = weighted_percentile(acc.runtime_values, acc.runtime_weights, 0.75)
    return {
        'p25': p25,
        'p75': p75,
        'sample_count': len(acc.runtime_values),
    }


def _director_affinity(acc):
    """Top-8 directors by the SAME signed weighted evidence as genre/decade.

    Phase 10: directors come from persisted Director/MediaDirector rows
    (models/director.py — populated only by the offline enrichment batch).
    No external calls, no separate scoring: every already-supported signal
    (ratings, diary, likes, tags, watchlist intent, feedback) flows through
    _Accumulator.add(directors=...) with its existing weight, recency decay
    and sign — negative evidence (e.g. not_interested on a Villeneuve film)
    is preserved. Output: L2-normalized like the other dimensions, keys
    sorted for deterministic JSON. signal_count / distinct_title_count are
    NOT affected: the director dimension reuses the same evidence events,
    it is not new evidence.
    """
    if not acc.director:
        return {}
    normalized = normalize_l2(dict(acc.director))
    ranked = sorted(normalized.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ranked[:_DIRECTOR_TOP_N])


# ══════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════

PROFILE_VERSION = 1  # bump when the computation algorithm changes


def compute_profile(user_id, now=None):
    """Compute and persist the user's TasteProfile. Idempotent.

    Bounded reads: ≤8 targeted queries (reviews, diary, likes, tags,
    watchlist rows, episode ratings, recent recommendation feedback, plus
    batched MediaItem metadata lookups) — no full-table scans, no N+1, no
    external calls.

    Returns the TasteProfile model instance (created or updated).
    """
    if now is None:
        now = datetime.utcnow()
    elif isinstance(now, date) and not isinstance(now, datetime):
        now = datetime(now.year, now.month, now.day)

    user_exists = db.session.get(User, user_id) if hasattr(db.session, 'get') \
        else User.query.get(user_id)
    if user_exists is None:
        raise ValueError(f"cannot compute taste profile: user {user_id} not found")

    acc = _Accumulator()
    _collect_review_ratings(acc, user_id, now)
    _collect_diary_entries(acc, user_id, now)
    _collect_media_likes(acc, user_id, now)
    _collect_tags(acc, user_id, now)
    _collect_watchlist(acc, user_id, now)
    _collect_episode_ratings(acc, user_id, now)
    _collect_feedback_events(acc, user_id, now)

    profile = TasteProfile.query.filter_by(user_id=user_id).first()
    if profile is None:
        profile = TasteProfile(user_id=user_id)
        db.session.add(profile)

    # Deterministic JSON documents (sorted keys) for stable round-trips.
    profile.genre_weights_json = db_json(normalize_l2(dict(acc.genre)))
    profile.decade_weights_json = db_json(normalize_l2(dict(acc.decade)))
    profile.director_affinity_json = db_json(_director_affinity(acc))
    profile.media_type_pref_json = db_json(
        _media_type_pref(acc))
    runtime = _runtime_pref(acc)
    profile.runtime_pref_json = db_json(runtime) if runtime else '{}'
    profile.mood_tags_json = None  # deferred dimension

    profile.confidence = _confidence(acc.signal_count, len(acc.titles))
    profile.signal_count = acc.signal_count
    profile.distinct_title_count = len(acc.titles)
    profile.profile_version = PROFILE_VERSION
    profile.updated_at = datetime.utcnow()

    db.session.commit()
    logger.info(
        "Taste profile computed user=%s signals=%s titles=%s confidence=%s",
        user_id, acc.signal_count, len(acc.titles), profile.confidence)
    return profile


def _media_type_pref(acc):
    """Normalize {movie, tv} weight to a share-of-total representation.

    Uses absolute (unsigned) weights — an interaction says 'this type
    interests me' regardless of rating polarity. Falls back to {} when a
    user has no typed evidence (never fabricates a preference).
    """
    weights = {k: round(v, _ROUND_DIGITS) for k, v in acc.media_type.items()
               if v > 0}
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {k: round(v / total, _ROUND_DIGITS) for k, v in weights.items()}


def db_json(value):
    """Serialize to the model's JSON-document text convention (sorted)."""
    import json
    return json.dumps(value if value is not None else {}, sort_keys=True)


def get_profile(user_id, create=False):
    """Return the user's persisted TasteProfile (None unless create=True)."""
    profile = TasteProfile.query.filter_by(user_id=user_id).first()
    if profile is None and create:
        profile = compute_profile(user_id)
    return profile


def describe_profile(profile):
    """Pure formatter: structured summary for future explanation layers.

    No DB access, no LLM, deterministic. Consumes a TasteProfile (model or
    to_dict()) and returns plain dicts/lists ready for ranking explanations
    or LLM context building elsewhere.
    """
    data = profile.to_dict() if hasattr(profile, 'to_dict') else dict(profile)

    genre_weights = _as_float_map(data.get('genre_weights') or {})
    ranked = sorted(genre_weights.items(), key=lambda kv: (-kv[1], kv[0]))
    top_positive = [{'genre': g, 'weight': w} for g, w in ranked if w > 0][:5]
    top_negative = [{'genre': g, 'weight': w} for g, w in reversed(ranked)
                    if w < 0][:3]

    decade_weights = _as_float_map(data.get('decade_weights') or {})
    top_decades = [
        {'decade': d, 'weight': w}
        for d, w in sorted(decade_weights.items(),
                           key=lambda kv: (-kv[1], kv[0]))[:3] if w > 0
    ]

    media_type_pref = _as_float_map(data.get('media_type_pref') or {})
    runtime_pref = data.get('runtime_pref') or {}

    return {
        'top_positive_genres': top_positive,
        'top_negative_genres': top_negative,
        'top_decades': top_decades,
        'media_type_pref': media_type_pref,
        'runtime_pref': {
            'p25': runtime_pref.get('p25'),
            'p75': runtime_pref.get('p75'),
            'sample_count': runtime_pref.get('sample_count', 0),
        },
        'director_affinity': _as_float_map(
            data.get('director_affinity') or {}),
        'confidence': data.get('confidence') or 0.0,
        'signal_count': data.get('signal_count') or 0,
        'distinct_title_count': data.get('distinct_title_count') or 0,
        'profile_version': data.get('profile_version'),
        'is_personalized': (
            (data.get('confidence') or 0) >= 0.2
            and bool(top_positive)
        ),
    }


def _as_float_map(raw):
    """Best-effort {str: float} coercion for formatter input."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


# ══════════════════════════════════════════════════════════════════════════
# Smart Lists taste matching (Feature #6, Phase 12) — pure helpers.
#
# The canonical TasteProfile is the ONLY taste source for the Smart Lists
# 'matches_my_taste' filter (api/smart_lists.py). These functions perform
# no DB work and no network work: they take an already-loaded profile and
# a plain candidate dict and produce a deterministic, inspectable score.
# V1 deliberately uses a zero-for-missing model — an unavailable dimension
# contributes 0 rather than being treated as negative evidence — and a
# fixed threshold (no dynamic re-normalization, no percentile cutoffs).
# ══════════════════════════════════════════════════════════════════════════

# Fixed V1 match threshold (not user-configurable, not dynamically tuned).
TASTE_MATCH_THRESHOLD = 0.45

# Component weights of taste_match_score(). Weights sum to 1.0; a missing
# dimension contributes 0 for its component (the score is NOT re-normalized
# over available dimensions).
TASTE_MATCH_WEIGHTS = {
    'genre': 0.50,
    'director': 0.20,
    'decade': 0.10,
    'media_type': 0.10,
    'runtime': 0.10,
}


def genre_match_score(profile_genres, candidate_genres):
    """Mean of the profile's signed weights over the candidate's genres.

    Bounded aggregation choice: the MEAN of the candidate's distinct genre
    weights (not the sum) so a title is never rewarded purely for having
    many genres. Duplicates are de-duplicated case-insensitively (the first
    spelling wins, matching the persisted profile's canonical labels).
    Missing/empty either side → 0.0.
    """
    if not isinstance(profile_genres, dict) or not candidate_genres:
        return 0.0
    seen = set()
    picked = []
    for genre in candidate_genres:
        if not isinstance(genre, str):
            continue
        label = genre.strip()
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        try:
            picked.append(float(profile_genres.get(label, 0.0)))
        except (TypeError, ValueError):
            picked.append(0.0)
    if not picked:
        return 0.0
    return round(sum(picked) / len(picked), _ROUND_DIGITS)


def director_match_score(profile_directors, candidate_directors):
    """Best positive director affinity among the candidate's directors.

    The candidate's persisted director names (batched lookup upstream) are
    matched against the L2-normalized top-8 profile affinity. Missing
    evidence on either side contributes 0 — never negative evidence.
    """
    if not isinstance(profile_directors, dict) or not candidate_directors:
        return 0.0
    best = 0.0
    for name in candidate_directors:
        if not isinstance(name, str):
            continue
        try:
            weight = float(profile_directors.get(name, 0.0))
        except (TypeError, ValueError):
            continue
        if weight > best:
            best = weight
    return round(best, _ROUND_DIGITS)


def decade_match_score(profile_decades, candidate_decade):
    """Signed profile weight for the candidate's decade label.

    candidate_decade uses the canonical '1980s' convention — reuse
    decade_from_release_date() upstream; no second parser lives here.
    Missing candidate decade or empty profile map → 0.0.
    """
    if not isinstance(profile_decades, dict) or not candidate_decade:
        return 0.0
    try:
        return round(float(profile_decades.get(candidate_decade, 0.0)),
                     _ROUND_DIGITS)
    except (TypeError, ValueError):
        return 0.0


def media_type_match_score(profile_media_types, candidate_media_type):
    """Positive share of the user's typed evidence spent on this type.

    profile_media_types is the share-of-total map ({'movie': 0.6, 'tv': 0.4}):
    a preferred type scores its share, the other type scores 0 (never
    negative — the taste matcher is a score, not a hard type gate).
    """
    if not isinstance(profile_media_types, dict) or not candidate_media_type:
        return 0.0
    try:
        share = float(profile_media_types.get(candidate_media_type, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return round(share if share > 0 else 0.0, _ROUND_DIGITS)


def runtime_match_score(runtime_pref, candidate_runtime):
    """1.0 inside the learned [p25, p75] interval, tapering outside.

    The taper is half the interquartile width on each side (deterministic,
    zero-config): score = 1 - distance/half_width, so halfway through the
    taper scores 0.5 and at (or beyond) the full taper distance the score
    reaches 0. Missing interval, missing runtime, or a degenerate
    (zero-width) interval → 0.0.
    """
    if not isinstance(runtime_pref, dict):
        return 0.0
    try:
        if candidate_runtime is None:
            return 0.0
        p25 = float(runtime_pref.get('p25'))
        p75 = float(runtime_pref.get('p75'))
    except (TypeError, ValueError):
        return 0.0
    if p75 <= p25:  # degenerate interval (insufficient variance)
        return 0.0
    try:
        runtime = float(candidate_runtime)
    except (TypeError, ValueError):
        return 0.0
    if p25 <= runtime <= p75:
        return 1.0
    half = (p75 - p25) / 2.0
    distance = p25 - runtime if runtime < p25 else runtime - p75
    return round(max(0.0, 1.0 - distance / half), _ROUND_DIGITS)


def taste_match_score(profile, candidate=None):
    """Deterministic 0..1 taste-match score for one candidate title.

        score = 0.50 * genre_match
              + 0.20 * director_match
              + 0.10 * decade_match
              + 0.10 * media_type_match
              + 0.10 * runtime_match

    Components (genre_match_score etc., same module) combine the persisted,
    signed profile dimensions with the candidate's local metadata. A missing
    dimension contributes 0 (zero-for-missing model — missing evidence is
    NOT negative evidence). Canonical form:

        taste_match_score(profile, candidate)

    where `profile` may be a TasteProfile, its to_dict(), or a precomputed
    taste_match_inputs() snapshot (so hot loops parse the profile JSON
    once). The single-argument form taste_match_score(profile) evaluates
    that dict ITSELF as the candidate (REPL/test convenience).
    Candidates are plain dicts with optional keys: genres, directors,
    decade, media_type, runtime.
    """
    if candidate is None:
        candidate = profile
        profile = taste_match_inputs(profile)
    inputs = (profile if isinstance(profile, dict)
              and 'genre_weights_json' not in profile
              else taste_match_inputs(profile))
    genres = candidate.get('genres') or []
    return round(
        TASTE_MATCH_WEIGHTS['genre'] * genre_match_score(
            inputs['genre_weights'], genres)
        + TASTE_MATCH_WEIGHTS['director'] * director_match_score(
            inputs['director_affinity'], candidate.get('directors'))
        + TASTE_MATCH_WEIGHTS['decade'] * decade_match_score(
            inputs['decade_weights'], candidate.get('decade'))
        + TASTE_MATCH_WEIGHTS['media_type'] * media_type_match_score(
            inputs['media_type_pref'], candidate.get('media_type'))
        + TASTE_MATCH_WEIGHTS['runtime'] * runtime_match_score(
            inputs['runtime_pref'], candidate.get('runtime')),
        _ROUND_DIGITS)


def taste_match_inputs(profile):
    """Snapshot a TasteProfile (model or dict) into plain match inputs.

    One dict per profile load; consumed by taste_match_score() so repeated
    candidate scoring parses no JSON repeatedly.
    """
    data = profile.to_dict() if hasattr(profile, 'to_dict') else dict(profile)
    runtime = data.get('runtime_pref')
    if not isinstance(runtime, dict):
        runtime = {}
    return {
        'genre_weights': data.get('genre_weights')
        if isinstance(data.get('genre_weights'), dict) else {},
        'director_affinity': data.get('director_affinity')
        if isinstance(data.get('director_affinity'), dict) else {},
        'decade_weights': data.get('decade_weights')
        if isinstance(data.get('decade_weights'), dict) else {},
        'media_type_pref': data.get('media_type_pref')
        if isinstance(data.get('media_type_pref'), dict) else {},
        'runtime_pref': runtime,
        'confidence': float(data.get('confidence') or 0.0),
        'distinct_title_count': int(data.get('distinct_title_count') or 0),
    }


def taste_profile_eligible(profile):
    """Canonical cold-start gate shared with For You: full personalization
    requires >= 5 distinct titles and confidence >= 0.4. Reuses For You's
    audited constants via a local import to avoid a circular dependency.
    """
    from api.for_you import (FULL_PERSONALIZED_MIN_TITLES,
                             FULL_PERSONALIZED_MIN_CONFIDENCE)
    data = profile.to_dict() if hasattr(profile, 'to_dict') else dict(profile)
    try:
        titles = int(data.get('distinct_title_count') or 0)
        confidence = float(data.get('confidence') or 0.0)
    except (TypeError, ValueError):
        return False
    return (titles >= FULL_PERSONALIZED_MIN_TITLES
            and confidence >= FULL_PERSONALIZED_MIN_CONFIDENCE)


# ══════════════════════════════════════════════════════════════════════════════
# Taste DNA presentation model (Feature #6, Phase 14)
#
# Converts the persisted profile into a human-readable presentation object
# for GET /api/taste-profile and the profile-page Taste DNA section. Pure
# formatting: one profile load, no computation, no network, no raw weights —
# dimensions become bounded strength labels ("high"/"moderate"/"low") or
# compact prose. Negative evidence is calibrated ("steer away from"), never
# absolute ("hates"/"never watches").
# ══════════════════════════════════════════════════════════════════════════════

# Presentation bounds (spec §18).
DNA_TOP_GENRES = 8
DNA_AVOID_GENRES = 5
DNA_TOP_DIRECTORS = 5
DNA_TOP_ERAS = 5

# Canonical thresholds reused for the confidence level (spec §6): For You's
# audited personalized gate (titles >= 5, confidence >= 0.4) is the
# "strong" bar; below it the profile is still developing. Centralized here,
# not in the route.
DNA_STRONG_MIN_CONFIDENCE = 0.4   # == For You FULL_PERSONALIZED_MIN_CONFIDENCE
DNA_STRONG_MIN_TITLES = 5         # == For You FULL_PERSONALIZED_MIN_TITLES
DNA_DEVELOPING_MIN_CONFIDENCE = 0.2

DNA_STRENGTHS = ('high', 'moderate', 'low')


def _strength_from_weight(weight):
    """Map a normalized signed weight to a bounded strength label (pure)."""
    if weight >= 0.6:
        return 'high'
    if weight >= 0.3:
        return 'moderate'
    return 'low'


def _strength_from_share(share):
    """Map a 0..1 media-type share to a bounded strength label (pure)."""
    if share >= 0.65:
        return 'high'
    if share >= 0.35:
        return 'moderate'
    return 'low'


def taste_dna(profile):
    """Presentation model for the authenticated user's own TasteProfile.

    Consumes a TasteProfile (model or to_dict()) — never recomputes, never
    touches RecommendationFeedback, never calls TMDb. Returns a compact
    JSON-safe dict with human-readable labels; no raw floats, no negative
    numbers, no IDs, no profile_version.

    Shape (sections with no evidence are simply absent):
        {
          "available": true,
          "level": "strong" | "developing" | "limited",
          "confidence": 0.73,
          "titles_analyzed": 42,
          "top_genres":      [{"name": ..., "strength": high|moderate|low}],
          "avoid_genres":    [{"name": ..., "strength": high|moderate|low}],
          "top_directors":   [{"name": ..., "strength": ...}],
          "eras":            [{"name": "2010s", "strength": ...}],
          "media_preference": {"movie": ..., "tv": ...},
          "runtime": {"min": 105, "max": 145},
        }
    """
    if profile is None:
        return None
    data = profile.to_dict() if hasattr(profile, 'to_dict') else dict(profile)

    genres = _as_float_map(data.get('genre_weights') or {})
    if not genres:
        return None  # nothing meaningful to present (cold/empty profile)

    ranked = sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))
    top_genres = [
        {'name': name, 'strength': _strength_from_weight(w)}
        for name, w in ranked if w > 0
    ][:DNA_TOP_GENRES]
    if not top_genres:
        # No positive evidence at all (cold/empty, or negative-only):
        # nothing meaningful to present — same semantics as the CineBot
        # taste formatter. Callers present the neutral cold-start state.
        return None
    # Steer-away list: weakest (most negative) first. Magnitudes only —
    # the API never emits negative numbers; wording stays calibrated.
    avoid_genres = [
        {'name': name, 'strength': _strength_from_weight(abs(w))}
        for name, w in reversed(ranked) if w < 0
    ][:DNA_AVOID_GENRES]

    directors = _as_float_map(data.get('director_affinity') or {})
    top_directors = [
        {'name': name, 'strength': _strength_from_weight(w)}
        for name, w in sorted(directors.items(), key=lambda kv: (-kv[1], kv[0]))
        if w > 0
    ][:DNA_TOP_DIRECTORS]

    decades = _as_float_map(data.get('decade_weights') or {})
    eras = [
        {'name': name, 'strength': _strength_from_weight(w)}
        for name, w in sorted(decades.items(), key=lambda kv: (-kv[1], kv[0]))
        if w > 0
    ][:DNA_TOP_ERAS]

    media_pref = _as_float_map(data.get('media_type_pref') or {})
    media_preference = {
        name: _strength_from_share(share)
        for name, share in sorted(media_pref.items(),
                                  key=lambda kv: (-kv[1], kv[0]))
    } or None

    runtime_pref = data.get('runtime_pref') or {}
    p25, p75 = runtime_pref.get('p25'), runtime_pref.get('p75')
    runtime = None
    if p25 and p75 and p75 > p25:
        runtime = {'min': int(round(p25)), 'max': int(round(p75))}

    confidence = float(data.get('confidence') or 0.0)
    titles = int(data.get('distinct_title_count') or 0)
    if titles >= DNA_STRONG_MIN_TITLES \
            and confidence >= DNA_STRONG_MIN_CONFIDENCE:
        level = 'strong'
    elif confidence >= DNA_DEVELOPING_MIN_CONFIDENCE:
        level = 'developing'
    else:
        level = 'limited'

    response = {
        'available': True,
        'level': level,
        'confidence': round(confidence, 2),
        'titles_analyzed': titles,
        'top_genres': top_genres,
    }
    # Sections with no evidence are omitted entirely (compact, spec §18).
    for key, value in (
            ('avoid_genres', avoid_genres),
            ('top_directors', top_directors),
            ('eras', eras),
            ('media_preference', media_preference),
            ('runtime', runtime)):
        if value:
            response[key] = value
    return response
