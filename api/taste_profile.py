"""Canonical taste profile computation (Feature #6, Phase 2).

The SINGLE source of truth for turning a user's first-party behavioral
data into the persisted, explainable TasteProfile (models/taste_profile.py):

    local persisted signals (reviews, diary, likes, tags, watchlist,
                             episode ratings)
        ↓  compute_profile(user_id)
    weighted evidence (quality factor × signal weight × recency decay)
        ↓  per-dimension aggregation + L2 normalization
    persisted TasteProfile row
        ↓  (later phases: For You rail, CineBot context, Smart List filters)

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
        self.media_type = defaultdict(float)
        self.runtime_values = []
        self.runtime_weights = []
        self.signal_count = 0
        self.titles = set()

    def add(self, evidence, weight, title_key, signal_name,
            genre=None, decade=None, media_type=None, runtime=None):
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
    """Phase 2: always empty — see module docstring.

    Directors exist only as request-time TMDb credits, never as persisted
    per-user evidence. Populating this requires the director-capture phase;
    doing it here would need external calls or a speculative new table,
    both forbidden for this service.
    """
    return {}


# ══════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════

PROFILE_VERSION = 1  # bump when the computation algorithm changes


def compute_profile(user_id, now=None):
    """Compute and persist the user's TasteProfile. Idempotent.

    Bounded reads: ≤7 targeted queries (reviews, diary, likes, tags,
    watchlist rows, episode ratings, batched MediaItem metadata lookups)
    — no full-table scans, no N+1, no external calls.

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
