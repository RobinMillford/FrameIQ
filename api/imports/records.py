"""Normalized import records (Task F6).

Every source adapter parses into these types, and only these types. Letterboxd
and TV Time field names therefore stop at the parser boundary: nothing
downstream — the resolver, the writers, the preview report, the API — knows
which service a record came from, only what it means.

Two records, because the two domains have genuinely different shapes:

  ``MovieImportRecord``      a watched movie event
  ``TVEpisodeImportRecord``  a watched episode event

Both are immutable dataclasses with an explicit ``source_key``: a
deterministic identity for "this exact record from this exact source file".
That is what makes repeated imports idempotent without a bookkeeping table —
see ``docs/import-format.md``.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class MovieImportRecord:
    """One watched-movie event from an external source.

    ``release_year`` and ``external_id`` are optional because sources differ:
    Letterboxd's ``watched.csv`` carries a year but no TMDb id, while TV Time
    carries a TMDb id. Both being optional is what forces the resolver to have
    a real resolution ladder rather than assuming an id is always present.
    """

    source: str
    source_key: Tuple[str, str, str]
    title: Optional[str] = None
    release_year: Optional[int] = None
    external_id: Optional[str] = None
    watched_at: Optional[date] = None
    rating: Optional[float] = None
    review_text: Optional[str] = None
    is_rewatch: Optional[bool] = None
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.title and not self.external_id:
            # Nothing resolvable: the adapter should have routed this to an
            # invalid row, but fail loudly here rather than deeper down.
            raise ValueError('movie record needs a title or an external id')


@dataclass(frozen=True)
class TVEpisodeImportRecord:
    """One watched-episode event from an external source.

    ``rating`` and ``rewatch`` are only populated when the source genuinely
    expresses them. TV Time has no per-episode rating; Letterboxd has no TV
    episodes at all. Fabricating either would be inventing data.
    """

    source: str
    source_key: Tuple[str, str, int, int]
    show_title: Optional[str] = None
    show_external_id: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    episode_title: Optional[str] = None
    watched_at: Optional[date] = None
    rating: Optional[float] = None
    is_rewatch: Optional[bool] = None
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.season_number is None or self.episode_number is None:
            raise ValueError('TV episode record needs season and episode')


@dataclass
class InvalidRow:
    """A source row that could not be turned into a normalized record.

    Kept (rather than dropped) so the preview can tell the user "3 rows were
    malformed" instead of silently losing data. ``position`` is a 1-based row
    number in the source file so a mismatch can actually be found.
    """

    source: str
    position: int
    reason: str
    excerpt: Optional[str] = None