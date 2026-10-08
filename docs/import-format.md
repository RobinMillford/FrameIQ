# FrameIQ Import Formats

Ingest of an existing watch history from another service. The counterpart to
[`docs/export-format.md`](export-format.md).

- **Sources:** Letterboxd, TV Time (GDPR export)
- **Added by:** Task F6
- **Endpoints:** `POST /api/account/import/<source>/preview`, `.../apply`

Import is read-then-write and **fully local**: no external service is contacted
while resolving, and nothing is fetched from TMDb to fill a gap.

## Contents

1. [Design](#1-design)
2. [Supported sources](#2-supported-sources)
3. [Normalized records](#3-normalized-records)
4. [Resolution](#4-resolution)
5. [Idempotency and duplicates](#5-idempotency-and-duplicates)
6. [Precedence against existing data](#6-precedence-against-existing-data)
7. [Date, rating and rewatch handling](#7-date-rating-and-rewatch-handling)
8. [TV episode eligibility](#8-tv-episode-eligibility)
9. [Preview and result](#9-preview-and-result)
10. [Supported and unsupported fields](#10-supported-and-unsupported-fields)
11. [Security](#11-security)
12. [Privacy](#12-privacy)
13. [Transaction semantics](#13-transaction-semantics)
14. [Performance](#14-performance)
15. [Future compatibility](#15-future-compatibility)

---

## 1. Design

    upload bytes
        -> uploads    size / extension / container sniff / archive safety
        -> sources    normalized records;  no DB, no resolution
        -> resolve    local MediaItem;    no writes
        -> preview    classify only;      WRITES NOTHING
        -> apply      canonical write paths; one transaction

Each layer is independently testable and strictly one-directional. A source
adapter never imports from FrameIQ's domain layer, and only `writer.py` may
write — always through the same functions the UI and API use.

**Import is not export.** F5's `frameiq-export` JSON remains the authoritative
representation of a FrameIQ account; Letterboxd and TV Time never round-trip
through it.

## 2. Supported sources

| Source | Accepted files | Produces |
|---|---|---|
| `letterboxd` | `.zip` (Letterboxd data export) | movie records |
| `tvtime` | `.json`, or `.zip` containing the JSON | episode records (+ movies if the export has them) |

### Letterboxd

Reads **only `watched.csv`**, deliberately:

- `diary.csv` in a real export overlaps `watched.csv`. Reading both would
  double-count every film.
- `ratings.csv` holds a rating without a watch event, and `watchlist.csv` holds
  a non-event. Neither is watch history.
- A row with no `Letterboxd URI` has no stable identity and is reported
  invalid, because it could not be de-duplicated on re-import.

Letterboxd's format is film-centric, so this adapter produces **movie records
only** and never fabricates TV seasons or episodes.

### TV Time

A GDPR export JSON, optionally inside a ZIP (common for GDPR downloads).
Shows without a name or without any external id are reported invalid.

An episode with `is_specials: true`, or with no `last_watched`, is **not**
history and is skipped or reported — never turned into a watch event.

## 3. Normalized records

Source field names stop at the parser boundary. Two dataclasses
(`api/imports/records.py`):

`MovieImportRecord`
: `source`, `source_key`, `title`, `release_year`, `external_id`, `watched_at`,
  `rating`, `review_text`, `is_rewatch`, `source_metadata`

`TVEpisodeImportRecord`
: `source`, `source_key`, `show_title`, `show_external_id`, `season_number`,
  `episode_number`, `episode_title`, `watched_at`, `rating`, `is_rewatch`,
  `source_metadata`

`source_key` is the record's deterministic identity, e.g.
`('letterboxd', 'watched', 'the-matrix')` or `('tvtime', '600100001', '1', '3')`.
It is preserved for future importers.

## 4. Resolution

Strictly ordered, and **local-only** — `MediaIndex` is preloaded once from
`MediaItem` for the whole import:

0. **a mapping the user saved themselves** (F7), or the choice they just made
   in the resolve panel. Both outrank inference: the user compared candidates
   and picked one, which is information no heuristic has;
1. a stable external id the record already carries (TV Time supplies a TMDb id,
   so this resolves exactly, with no title matching at all);
2. a local title match that is **unique** — normalised title, and release year
   when the record has one.

There is deliberately **no third "best fuzzy match" rung**. If several local
titles match, the record is `ambiguous`; if none do, it is `unresolved`. Neither
is imported, and neither triggers a network call.

Title normalisation (`resolve.normalize_title`) is NFKD + casefold + Unicode
punctuation folding. The character class is `\w` under Unicode semantics, **not**
`[a-z0-9]`: an ASCII-only class silently deletes Bengali, Arabic, Japanese and
Cyrillic characters, normalising those titles to the empty string so they can
never resolve. Accented Latin legitimately folds to ASCII (`Amélie` →
`amelie`), which is how a user's unaccented spelling still matches.

> **How to resolve an `unresolved` record (F7):** the preview lists local
> candidates for the row and nothing is pre-selected. Pick one, or leave it out.
> If no candidate looks right, **Search FrameIQ** searches local titles only.
> There is still no "fetch from TMDb and guess" path.

## 5. Idempotency and duplicates

Both writers are keyed on **domain identity**, not on remembering which source
rows were seen:

| Domain | Identity | Model |
|---|---|---|
| movie watch | `(user, media, watched_date)` | `DiaryEntry` |
| episode watch | `(user, show, season, episode)` | `TVEpisodeWatch` |

This needs no bookkeeping table, survives re-importing the same file, and cannot
drift from the data it describes. Re-importing an identical export reports every
record as `already_present` and creates nothing.

A film genuinely watched twice on **different dates** keeps both events — those
are two real watch events, not duplicates. Within one file, two rows with the
same `(title, date)` collapse to one.

A rewatch is derived from existing history, not trusted from the source:
`is_rewatch = (prior watch events for this user and media) > 0`, the same rule
`routes/diary.py` uses.

> `ratings.csv` from F5 remains a **projection** and is never an import source.
> Ratings are read from the owning domain row.

## 6. Precedence against existing data

Import never overwrites newer user data:

- a movie event that already exists is reported `already_present`, and its
  stored rating/notes are left alone — an older source never clobbers a rating
  the user just added;
- a TV episode that already exists is never rewritten; metadata keys are only
  forwarded when the source actually has a value, so an absent rating cannot
  clear a stored one.

## 7. Date, rating and rewatch handling

| Field | Rule |
|---|---|
| Date | `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, or ISO-8601 with `Z`. Parsed to a **date**; a row with no usable date is `unsupported`, not "today". |
| Movie rating | Letterboxd 0–5 → FrameIQ 0.5–5. A source `0` means *no rating* and becomes `null`, because `0` would look deliberate. |
| TV Time rating | 0–10 → 0.5–5 stars. |
| TV episode rating | TV Time has none, so it is `None`. Nothing is invented. |
| Rewatch | Derived from existing history (§5). |

## 8. TV episode eligibility

Every imported episode goes through
`routes.tv_tracking.mark_episode_watched_core` — the same function the UI and
API use. Import **never** inserts a `TVEpisodeWatch` row directly, because that
would bypass F4's guarantees. So an imported episode gets:

- the same eligibility gate as a UI click (`episode_eligibility`), so a
  **future** episode and an **ineligible special** cannot become watch history;
- canonical counter sync and completion gating;
- metadata-preservation semantics (absent keys leave stored values alone).

Consequently imported TV state is indistinguishable from manually watched TV
state: the canonical progress map, the `Viewed` verdict, next-episode
calculation, Continue Watching and unmark all behave the same. Tests assert
this directly by reaching the same state both ways and comparing.

> When a show has **no verifiable aired metadata at all**, F4's gate is
> deliberately permissive so degraded shows stay trackable. That is an existing
> F4 decision and import inherits it; it is not a special case for import.

## 9. Preview and result

Two-stage. `preview` issues **no** INSERT/UPDATE/DELETE — a test asserts row
counts are unchanged across it.

| Category | Meaning |
|---|---|
| `imported` | will be / was written |
| `already_present` | the user already has this exact event |
| `unresolved` | no local match; open the title once, then re-run |
| `ambiguous` | several local matches; never auto-picked |
| `ineligible` | F4's gate refused it (unaired / ineligible special) |
| `invalid` | the source row was malformed |
| `unsupported` | the row is well-formed but FrameIQ will not invent the missing field |

Response samples are capped at 100 rows per category and 50 failures, so a
10,000-record import cannot produce an unbounded body or an unbounded page.

## 10. Supported and unsupported fields

**Imported:** movie title, release year, watch date, movie rating, show identity,
season, episode number, episode title, and (F7) the Letterboxd review body.

**Intentionally not imported:**

| Field | Reason |
|---|---|
| Genres, directors, runtime, popularity, streaming availability | FrameIQ caches these from TMDb; the import must not fabricate or duplicate TMDb cache rows. |
| Letterboxd watchlist / favourites / ratings-only rows | Not watch history. |
| Letterboxd diary body | `diary.csv` overlaps `watched.csv`; reading both would double-count every film. `reviews.csv` is safe because it is read for the review body only (§16). |
| Letterboxd review **titles**, tags, spoilers flag | `reviews.csv` carries only a body; the other fields live in files FrameIQ does not read. |
| TV Time per-episode rating | The source does not have one. |
| `user_viewed`, `TVShowProgress` counters | Derived; F4/F5 treat them as non-canonical. |
| Notification preferences | **No such model exists** — F5 documents the omission and F6 does not invent one. |
| Chat, quota, agent memory | Out of scope; F5 deliberately excludes assistant text and quota state. |

## 11. Security

Uploads are untrusted. Every gate runs **before** parsing:

- authenticated; strictly the session user, with no `user_id` parameter or path
  segment;
- POST-only, and CSRF-protected by the app's existing `CSRFProtect`;
- rate-limited through the shared `extensions.limiter`
  (`"5 per minute; 20 per hour"`, an existing repo policy);
- size-bounded: the body is capped by `MAX_CONTENT_LENGTH`, re-checked in the
  route, and again inside `check_size`;
- extension allow-list **per source** — an upload whose extension is not listed
  is refused, not "best effort";
- **container sniffed**, not trusted: a `.json` that is not JSON, and a `.zip`
  that is not a ZIP, are both refused;
- archives are read **in memory**; members are validated individually, rejecting
  `..`, absolute paths, Windows drive/UNC paths, backslash separators, NUL
  bytes and dotfiles. Nothing is ever extracted to a filesystem path, so
  zip-slip is not reachable even in principle;
- a zip bomb is refused via declared total and per-member uncompressed size;
- member count is bounded;
- **no** `pickle`, `subprocess`, `shutil`, `marshal`, dynamic import, `eval`,
  `exec`, `yaml.load` or shell execution — enforced by an AST-based test so the
  package's own prose cannot satisfy it;
- uploaded bytes are never written to disk;
- errors are 4xx with a safe message and never leak SQL, tracebacks or paths.

## 12. Privacy

Strictly current-user scoped. The user is `current_user`, full stop. A two-user
test snapshots user B's diary, episodes, lists and reviews before A's import and
compares afterwards, requiring them unchanged.

## 13. Transaction semantics

Two deliberately different failure classes:

- **Per-row failure** — one unwritable title is recorded in `failures` and the
  import still commits the good rows. Discarding 999 valid watches because of
  one bad row is worse than reporting one failure.
- **Systemic failure** — anything reaching commit rolls the whole transaction
  back, so a half-imported account is not possible.

`summary_payload` is always accurate about what actually happened.

## 14. Performance

No per-row lookups and no network:

- `MediaIndex` preloads all local media once per run;
- existing-watch checks are one batched query per domain, not one per row;
- TV Time episodes resolve on their TMDb id, bypassing title matching entirely.

A test asserts the statement count does not grow materially when the fixture
grows 100×, and a 1,000-row import is verified for correct counts, no
duplicates, and a clean re-import. TV import consults only the offline show
payload (the F3 registry), never the network.

## 15. Future compatibility

- Normalized records keep stable identifiers, watched dates, season/episode
  coordinates and rewatch information, so a future importer can consume them
  without re-parsing.
- `source_key` is preserved per record.
- No generic import/export versioning framework is introduced here; F5's
  `version` field remains the single format-version mechanism, and it is not
  used by these source adapters.
- CSV column layouts are documented per source in the adapter docstrings, not
  versioned as a public contract.

---

# Task F7 additions

F7 changes three things F6 could not do: it remembers a user's answer, it lets
the user answer a question the importer refuses to guess at, and it imports
Letterboxd review text.

## 16. Letterboxd reviews

`reviews.csv` is read **for the review body only**.

Letterboxd documents its `Review` column as *"Text/HTML … accepts the same set
of HTML tags as on the Letterboxd website"*, so a real export carries markup.

**Reduction to plain text.** FrameIQ renders review bodies as auto-escaped text,
so storing the markup verbatim would show the user literal `<p>` in their own
review. `<p>`/`<br>`-style tags become line breaks, other tags are dropped,
`<script>`/`<style>` *content* is discarded, entities are decoded, and no-break
spaces plus the zero-width family (U+200B/200C/200D/2060/FEFF) are removed so
two copies of the same review compare equal.

**No double counting.** A Letterboxd review is per *film*, not per watch. A row
is therefore never emitted as extra watch history:

| Case | Result |
|---|---|
| Film in `watched.csv`, reviewed once | Body attaches to the **first** watch; a rewatch stays a plain watch. |
| Film watched 3×, one review | One body, on the first watch. Two extra events remain. |
| Review with a whitespace-only body | Skipped silently — nothing to carry, and no phantom record. |
| Review whose film is missing from `watched.csv` | Becomes its own record: a review still asserts the film was watched. |
| Review row with no usable URI | Reported `invalid`; never invented. |
| `reviews.csv` with no `Review` column | Reported `invalid` — not silently treated as "no reviews". |
| `reviews.csv` present but no `watched.csv` | Whole import refused; watch history cannot come from reviews alone. |

**Per-watch URIs.** `diary.csv` uses `…/film/slug/2` for the second watch.
`_letterboxd_film_slug` drops that trailing ordinal, so every watch of one film
collapses to a single identity.

## 17. Source mappings

A mapping is the user's durable answer to "this external title is that FrameIQ
title". F6 refused to guess, so its only recovery was *open the title once, then
re-run* — repeated forever, for every file.

### Precedence

    saved mapping  >  explicit in-panel choice  >  source external id
                  >  unique title match        >  candidates  >  unresolved

A saved mapping outranks a fresh choice. A user who saved `Stalker → 1979` and
then ticks `2002` in the panel for one row still gets 1979, because silently
re-resolving behind a saved mapping is exactly the surprise this feature
exists to remove. Changing a mapping is an explicit edit or delete.

### Source identity (the mapping key)

The key is the **resolution identity**, not the record identity:

| Record | `source_key` | Mapping key |
|---|---|---|
| Movie | `(letterboxd, watched, 29qU)` | `(letterboxd, movie, 29qU)` |
| Movie from `reviews.csv` | `(letterboxd, review, stlk)` | `(letterboxd, movie, stlk)` |
| TV episode | `(tvtime, 600100001, 1, 4)` | `(tvtime, tv, 600100001)` |

The show collapses to **one** mapping, so mapping a show resolves all of its
episodes — bulk resolution is a consequence of the key, not a separate feature.
A `watched.csv` row and its `reviews.csv` counterpart share a key.

TV Time's show identity comes from `tmdb_id` (`tvdb_id` is the fallback), **not**
the bare export `id`; a show with neither is reported unidentifiable.

### Persistence

Storing a heuristic match as a permanent override is a decision, not a side
effect, so `save_mappings` is **opt-in** and defaults to off. When on, one
mapping is written per resolved identity — once per show, not once per episode.

### Conflict and staleness

| Situation | Behaviour |
|---|---|
| Same identity saved again | Updated, not duplicated. |
| Target `MediaItem` deleted | Listed with `stale: true` and a reason. A mapping the user cannot see is one they can never clean up. |
| Target `media_type` changed | Also `stale`. |
| Delete | Removes import bookkeeping **only**. Diary entries, reviews and watched episodes are untouched. |
| Another user's mapping id | `DELETE` matches nothing → 400. Scoped in the `WHERE` clause, not by a prior lookup. |

### Why there is no server-side import session

Resolve needs to carry choices from preview to apply. The browser already holds
the `File`, and choices are a few KB of JSON, so apply re-uploads the same file
with the choices attached. Re-parsing at apply is what lets the server check
every submitted key against the records actually in the file. No upload is
persisted, and a preview cannot be replayed against a different file.

## 18. Explicit resolution

`unresolved` and `ambiguous` rows carry `resolution_key` and a bounded
candidate list (`candidates`, ≤ 6 in the preview; more via search).

**Candidates are local and deterministic** — ranked year-agreeing, then
year-differing, then substring matches; read from the preloaded `MediaIndex` so
they issue **no queries**. The same file always yields the same list in the same
order.

**Nothing is auto-selected**, and there is no auto-skip either: an undecided row
is `unresolved` until the user chooses or explicitly skips it.

**Submissions are validated three ways** before they may influence a write,
because this is the one path where client data decides which of the user's
titles an event lands on:

1. the key must be well-formed and belong to the URL's source (a client cannot
   mint a mapping into a namespace it does not own);
2. the identity must exist **in this file** — otherwise a client could map
   identities it invented and steer an unrelated row onto an unrelated title;
3. the target must be a real `MediaItem` whose `media_type` matches the key.

Any failure is a **400**, never a silent drop: dropping it would let apply
write fewer rows than the preview promised and still report success.

## 19. Review conflict semantics

`Review` is `UNIQUE(user_id, media_id, media_type)` — one review per film per
user — so a pre-existing review is a genuine **conflict**, not something to
merge. There is no honest automatic resolution: a FrameIQ review and a
Letterboxd review are two different pieces of writing.

**Existing user data always wins.** An import never edits or deletes a review
the user already has; it reports `conflict` and leaves both texts alone.

| Situation | Result |
|---|---|
| No existing review | Created from the imported body + rating. |
| Existing review | `conflict`; user's text, rating and date untouched. |
| Soft-deleted review | Not a conflict — a deleted review is not the user's current opinion, so the import restores it. |
| Another user's review | Irrelevant; reviews are per-user. |
| No rating anywhere | Not imported. `Review.rating` is `NOT NULL` and a star rating is an opinion, so none is invented. |

A review imports **independently** of the watch event: a review row with no date
produces no diary entry (`unsupported`) and still imports the body.

## 20. Resolution status counts

`apply` now reports `records_detected` as a denominator. F6 set `details` only
in `preview`, so every apply summary read "imported N" with no "of M".

