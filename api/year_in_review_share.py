"""Shareable Year-in-Review transformation + token utilities (Phase 9).

This layer consumes the canonical recap model — it is NOT a second
Year-in-Review engine:

    api.statistics.get_statistics()   (one call, inside the builder)
        ↓
    api.year_in_review.build_year_in_review()
        ↓
    build_public_year_in_review()     (this module: pure transformation)
        ↓
    public share page / card

Token design (§3/§15): the share URL carries an opaque token from
``secrets.token_urlsafe`` (256-bit entropy, cryptographically secure).
Nothing — no user ID, no year, no statistics — is encoded into it. Only
the SHA-256 hash of the token is persisted; the raw token exists exactly
once, in the creation response, and is never logged.

Public model (§8/§9/§29): only explicitly approved fields are exposed.
The private recap's rating distribution, monthly/daily series, people
blocks (actors), and rating counts are deliberately omitted from the
public surface. Directors and season ratings pass through as bounded,
neutral top slices (§9 "selected ... if appropriate", §52 card density):
the canonical ordering is reused verbatim — no re-ranking happens here.
Empty recap models are rejected — a public share never points at a
nonexistent story (§30).
"""
import hashlib
import secrets

# §52: the public card stays compact — fixed bounded slices of the
# canonical people/season orderings (never the unbounded private lists).
PUBLIC_DIRECTORS_LIMIT = 3
PUBLIC_SEASON_LIMIT = 3

__all__ = [
    "PUBLIC_DIRECTORS_LIMIT",
    "PUBLIC_SEASON_LIMIT",
    "build_public_year_in_review",
    "generate_share_token",
    "hash_share_token",
]


def generate_share_token():
    """256-bit cryptographically random opaque URL-safe token (§3)."""
    return secrets.token_urlsafe(32)


def hash_share_token(token):
    """SHA-256 hex digest of a token — the only persisted representation.

    Raises ValueError for non-string/empty input (malformed public
    tokens are rejected before any database lookup happens).
    """
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token must be a non-empty string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_public_year_in_review(recap):
    """Pure transformation: canonical recap → approved public model.

    Deterministic: identical recap → identical public dict, no
    timestamps, no randomness, no re-selection of highlights (the
    canonical highlight facts pass through as-is, trimmed to their
    user-facing-safe fields). Raises ValueError for empty/unavailable
    recap models (§30) — share creation and the public route both fail
    closed instead of exposing an empty or fabricated story.
    """
    if not isinstance(recap, dict) or not recap.get("available") \
            or recap.get("state") != "ready":
        raise ValueError("year in review is not shareable")

    highlights = recap.get("highlights") or {}
    summary = recap.get("summary") or {}
    public = {
        "year": recap["year"],
        "summary": {
            "total_watch_events": summary.get("total_watch_events"),
            "distinct_titles": summary.get("distinct_titles"),
            "total_hours_watched": summary.get("total_hours_watched"),
            "average_rating": summary.get("average_rating"),
        },
        "highlights": {},
    }

    top_genre = highlights.get("top_genre")
    if top_genre:
        public["highlights"]["top_genre"] = {"name": top_genre["name"]}
    busiest = highlights.get("busiest_month")
    if busiest:
        public["highlights"]["busiest_month"] = {"text": busiest["text"]}
    media_split = highlights.get("media_split")
    if media_split:
        public["highlights"]["media_split"] = {
            "description": media_split["description"]}
    rewatches = highlights.get("rewatches")
    if rewatches:
        # Canonical sentence included verbatim so no presentation layer
        # ever recomputes the percentage (§21-style canonical wording).
        public["highlights"]["rewatches"] = {
            "count": rewatches["count"],
            "rate": rewatches["rate"],
            "text": rewatches["text"],
        }

    genres = recap.get("genres") or []
    if genres:
        public["genres"] = [{"name": genre["name"], "count": genre["count"]}
                            for genre in genres]

    # §52: keep the public card compact — bounded top slices of the
    # canonical people/season orderings (no re-ranking, no new engine).
    directors = ((recap.get("people") or {}).get("directors") or [])
    if directors:
        public["directors"] = [
            {"name": person["name"],
             "watch_event_count": person["watch_event_count"],
             "distinct_title_count": person["distinct_title_count"]}
            for person in directors[:PUBLIC_DIRECTORS_LIMIT]
        ]
    seasons = recap.get("season_quality") or []
    if seasons:
        public["season_quality"] = [
            {"show_name": season["show_name"],
             "season_number": season["season_number"],
             "average_rating": season["average_rating"],
             "rating_count": season["rating_count"]}
            for season in seasons[:PUBLIC_SEASON_LIMIT]
        ]

    runtime = recap.get("runtime")
    if runtime:
        # Canonical wording preserved verbatim ("At least X hours" when
        # coverage is incomplete) — never recomputed client- or
        # server-side elsewhere.
        public["runtime"] = {
            "hours": runtime["hours"],
            "complete": runtime["complete"],
            "text": runtime["text"],
        }
    return public
