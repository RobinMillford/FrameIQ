# FrameIQ Export Format

Machine-readable account export produced by **Task F5**.

- **Format name:** `frameiq-export`
- **Version:** `1` (dedicated export-format integer — deliberately *not* the
  application version and *not* a git SHA, so it never changes meaning when
  the app is rebuilt)
- **Generated:** `docs/export-format.md`

## Contents

1. [Scope and privacy model](#1-scope-and-privacy-model)
2. [Audited model inventory](#2-audited-model-inventory)
3. [JSON contract](#3-json-contract)
4. [CSV contract](#4-csv-contract)
5. [ZIP bundle](#5-zip-bundle)
6. [Value conventions](#6-value-conventions)
7. [Canonical vs derived](#7-canonical-vs-derived)
8. [Omitted sensitive data](#8-omitted-sensitive-data)
9. [Relationship handling](#9-relationship-handling)
10. [Determinism](#10-determinism)
11. [Evolution rules](#11-evolution-rules)

---

## 1. Scope and privacy model

The export represents **the authenticated user's own portable FrameIQ data**.
It is not a database dump, and it is not a backup of infrastructure state.

Three rules govern every field:

1. **User-scoped.** Every query filters on the current session user. No
   export query may read another user's rows, and there is no `user_id`
   parameter, path segment, or admin override.
2. **Canonical-source based.** History is exported from the model that is
   the source of truth (see [§7](#7-canonical-vs-derived)). Derived counters
   and badges are never presented as historical truth.
3. **Privacy-safe.** Credentials, tokens, secrets, and other users' private
   data are excluded by construction — they are never selected into the
   serializer in the first place, rather than being filtered out afterwards.

The exporter reads only from the local database. It makes **no TMDb requests
and no outbound network calls at all** (see `docs` §13 of the task report;
asserted by `tests/test_account_export.py::test_export_makes_zero_tmdb_requests`).

---

## 2. Audited model inventory

Every model was inspected. This table is the audit record.

| Model / table | Ownership field | User data it holds | Exported | Canonical or derived | CSV domain | Contains another user's data | Contains secrets |
|---|---|---|---|---|---|---|---|
| `User` | `id` | account + profile | **yes** (allow-listed fields) | source | — | no | **yes — `password_hash` excluded** |
| `DiaryEntry` | `user_id` | movie/TV watch log, date, rating, rewatch, review link | **yes** | **canonical** | `movies.csv`, `ratings.csv` | no | no |
| `TVEpisodeWatch` | `user_id` | episode watch log, date, rating, notes, rewatch | **yes** | **canonical** | `tv_episodes.csv`, `ratings.csv` | no | no |
| `TVShowProgress` | `user_id` | show tracking state (`status`, `is_favorite`, dates) | **partly** | mixed — see [§7](#7-canonical-vs-derived) | `tv_progress.csv` | no | no |
| `user_watchlist` | `user_id` | watchlist membership, `date_added`, `priority` | **yes** | **canonical** | `watchlist.csv` | no | no |
| `user_viewed` | `user_id` | movie "viewed" mirror | **yes**, labelled `mirror` | **derived** mirror of `DiaryEntry` | — | no | no |
| `Review` | `user_id` | review title, body, rating, spoiler flag, dates | **yes** | **canonical** | `reviews.csv`, `ratings.csv` | no | no |
| `UserList` | `user_id` | list title, description, visibility, slug, `list_type` | **yes** | **canonical** | `lists.csv` | no | no |
| `UserListItem` | via `list_id` | item membership, `position`, `note`, `added_at` | **yes** (owner lists only) | **canonical** | `list_items.csv` | no | no |
| `ListCollaborator` | `user_id` | user's own membership/role | **yes** — role only | **canonical** (relationship) | `list_collaborators.csv` | **no — co-author identity excluded** | no |
| `UserListCategory` / `ListCategory` | via `list_id` | list categories | **yes** (name only) | **canonical** | `lists.csv` | no | no |
| `ListAnalytics` | via `list_id` | view/share/fork counters | **yes**, under `derived` | **derived** | — | no | no |
| `ListLike` | `user_id` | user's like on a list | **yes** — id + created_at | **canonical** (relationship) | `social.csv` | **no — list owner content excluded** | no |
| `ListComment` | `user_id` | user's own comment text | **yes** (authored) | **canonical** | `comments.csv` | **no** | no |
| `ListView` | `user_id` (nullable) | who viewed a list, when, from which IP | **no** | derived analytics | — | no | **no — contains `ip_address`** |
| `Tag` | none (global) | tag name | **yes**, via own associations | reference | `tags.csv` | no | no |
| `UserMediaTag` | `user_id` | user's tag on a title | **yes** | **canonical** | `tags.csv` | no | no |
| `MediaLike` | `user_id` | user's heart on a title | **yes** | **canonical** | `social.csv` | no | no |
| `MediaComment` | `user_id` | user's own comment text | **yes** (authored) | **canonical** | `comments.csv` | no | no |
| `ReviewLike` | `user_id` | user's like on a review | **yes** — id + created_at | **canonical** (relationship) | `social.csv` | **no — review author excluded** | no |
| `ReviewComment` | `user_id` | user's own comment text | **yes** (authored) | **canonical** | `comments.csv` | no | no |
| `ReviewHelpful` | `user_id` | helpful/not-helpful vote | **yes** | **canonical** (relationship) | `social.csv` | **no — review content excluded** | no |
| `UserFollow` | `follower_id` | who the user follows | **yes** — username + created_at | **canonical** (relationship) | `social.csv` | **no — no email/bio/profile** | no |
| `SmartList` | `user_id` | saved filter definition, scope, sort, visibility | **yes** | **canonical** (definition) | `smart_lists.csv` | no | no |
| `UserStreamingService` | `user_id` | provider id + region | **yes** | **canonical** | `streaming_services.csv` | no | no |
| `RecommendationFeedback` | `user_id` | append-only interaction events | **yes** | **canonical** (source events) | `recommendation_feedback.csv` | no | no |
| `TasteProfile` | `user_id` (1:1) | computed weights, confidence, counts | **yes**, under `derived` | **derived** | — | no | no |
| `WatchProgress` | `user_id` | in-progress playback position | **yes** | **canonical** (resume point) | `continue_watching.csv` | no | no |
| `ContinueWatchingItem` | `user_id` | continue-watching membership | **yes** | **canonical** | `continue_watching.csv` | no | no |
| `Notification` | `user_id` | notification records + read state | **partly** — see [§8](#8-omitted-sensitive-data) | read state is user-owned | `notifications.csv` | no | no |
| `ChatConversation` | `user_id` | conversation title | **yes** | **canonical** | `chat.csv` | no | no |
| `ChatMessage` | via `conversation_id` | message content, role, metadata | **partly** — content excluded | user-authored content | `chat.csv` | no | no |
| `UserChatMemory` | `user_id` | assistant-authored memory of the user | **no** | derived/agent state | — | no | no |
| `UserChatDailyUsage` | `user_id` | per-day question counter | **no** | derived quota accounting | — | no | no |
| `YearInReviewShare` | `user_id` | share authorization | **no** | access token record | — | no | **yes — `token_hash` excluded** |
| `MediaItem` | none (shared cache) | title, year, poster, genres | **yes**, as a *lookup only* | reference cache | inline in domain CSVs | no | no |
| `MovieReleaseDate`, `Director`, `MediaDirector` | none | release-date / director caches | **no** | pure TMDb cache | — | no | no |

### Excluded because the user asked for it

| Model / table | Reason |
|---|---|
| imports (Letterboxd, TV Time, Trakt, IMDb, generic CSV) | Out of scope for F5 by design |

---

## 3. JSON contract

`GET /api/account/export/json` returns the complete export.

```jsonc
{
  "format": "frameiq-export",
  "version": 1,
  "generated_at": "2026-10-08T17:04:11.482913Z",   // UTC, ISO-8601
  "account": {
    "user_id": 42,
    "username": "amin",
    "email": "amin@example.com",
    "email_verified": true,
    "date_joined": "2025-01-04T09:12:00Z",
    "streaming_region": "BD"
  },
  "profile": {
    "first_name": "Amin",
    "last_name": "Hasan",
    "bio": "🎬 Bengali: আমি সিনেমা দেখি",
    "profile_picture": "https://res.cloudinary.com/…/user_42_profile.jpg"
  },
  "watch_history": {
    "movies": [ /* DiaryEntry rows, media_type == 'movie' */ ],
    "tv": [ /* DiaryEntry rows, media_type == 'tv' */ ]
  },
  "tv_history": {
    "episodes": [ /* TVEpisodeWatch */ ],
    "progress": [ /* TVShowProgress — user-authored fields only */ ]
  },
  "watchlist": [ /* user_watchlist joined to MediaItem */ ],
  "lists": [ /* UserList + categories */ ],
  "list_items": [ /* UserListItem, owner lists only */ ],
  "list_collaborators": [ /* user's own membership */ ],
  "reviews": [ /* Review */ ],
  "ratings": [ /* denormalised rating ledger */ ],
  "tags": [ /* UserMediaTag joined to Tag */ ],
  "smart_lists": [ /* SmartList definitions */ ],
  "streaming_services": [ /* provider_id + region */ ],
  "recommendation_data": {
    "feedback": [ /* RecommendationFeedback */ ]
  },
  "social_data": {
    "following": [ /* UserFollow → public username */ ],
    "media_likes": [],
    "review_likes": [],
    "review_helpful_votes": [],
    "list_likes": []
  },
  "authored_content": {
    "media_comments": [],
    "review_comments": [],
    "list_comments": []
  },
  "notifications": {
    "preferences": {
      "note": "FrameIQ has no persisted notification preferences model; nothing to export."
    },
    "records": [ /* type + target + read state */ ]
  },
  "activity_state": {
    "continue_watching": [],
    "watch_progress": [],
    "chat_conversations": []
  },
  "viewed_mirror": {
    "note": "Derived mirror of diary entries; DiaryEntry is the canonical movie history. Present for import convenience only.",
    "movies": []
  },
  "derived": {
    "note": "Recomputed from canonical sources; never import these as source data.",
    "taste_profile": {},
    "list_analytics": []
  },
  "counts": {
    "diary_entries": 12,
    "tv_episode_watches": 38,
    "tv_progress": 3,
    "watchlist_items": 9,
    "lists": 4,
    "list_items": 61,
    "list_collaborators": 2,
    "reviews": 7,
    "ratings": 19,
    "tags": 14,
    "smart_lists": 2,
    "streaming_services": 3,
    "recommendation_feedback": 25,
    "following": 5,
    "media_likes": 4,
    "review_likes": 2,
    "review_helpful_votes": 1,
    "list_likes": 1,
    "media_comments": 2,
    "review_comments": 1,
    "list_comments": 1,
    "notifications": 20,
    "continue_watching": 2,
    "watch_progress": 1,
    "chat_conversations": 1,
    "viewed_mirror_movies": 11
  },
  "csv_files": ["movies.csv", "tv_episodes.csv", "..."]
}
```

### Section rules

- Every section is always present, even when empty (`[]` or `{}`). A missing
  key would make consumers guess; an empty list is unambiguous.
- `counts` is computed from the **exported payload**, not from separate
  `COUNT(*)` queries, so it can never disagree with the rows shipped.
- Domain row shapes are documented in [§4](#4-csv-contract); the JSON uses
  the same field names as the corresponding CSV, plus nested
  `media` objects where the CSV has flat `*_title` columns.

---

## 4. CSV contract

`GET /api/account/export/csv` returns `application/zip` containing the files
below plus `README.txt`.

**JSON is the complete export. CSV files are flattened, per-domain
projections of the same data** — they are for spreadsheets and migration
tooling, and never contain anything the JSON does not.

Files are always emitted (empty with a header row when the domain is empty),
so a consumer can rely on the file list being stable. Every file is UTF-8
with a BOM-free `\n` line terminator.

### `movies.csv` — canonical source: `DiaryEntry` where `media_type='movie'`

`diary_id,watched_date,media_id,tmdb_id,media_type,title,release_date,rating,is_rewatch,created_at,review_id`

### `tv_episodes.csv` — canonical source: `TVEpisodeWatch`

`watch_id,watched_date,show_media_id,show_tmdb_id,season_number,episode_number,episode_name,rating,notes,is_rewatch,created_at,updated_at`

> `watched_date` is a **date** (`YYYY-MM-DD`), not a timestamp: the model
> stores `db.Date`. It is never widened into a local-midnight instant.

### `tv_progress.csv` — source: `TVShowProgress`, user-authored fields only

`progress_id,show_media_id,show_tmdb_id,show_title,status,is_favorite,started_at,last_watched,completed_at,created_at,updated_at`

> Counter columns (`watched_episodes`, `total_episodes`, `watched_seasons`,
> `total_seasons`) are **intentionally absent**. They are derived, and the
> aired denominator is not reconstructible offline. See [§7](#7-canonical-vs-derived).

### `watchlist.csv` — source: `user_watchlist` + `MediaItem`

`media_id,tmdb_id,media_type,title,release_date,date_added,priority`

### `lists.csv` — source: `UserList`

`list_id,title,description,is_public,list_type,slug,cover_image,categories,created_at,updated_at`

> `categories` is a `|`-joined list of category names.

### `list_items.csv` — source: `UserListItem` (owner lists only)

`item_id,list_id,tmdb_id,media_type,title,position,note,added_at`

### `list_collaborators.csv` — source: `ListCollaborator` (own membership)

`list_id,role,added_at`

> No `user_id`, `username`, or `added_by`. A collaborator's identity is
> another user's data and is not exported — see [§9](#9-relationship-handling).

### `reviews.csv` — source: `Review`

`review_id,media_id,tmdb_id,media_type,title,review_title,contains_spoilers,rewatch,watched_date,created_at,updated_at,content,rating`

> Engagement counters (`likes_count`, `helpful_count`, `comments_count`) are
> derived and excluded. Deleted reviews (`is_deleted`) are excluded as
> deleted content.

### `ratings.csv` — denormalised rating ledger

`source,entity_id,tmdb_id,media_type,title,season_number,episode_number,rating,watched_date`

`source` is one of `diary_entry`, `review`, `tv_episode`.

> Ratings are not a separate canonical model in FrameIQ — they are fields on
> `DiaryEntry`, `Review` and `TVEpisodeWatch`. This file is a **projection**
> that lets a spreadsheet see every rating in one place without
> inventing a second source of truth. A future importer must treat the
> owning domain row as canonical and use this file for convenience only;
> that is recorded in the bundle `README.txt`.

### `tags.csv` — source: `UserMediaTag` + `Tag`

`user_media_tag_id,tag_id,tag,tmdb_id,media_type,created_at`

### `smart_lists.csv` — source: `SmartList`

`smart_list_id,name,description,scope,filters_json,sort,is_public,created_at,updated_at`

### `streaming_services.csv` — source: `UserStreamingService`

`service_id,provider_id,region,created_at`

### `recommendation_feedback.csv` — source: `RecommendationFeedback`

`feedback_id,media_id,media_type,surface,source,event,position,reason_kind,payload_json,model_version,event_date,created_at`

### `social.csv` — source: `UserFollow`, `MediaLike`, `ReviewLike`, `ReviewHelpful`, `ListLike`

`kind,target_id,related_id,related_username,created_at`

`kind` is one of `following`, `media_like`, `review_like`, `review_helpful`, `list_like`.

> For relationships pointing at another user's content (`review_like`,
> `list_like`), only the numeric ids and the like date are exported. The
> review author's and list owner's profile data are not.

### `comments.csv` — source: `MediaComment`, `ReviewComment`, `ListComment` (authored)

`comment_id,kind,target_id,media_id,media_type,parent_id,content,created_at,updated_at,is_deleted`

### `notifications.csv` — source: `Notification`

`notification_id,type,show_id,season,episode,created_at,read_at`

> `title`, `body`, `poster_path`, `episode_name` and `target_url` are
> server-generated display content derived from TMDb, not user data — see
> [§8](#8-omitted-sensitive-data).

### `continue_watching.csv` — source: `ContinueWatchingItem` + `WatchProgress`

`kind,tmdb_id,media_type,season,episode,title,current_time,duration,started_at,updated_at`

### `chat.csv` — source: `ChatConversation` + `ChatMessage`

`conversation_id,conversation_title,conversation_created_at,message_id,message_role,message_created_at,message_content_bytes`

> Assistant message text is deliberately excluded; only the user's own
> messages carry `message_content_bytes` content, and their `role`. See
> [§8](#8-omitted-sensitive-data).

---

## 5. ZIP bundle

`GET /api/account/export/csv` returns `application/zip` containing:

```
README.txt
movies.csv
tv_episodes.csv
tv_progress.csv
watchlist.csv
lists.csv
list_items.csv
list_collaborators.csv
reviews.csv
ratings.csv
tags.csv
smart_lists.csv
streaming_services.csv
recommendation_feedback.csv
social.csv
comments.csv
notifications.csv
continue_watching.csv
chat.csv
```

All names are fixed literals — never derived from user input — so no
path-traversal or filename-injection surface exists. The archive contains no
`.env`, logs, database dumps, or configuration.

The bundle does **not** embed `frameiq-export.json` (it would double the
payload); the JSON is a separate download. `README.txt` names it.

Built with `zipfile` from the Python standard library; written to a
`tempfile.NamedTemporaryFile` and unlinked in a `finally` block, so a failed
or aborted response cannot leave user data on disk.

---

## 6. Value conventions

| Concern | Rule |
|---|---|
| JSON null | `null` |
| CSV null | empty field |
| Booleans | `true` / `false` in JSON; `true` / `false` in CSV |
| `db.Date` | `YYYY-MM-DD` (date only — never widened to an instant) |
| `db.DateTime` | ISO-8601 UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| Timezone | all `DateTime` values are naive-UTC by the project's convention and are emitted with an explicit `Z` |
| Localized dates | never |
| Ambiguous dates | never (`08/10/26` is not emitted) |
| Float ratings | emitted as stored (`4.5`, `3.0`) |

`watched_date` on `DiaryEntry` and `TVEpisodeWatch` is a **calendar date the
user chose**, not an instant. It is exported as a date in both JSON and CSV
and is never shifted into a different day by timezone conversion.

---

## 7. Canonical vs derived

| Domain | Canonical source | Derived fields |
|---|---|---|
| Movie watch history | `DiaryEntry` (`media_type='movie'`) | `user_viewed` mirror (see below) |
| TV episode history | `TVEpisodeWatch` | — |
| TV progress | `TVShowProgress` user-authored columns (`status`, `is_favorite`, dates) | counter columns, excluded |
| Viewed state (movies) | `DiaryEntry` | `user_viewed` mirror |
| Viewed state (TV) | `TVEpisodeWatch` **plus aired metadata** | not exportable offline; not exported |
| Watchlist | `user_watchlist` | — |
| Lists / list items | `UserList`, `UserListItem` | `ListAnalytics` → `derived` |
| Reviews | `Review` | `likes_count`, `helpful_count`, `comments_count`, excluded |
| Ratings | fields on the owning domain row | `ratings.csv` is a projection |
| Tags | `UserMediaTag` (+ `Tag` name) | `Tag.usage_count`, excluded |
| Smart Lists | `SmartList` definition | computed result rows, never stored or exported |
| Taste | `RecommendationFeedback` (raw events) | `TasteProfile` → `derived` |
| Notifications | `Notification.read_at` | server-generated title/body, excluded |
| Continue Watching | `ContinueWatchingItem`, `WatchProgress` | `progress_pct` (computable) |

### Why TV counters and the `user_viewed` mirror are not canonical history

Tasks F1–F4 established that for TV, *Viewed* is derived from
`TVEpisodeWatch` **intersected with the aired set**, and that the aired set
comes from TMDb metadata. `TVShowProgress.watched_episodes` /
`total_episodes` are cached counters maintained by
`sync_tv_progress_counters()`; the aired denominator is not reconstructible
without TMDb. Exporting them would mean shipping a stale derived number as
if it were history, and the export must run fully offline. They are omitted,
and `tv_progress.csv` carries only the fields the user actually set.

For movies, `user_viewed` is a mirror of diary entries. It is exported under
`viewed_mirror`, explicitly labelled as a mirror, because importers find it
convenient — but the canonical movie history is `movies.csv` /
`watch_history.movies`.

---

## 8. Omitted sensitive data

Never selected into the serializer, so it cannot leak:

| Category | Specific fields |
|---|---|
| Credentials | `User.password_hash` |
| Email-verification / reset artifacts | any token derived from `utils/email.py` (never persisted on `User`) |
| Sessions | Flask session cookies, `session_id` |
| CSRF | `csrf_token` (session cookie value) |
| OAuth | access tokens, refresh tokens (not persisted) |
| API keys | TMDb, OpenAI, Cloudinary, SMTP |
| Secret signing keys | `SECRET_KEY` |
| IP addresses | `ListView.ip_address` |
| Share tokens | `YearInReviewShare.token_hash` |
| Agent state | `UserChatMemory.content`, `UserChatDailyUsage` |
| Quota accounting | `UserChatDailyUsage.question_count` |
| Derived counters | `User.total_reviews`, `total_movies_watched`, `followers_count`, `following_count`; `Review.likes_count`/`helpful_count`/`not_helpful_count`/`comments_count`; `Tag.usage_count`; `ListCategory.usage_count` |
| Enrichment caches | `MediaItem.directors_enriched_at` |
| TMDb caches | `MovieReleaseDate`, `Director`, `MediaDirector`, `UpcomingEpisode` |

Chat message content is exported **only for the user's own messages**
(`role == 'user'`). Assistant text is omitted deliberately: it is not the
user's data, and it is model output that would bloat a backup.

---

## 9. Relationship handling

Where a row references another user, only the minimum portable reference is
exported:

| Relationship | Exported | Withheld |
|---|---|---|
| following | followed user's **public username** + follow date | email, bio, profile picture, id-lookup keys, follower counts |
| review like / helpful vote | target review id + date | the review author's identity and the review content |
| list like | target list id + date | the list owner's identity and list content |
| list collaborator (own membership) | list id + own role + date | collaborator identity, `added_by`, invitee details |
| liked / commented media | tmdb id + media type | — (public catalogue data) |

A list the user **collaborates on but does not own** contributes only a
`list_collaborators.csv` row (their role). Its title, description, items and
categories belong to the owner and are **not** exported — being a
collaborator is not ownership.

---

## 10. Determinism

For identical database state, the export is byte-identical except
`generated_at`.

Every collection is ordered by an explicit total ordering that ends in a
primary key, so no result depends on unspecified SQL row order:

| Collection | Order |
|---|---|
| `movies.csv` | `watched_date`, `diary_id` |
| `tv_episodes.csv` | `show_tmdb_id`, `season_number`, `episode_number`, `watch_id` |
| `tv_progress.csv` | `show_media_id` |
| `watchlist.csv` | `media_type`, `tmdb_id` |
| `lists.csv` | `list_id` |
| `list_items.csv` | `list_id`, `position`, `item_id` |
| `list_collaborators.csv` | `list_id` |
| `reviews.csv` | `created_at`, `review_id` |
| `ratings.csv` | `source`, `entity_id` |
| `tags.csv` | `tag`, `user_media_tag_id` |
| `smart_lists.csv` | `created_at`, `smart_list_id` |
| `streaming_services.csv` | `provider_id` |
| `recommendation_feedback.csv` | `event_date`, `feedback_id` |
| `social.csv` | `kind`, `target_id`, `related_id` |
| `comments.csv` | `kind`, `comment_id` |
| `notifications.csv` | `created_at`, `notification_id` |
| `continue_watching.csv` | `kind`, `tmdb_id`, `season`, `episode` |
| `chat.csv` | `conversation_id`, `message_created_at`, `message_id` |

Tests normalise `generated_at` and then compare the full canonical payload,
so a future format regression fails loudly.

---

## 11. Evolution rules

- `version` is an integer that increments only for **breaking** changes to
  the meaning of an existing field. Adding an optional field within a section
  is a minor change and does not bump it.
- A version bump is never silent: the old interpretation must keep working
  for old `version` values, or a new field name must be introduced.
- `version` is independent of the application version and of any git SHA.
- Consumers should branch on `version`, and ignore unknown keys within a
  known version, so that additive changes do not break them.
- `format` is a constant string; a different `format` value means the file is
  not a FrameIQ export at all.

---

## See also

- [`docs/import-format.md`](import-format.md) — the ingest counterpart:
  Letterboxd and TV Time import, resolution and idempotency rules.
- [`docs/conventions.md`](conventions.md) — response cleanup (`call_on_close`
  is not reliable here), the offline TMDb placeholder, generated-vs-canonical
  data rules, and untrusted-upload handling.