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

> **How to resolve an `unresolved` record:** open that title or show once in
> FrameIQ, then re-run the import. It then matches on the local cache. There is
> no "fetch from TMDb and guess" path.

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
season, episode number, episode title.

**Intentionally not imported:**

| Field | Reason |
|---|---|
| Genres, directors, runtime, popularity, streaming availability | FrameIQ caches these from TMDb; the import must not fabricate or duplicate TMDb cache rows. |
| Letterboxd watchlist / favourites / ratings-only rows | Not watch history. |
| Letterboxd review text | Not present in `watched.csv`; the adapter does not reach into unrelated files. |
| Letterboxd diary body | Same reason. |
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