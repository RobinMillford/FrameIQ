"""Year-in-review / Wrapped foundation (Feature #8, Phase 4).

THE canonical backend story model for a user's Year in Review — a pure,
deterministic transformation over the canonical statistics service:

    DiaryEntry → api.statistics.get_statistics()   (one call, canonical)
                        ↓
    api.year_in_review.build_year_in_review()      (pure transformation)
                        ↓
    story presentation model (JSON-ready dict)

This module is NOT a second statistics engine. It never queries
DiaryEntry, never re-aggregates watch history, never touches TMDb or
any external service, performs no writes, and exposes no IDs. Every
number in the output comes verbatim from one get_statistics() call.

Contract:
  build_year_in_review(user_id, year)
    - YEAR-ONLY (a Year in Review is always a calendar year; no
      lifetime support here). Validation is delegated to the canonical
      statistics validation (calendar_year_bounds → ValueError).
    - available=False + state="empty" when the user logged nothing in
      the year — no story is fabricated for empty data (§5).
    - Deterministic: identical statistics → byte-identical JSON output.
      No randomness, no current-time reads, no unordered sets.

Highlight rules (all derived from canonical fields only):
  TOP GENRE      first entry of the canonical top_genres ordering.
                 Omitted when there are no genres — never invented.
  BUSIEST MONTH  the monthly_watch_counts entry with the highest count.
                 TIE RULE: earliest month wins (the canonical series is
                 January → December and the scan keeps the first strict
                 maximum). Zero-count months are never highlighted.
  RATINGS        canonical average_rating + rating_count only. No
                 "best rated movie" is claimed — the statistics service
                 deliberately exposes no title-level ranking.
  REWATCHES      explicitly marked rewatch events (canonical semantics;
                 never inferred from duplicate titles).
  MEDIA SPLIT    deterministic mapping of media_type_distribution:
                 movie share ≥ 0.75 → "mostly movies",
                 TV share   ≥ 0.75 → "mostly TV",
                 otherwise           → "balanced".
                 Observed viewing behavior — NOT a TasteProfile
                 preference, and no recommendation signals are used.
  RUNTIME        canonical hours + coverage. complete = no missing
                 runtime events; when incomplete the story text says
                 "At least X hours" — exact-hours claims are reserved
                 for complete coverage. Runtime is never invented and
                 no TMDb call fills gaps.

Story text uses bounded deterministic templates (§21) — no LLM, no
overstated conclusions: "Most watched genre", "Busiest month",
"Average rating". Words like "favorite" are deliberately avoided.

Privacy: output contains no user IDs, emails, usernames, database IDs,
review text, watchlist data, or feedback payloads. Only user-facing-safe
facts are included so a future share-card UI can consume the model
without exposing internals (no share URLs yet — §23/§24).

Phase 8 (presentation experience): the model gains three ADDITIVE
canonical passthrough fields for the private recap UI — people
(directors/actors), season_quality, and daily_activity — copied verbatim
from the same one get_statistics() call. No new computation, no renamed
fields, no IDs; sections the canonical data cannot support stay absent
or [] exactly as the canonical service reports them.
"""
from api.statistics import calendar_year_bounds, get_statistics

__all__ = [
    "MONTH_NAMES",
    "build_highlights",
    "build_rating_summary",
    "build_rewatch_summary",
    "build_runtime_summary",
    "build_year_in_review",
    "describe_media_split",
    "select_busiest_month",
    "select_top_genre",
]

# Canonical month names for deterministic story text (index = month-1).
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Media-split share threshold: the dominant type must hold at least
# this fraction of watch events to be called "mostly ..." (§12).
MOSTLY_SHARE = 0.75


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers — explicit inputs, no DB access, no network, deterministic
# ════════════════════════════════════════════════════════════════════════════

def _plural(count, word):
    """Deterministic 1-pluralization for story text."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _month_name(month_label):
    """'2026-08' → 'August' (canonical month label → name)."""
    return MONTH_NAMES[int(month_label.split("-")[1]) - 1]


def _month_year(month_label):
    """'2026-08' → '2026' (the label's own year — no current time)."""
    return month_label.split("-")[0]


def select_top_genre(top_genres):
    """Highlight the first valid genre of the canonical ordering (§8).

    Returns None when there are no genres — a genre is never invented.
    """
    for genre in top_genres or []:
        name = genre.get("name")
        if name:
            count = int(genre.get("count", 0))
            return {
                "name": name,
                "count": count,
                "text": (
                    f"Most watched genre: {name} "
                    f"({_plural(count, 'watch event')})."
                ),
            }
    return None


def select_busiest_month(monthly):
    """Highlight the month with the most watch events (§9).

    Tie rule: the canonical series is chronological (January →
    December) and the scan keeps the first strict maximum, so the
    EARLIEST month wins a tie. Zero-count months are never selected.
    Returns None when every month is zero.
    """
    best = None
    for row in monthly or []:
        count = int(row.get("count", 0))
        if count <= 0:
            continue
        if best is None or count > int(best["count"]):
            best = row
    if best is None:
        return None
    label = best["month"]
    count = int(best["count"])
    return {
        "month": label,
        "count": count,
        "text": (
            f"Your busiest month was {_month_name(label)} "
            f"{_month_year(label)} with "
            f"{_plural(count, 'watch event')}."
        ),
    }


def build_rating_summary(stats):
    """Ratings highlight from canonical average/count/distribution (§10).

    Returns None when nothing was rated — no fabricated average. No
    "best rated movie" claim exists anywhere: the statistics service
    exposes no title-level ranking by design.
    """
    average = stats.get("average_rating")
    count = int(stats.get("rating_count", 0))
    if average is None or count == 0:
        return None
    return {
        "average_rating": average,
        "count": count,
        "distribution": stats.get("rating_distribution"),
        "text": (
            f"Average rating: {average} across "
            f"{_plural(count, 'rating')}."
        ),
    }


def build_rewatch_summary(stats):
    """Rewatches highlight from canonical count/rate (§11)."""
    count = int(stats.get("rewatch_count", 0))
    rate = stats.get("rewatch_rate", 0.0)
    if count > 0:
        text = (
            f"Rewatched {_plural(count, 'time')} "
            f"({int(round(rate * 100))}% of watch events)."
        )
    else:
        text = "No rewatches this year."
    return {"count": count, "rate": rate, "text": text}


def describe_media_split(distribution):
    """Deterministic observed movie/TV description (§12).

    Canonical rule: movie share ≥ MOSTLY_SHARE → "mostly movies";
    TV share ≥ MOSTLY_SHARE → "mostly TV"; otherwise "balanced".
    Returns None when there are no events at all.
    """
    movies = int((distribution or {}).get("movie", 0))
    tv = int((distribution or {}).get("tv", 0))
    total = movies + tv
    if total == 0:
        return None
    if movies / total >= MOSTLY_SHARE:
        description = "mostly movies"
    elif tv / total >= MOSTLY_SHARE:
        description = "mostly TV"
    else:
        description = "balanced"
    return {
        "movie": movies,
        "tv": tv,
        "description": description,
        "text": (
            f"{description.capitalize()}: "
            f"{_plural(movies, 'movie')} and {_plural(tv, 'TV event')}."
        ),
    }


def build_runtime_summary(stats):
    """Runtime summary with explicit completeness (§15/§16).

    hours is the canonical total (missing-runtime events excluded);
    complete is False when any event lacked persisted runtime, and the
    story text then claims only "At least X hours" — exact-hours
    language is reserved for complete coverage.
    """
    hours = stats.get("total_hours_watched", 0.0)
    covered = int(stats.get("runtime_covered_events", 0))
    missing = int(stats.get("runtime_missing_events", 0))
    complete = missing == 0
    if complete:
        text = f"{hours} hours watched."
    else:
        text = (
            f"At least {hours} hours watched (runtime missing for "
            f"{_plural(missing, 'watch event')})."
        )
    return {
        "hours": hours,
        "covered_events": covered,
        "missing_events": missing,
        "complete": complete,
        "text": text,
    }


def build_highlights(stats):
    """Compose the deterministic highlight facts (§7).

    Unsupported facts are omitted (no top genre / no busiest month /
    no ratings) — nothing is invented from absent data.
    """
    highlights = {}
    top_genre = select_top_genre(stats.get("top_genres"))
    if top_genre is not None:
        highlights["top_genre"] = top_genre
    busiest = select_busiest_month(stats.get("monthly_watch_counts"))
    if busiest is not None:
        highlights["busiest_month"] = busiest
    ratings = build_rating_summary(stats)
    if ratings is not None:
        highlights["ratings"] = ratings
    highlights["rewatches"] = build_rewatch_summary(stats)
    media_split = describe_media_split(stats.get("media_type_distribution"))
    if media_split is not None:
        highlights["media_split"] = media_split
    return highlights


# ════════════════════════════════════════════════════════════════════════════
# Primary function
# ════════════════════════════════════════════════════════════════════════════

def build_year_in_review(user_id, year):
    """Build the year-in-review story model for one user and year.

    Exactly one get_statistics() call, zero additional DB queries,
    zero network requests, zero writes. Raises ValueError for invalid
    years (canonical statistics validation). Empty years return
    {"year", "available": False, "state": "empty"} — no fabricated
    story sections.
    """
    calendar_year_bounds(year)  # canonical validation (raises ValueError)
    stats = get_statistics(user_id, year=year)

    if not stats.get("total_watch_events"):
        return {"year": year, "available": False, "state": "empty"}

    return {
        "year": year,
        "available": True,
        "state": "ready",
        "summary": {
            "total_watch_events": stats["total_watch_events"],
            "distinct_titles": stats["distinct_titles"],
            "movies_watched": stats["movies_watched"],
            "tv_watch_events": stats["tv_watch_events"],
            "total_hours_watched": stats["total_hours_watched"],
            "average_rating": stats["average_rating"],
            "rating_count": stats["rating_count"],
            "rewatch_count": stats["rewatch_count"],
            "rewatch_rate": stats["rewatch_rate"],
        },
        "highlights": build_highlights(stats),
        "genres": stats["top_genres"],
        "ratings": {
            "average_rating": stats["average_rating"],
            "count": stats["rating_count"],
            "distribution": stats["rating_distribution"],
        },
        "monthly": stats["monthly_watch_counts"],
        "media_type": stats["media_type_distribution"],
        "rewatches": {
            "count": stats["rewatch_count"],
            "rate": stats["rewatch_rate"],
        },
        "runtime": build_runtime_summary(stats),
        # ── Phase 8 additive passthroughs (verbatim canonical values) ──
        "people": {
            "directors": stats["directors"],
            "actors": stats["actors"],  # unavailable → [] (§23)
        },
        "season_quality": stats["season_quality"],
        "daily_activity": stats["daily_activity"],
    }
