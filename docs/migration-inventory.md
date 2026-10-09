# Migration inventory and runbook

How schema changes reach FrameIQ's database, what happened before this system
existed, and which paths are not covered by tests.

Read this before adding a migration. The short version:

- **Add a file in `migrates/` and register it in `migrates/registry.py`.** That
  is the whole mechanism.
- **Never edit a migration that has already been applied.** The runner refuses
  to start if you do.
- **Never run a script directly against production.** Only the runner runs
  migrations, and it refuses a remote target unless you pass
  `--allow-production` explicitly.

---

## 1. The problem this replaced

The database was changed by whatever script happened to be run. There was no
record of what had been applied, so:

- a script could be run twice, or missed entirely, with nothing to detect it;
- a script edited after deployment would silently differ from what ran;
- `.github/workflows/deploy.yml` had grown a hand-maintained list of script
  names, and **9 of the 31 historical owning migrations were missing from it**;
- the live database had drifted: 34 tables where 42 were declared.

The 11 missing tables were missing because `db.create_all()` in `app.py` had
been failing on an index-name collision (`idx_taste_profile_updated` existed on
the legacy `user_taste_profile` table, blocking creation of `taste_profile`) and
rolling back the whole call. Nothing reported the failure, because a web process
cannot report anything: it just serves.

---

## 2. The current system

```
migrates/migrations_000N_*.py     one file per migration, exposes run(connection)
migrates/registry.py              the ordered list of what is runnable
models/schema_migration.py        the ledger table (schema_migrations)
scripts/migrate.py                the runner: status | validate | upgrade | adopt-legacy-baseline
```

The runner is the only supported way to change the schema. The web process
performs no DDL, ever — including no implicit `create_all()`.

### Commands

A destructive migration is never reached by a plain `upgrade`. It is deferred,
reported, and left pending until it is named explicitly *and* accompanied by
backup evidence.

| Command | Writes? | Use it for |
|---|---|---|
| `status` | no | Seeing applied/pending history and the guard verdict |
| `validate` | no | CI: registry soundness and every recorded checksum |
| `init-ledger` | yes (one table) | Creating ONLY `schema_migrations`, if absent |
| `upgrade` | yes | Applying every pending **non-destructive** migration |
| `upgrade --only <id>` + approval | yes | Applying one named destructive migration |
| `adopt-legacy-baseline` | yes | Once, to record the verified pre-existing state |

All four require `DATABASE_URL`. The write commands refuse a remote target
unless `--allow-production` is passed; **there is no environment variable that
grants this**, because a deploy should not be able to widen its own authority by
accident.

### What `status` will tell you

`status` separates two claims that are easy to conflate:

- **the schema matches the models** (the guard's verdict), and
- **history is known from the baseline onward** (the ledger).

A database can have a perfect schema and no history. That is the normal state of
every database that has not adopted a baseline yet, and `status` says so
explicitly rather than implying everything is accounted for.

### The ledger

`schema_migrations` columns: `version`, `checksum`, `kind`, `applied_at`,
`execution_ms`, `note`.

`checksum` is the SHA-256 of the migration's **entire source file**. That is
deliberately strict: changing a comment changes the checksum. This is what makes
"two databases both report `0001` applied" mean "both have the same code applied",
which is the only interpretation that makes the ledger worth having.

The `kind` column distinguishes `migration` from `baseline`, because they mean
very different things.

---

## 3. Guarantees, and where they stop

### Atomicity — real on PostgreSQL, weaker on SQLite

One transaction per migration, owned by the runner. The migration body and its
ledger row commit together, so a crash cannot produce a ledger row for work that
did not happen, or work without a ledger row.

On **PostgreSQL** that includes DDL, which is transactional. On **SQLite**,
pysqlite commits before DDL, so a failing migration that had already run DDL may
leave that DDL behind — while still correctly *not* recording a ledger row.

Production is PostgreSQL. CI and local development are SQLite, so **the weaker
guarantee is the one that is actually tested**. Do not read a green CI run as
proof that a failed PostgreSQL migration is fully undone.

### Concurrency — the lock, and what CI cannot show

Concurrent runs are excluded with a **session-scoped** PostgreSQL advisory lock
(`pg_advisory_lock`, key `0x46515538`). Session-scoped, not `pg_advisory_xact_lock`,
because the runner commits between reading the ledger and applying each
migration; a transaction-scoped lock would be released by the first COMMIT and
would leave exactly the interesting window unprotected.

The lock is taken **before** pending migrations are computed, so two runners
cannot both decide the same migration is pending and both apply it. There is no
lock table and therefore no stale row to clean up; the lock is released
explicitly and also automatically when the connection closes, so a killed runner
cannot leave a deployment blocked forever.

**The advisory-lock path is not exercised by CI.** This environment has the
`psql` client but no PostgreSQL server, so `pg_advisory_lock` has never actually
been executed here. On SQLite the runner prints:

```
lock: UNAVAILABLE on this backend (no such function: pg_advisory_lock)
      Continuing WITHOUT concurrent-runner exclusion. Do not rely on this for
      a shared database.
```

and continues. That is intentional — better an honest warning than a silent
false sense of safety — but it means the exclusion behaviour is **untested**. Run
one manual concurrent check against a real PostgreSQL before the first deploy
that ships a migration.

### Fail-closed checks, in order of how damaging it would be to skip them

1. The registry must be sound (no duplicate versions, no unknown or forward
   dependency, no cycle).
2. An applied migration whose file changed → refuse, before applying anything.
3. A pending migration whose dependency is unmet → refuse. Dependencies are
   validated against what *will* exist after earlier migrations in the same run
   complete, not only against what is already recorded — otherwise a fresh
   database could never bootstrap.
4. A migration that runs but whose `verify()` reports problems → roll back,
   record nothing.

### Baseline adoption is never automatic

`adopt-legacy-baseline` refuses unless the schema guard passes, the database is
not empty, and no baseline exists. An empty database is a *fresh bootstrap*, not
an adopted legacy one, and recording a baseline for it would be a false claim
that would permanently suppress the migrations needed to fix it.

The baseline's checksum is the SHA-256 of a fixed explanatory note, not of the 31
historical scripts. **It records that the schema was verified at a point in
time. It does not claim that each historical script ran**, because nobody
recorded that and reconstructing it would be inventing provenance.

---

## 4. Inventory of the 31 historical scripts

These are **not** registered and **must not** be scheduled by any directory scan.
Several drop tables, several rewrite data, and several call unscoped
`db.create_all()` that would create far more than they claim.

Their effects were verified once, during the F8 read-only audit, and the current
schema is the result. From the baseline onward, `registry.py` is authoritative.

### Ownership — the 11-table gap

These nine modern migrations own tables that were declared in the models but
**missing from production**, and none of them was wired into `deploy.yml`:

| Script | Owns |
|---|---|
| `migrate_continue_watching.py` | `continue_watching_item` |
| `migrate_director_capture.py` | `director`, `media_director` |
| `migrate_import_source_mapping.py` | `import_source_mapping` (F7) |
| `migrate_notification.py` | `notification` |
| `migrate_recommendation_feedback.py` | `recommendation_feedback` |
| `migrate_smart_lists.py` | `smart_list` |
| `migrate_streaming_services.py` | `user_streaming_services` |
| `migrate_taste_profile.py` | `taste_profile` |
| `migrate_year_in_review_share.py` | `year_in_review_share` |

Plus `migrate_movie_release_dates.py` → `movie_release_date`, which was already
wired but whose table had not been created on the deploy branch.

**Resolution:** `migrates/migrate_schema_convergence.py` creates exactly this
allow-list of 11 tables. It is additive only, idempotent, and fails closed on
any unexpected drift. It is a *historical* script, not a versioned migration,
because it repairs drift rather than recording a change in behaviour.

`migrate_week4_discovery.py` is a further historical owner whose tables already
exist; nothing to do.

### The rest

| Script | Disposition |
|---|---|
| `migrate_remove_wishlist.py` | Superseded by `0002_remove_legacy_wishlist` |
| `migrate_canonical_watched.py` | Superseded by `0001_canonical_watched_reconcile` |
| `migrate_lists_v2.py`, `migrate_lists_diary.py`, `migrate_week2_lists.py`, `migrate_week2b_lists.py` | Historical; tables already present |
| `migrate_week1.py`, `migrate_week3_reviews.py` | Historical; tables already present |
| `migrate_tv_tracking.py`, `migrate_tags.py`, `migrate_genres.py` | Historical; tables already present |
| `migrate_media_runtime.py`, `migrate_priority_column.py`, `migrate_email_verified.py` | Historical; columns already present |
| `migrate_comments.py`, `migrate_diary_statistics_indexes.py`, `migrate_userfollow_indexes.py`, `migrate_watch_progress_indexes.py` | Historical; indexes already present |

---

## 5. Outstanding production data, and why it is not automated

Found by the F8 read-only audit. **Not applied.** Each needs a deliberate
operator decision, not a deploy-time surprise.

### `canonical_watched` — 2 diary-only, 15 viewed-only pairs

F4 made the diary canonical for movies and `user_viewed` derived. Two
populations disagree:

- **diary-only** (2): a movie watched per the diary with no `user_viewed` row.
  `0001` inserts the missing `user_viewed` rows. This is the only direction the
  migration performs, because diary is canonical.
- **viewed-only** (15): marked viewed but never written to the diary. `0001`
  **preserves these rows exactly as they are** and reports them as
  `unresolved_legacy_markers`.

#### The canonical-viewed policy

**`DiaryEntry` is the canonical authority for MOVIE watch history.
`user_viewed` is a compatibility mirror, not an independent source of canonical
movie-watch history.**

The mirror exists so that legacy view-state surfaces can answer "has this been
marked viewed?" cheaply. It is not a second, parallel record of watching. The
distinction is not academic: production holds 15 `user_viewed` rows with no
`DiaryEntry`, and because several surfaces counted the mirror directly, each
marker inflated a real statistic — a "N items watched" badge, a profile stat
card, an achievement bar, a smart-list total.

Rules that follow from the policy:

| Rule | Consequence |
|---|---|
| Never fabricate a `DiaryEntry` or a watched date from a marker | `0001` creates diary rows in one direction only: diary → viewed |
| Never silently delete a marker | the 15 rows stay, unchanged |
| Markers are not canonical watch history | they contribute nothing to watched counts, totals or statistics |
| Never report markers as reconciled | `0001` returns `reconciled_viewed_only: 0` and `unresolved_legacy_markers: 15` |
| `verify()` must stay meaningful | it asserts the *derived* direction converged; it deliberately does **not** require the marker count to reach zero |
| TV is never folded in | TV history is `TVEpisodeWatch`; the `diary` smart-list scope now restricts to `media_type='movie'` |

#### Why the 15 are not reconciled, and what "unresolved" means

A `user_viewed` row with no diary entry is a **legacy marker**, not watch
history. Converting one would:

1. **Fabricate canonical history.** A `DiaryEntry` is authoritative forever in
   the diary, statistics and Continue Watching. Synthesising one invents a
   record the user never created.
2. **Require a date that does not exist.** `user_viewed.date_viewed` is
   nullable and not authoritative — it can be absent, or have been set by a bulk
   import rather than by the user. `diary_entry.watched_date` is `NOT NULL`, so
   a NULL `date_viewed` would actually **abort the INSERT** outright rather
   than degrade gracefully.
3. **Erase the distinction F4 exists to keep** — "watched and logged" versus
   "marked viewed".

So `0001` does not delete them, does not rewrite them, and does not count them
as reconciled. Its return value sets `reconciled_viewed_only: 0` and
`unresolved_legacy_markers: 15`.

**These 15 rows remain a real discrepancy and are still there after the
migration.** Resolving them needs an explicit product/data policy, for example:

- ask the affected user to confirm the watch, creating a dated `DiaryEntry`
  with recorded provenance; or
- accept the mark as canonical and record *why* the date is trustworthy; or
- leave them as markers indefinitely.

Until one of those is chosen, **statistics that read the diary are correct and
statistics that read the mirror are not comparable to them.** That is now
enforced in code rather than left to each surface's judgement: see below.

#### Surfaces corrected to follow the policy

Every watched-count / watched-total surface was audited. `api/statistics.py` is
the canonical service and already derived everything from `DiaryEntry`; these
surfaces bypassed it and counted the mirror:

| Surface | Was | Now |
|---|---|---|
| `routes/profile_enhancements.py` (badges, enhanced stats, achievements — 4 sites) | `count(user_viewed)` | `canonical_movies_watched(user_id)` |
| `routes/auth.py` profile `VIEWED` card | `.rowcount` on a bare `SELECT` — **rendered as `-1`** | `canonical_movies_watched(uid)` |
| `routes/main.py` + `templates/user_profile.html` "Movies Watched" | `len(user.viewed_media)` — also counted TV-typed rows | `canonical_movies_watched(user.id)` |
| `api/smart_lists.py` `diary` scope, labelled "Watched History" | joined `user_viewed`, no media-type restriction | joins `DiaryEntry`, `media_type='movie'` |
| `routes/lists.py` `watched_count` | summed the mirror-derived badge | counts a new `watched_canonical` flag from `DiaryEntry` |

`api/statistics.canonical_movies_watched(user_id)` is now the single shared
definition, so these surfaces cannot drift apart again.

**Deliberately unchanged.** The per-item "watched" *badge* still reads the
mirror: "has this been marked viewed" is exactly what the mirror records, and it
is the mirror's proper use. `routes/lists.py` now keeps both signals side by
side (`watched` from the mirror, `watched_canonical` from the diary) so the
badge and the statistic cannot be confused for one another. Raw account export
(`api/account_export.py`) still emits the mirror rows, already labelled
`viewed_mirror` / `derived`. `engaging_friends_count` and the
"Shared N movies with you" suggestion count remain mirror-derived: they count
social interactions, not watch history, and rewiring them is a product change.

### `user_wishlist` — 1 row still present

Read-only inspection of production (server-enforced read-only session; no
application import; no join to the `user` table, so no usernames or emails):

```
user_wishlist  columns : user_id, media_id, media_type, date_added, priority
user_watchlist columns : user_id, media_id, media_type, date_added, priority
  -> identical column sets; no legacy-only and no target-only columns
both keyed by : PRIMARY KEY (user_id, media_id, media_type)

source row     : user_id=3 (digest c906d725f0), media_id=13, media_type='movie',
                 date_added=2026-01-27 08:15:51.204544, priority='low'
matching target: NONE
user_watchlist : 4 rows
```

This is **not** a duplicate. The migration will **INSERT** it, carrying
`date_added` and `priority` through unchanged, then drop the legacy table.

#### Pre-DROP safety guarantee

`0002` classifies every source row before touching anything and refuses to drop
if any row cannot be represented:

| Verdict | Meaning |
|---|---|
| `INSERTED` | no target row existed; all source values kept |
| `EXACT_DUPLICATE` | a target row existed and every informative column matched |
| `REDUNDANT_NULL` | a target row existed and the source's differing columns were NULL, so nothing is lost |
| `CONFLICT` | a target row exists with differing **non-NULL** source values — stopping |
| `UNREPRESENTABLE` | the source cannot exist in the target (e.g. NULL `media_id`, part of the NOT NULL key) — stopping |

`CONFLICT` and `UNREPRESENTABLE` raise **before** the `DROP TABLE`, leaving
`user_wishlist` intact. The raised message names the verdicts and the
at-risk columns, because the runner wraps and truncates the printed detail. A
final gate re-queries for any source key still missing from the target and
checks that the classification counts equal the source row count — both before
the `DROP`.

The uniqueness rule that makes `EXACT_DUPLICATE` a well-defined verdict is the
primary key `(user_id, media_id, media_type)`, present on both tables: it means
at most one target row can correspond to a source row.

On PostgreSQL the whole thing is one transaction owned by the runner, so a
failure rolls the INSERT and the DROP back together.

#### Backup requirement — NOT YET MET

**`0002` is destructive and must not run until an operator has created and
verified a recoverable snapshot of the production database.** No such snapshot
has been verified for this task, and none is claimed to exist. The deployment
procedure must:

1. take a snapshot or `pg_dump` of production;
2. **verify it is restorable**, not merely that a file appeared;
3. record who verified it and when;
4. only then run the deploy, whose `upgrade` step applies `0002`.

Without a verified restore point, the legacy table is the only copy of its one
row, and the `DROP` is irreversible.

### Everything else

Audited and already applied, or a no-op.

---

## 6. Adding a migration

1. Create `migrates/migrations_<version>_<name>.py` exposing
   `run(connection)`. It **must not commit** — the runner owns the transaction.
2. Optionally expose `verify(connection) -> list[str]`. It runs in the same
   transaction after `run`; a non-empty result rolls everything back. Use it
   whenever "did this actually work?" is checkable, because it is the difference
   between a migration that fails and one that silently half-works.
3. If the migration genuinely cannot be transactional, set
   `NON_TRANSACTIONAL = True`. The runner will then **refuse** it rather than
   record a row it cannot make atomic.
4. Register it in `migrates/registry.py` with `depends_on` set correctly.
5. Run `python scripts/migrate.py validate`, then `upgrade` on a scratch
   database, then re-run `upgrade` to confirm it is a no-op.

Version strings are zero-padded and sorted as whole strings. Do not reuse a
version number, even for an identical migration.

---

## 6a. The destructive-migration gate

`0002_remove_legacy_wishlist` drops a table. No verified, restorable production
snapshot exists, so it is blocked by default. Enforcement is at **two
independent levels**, so that a mistake in either one is caught by the other.

### Level 1 — the deployment workflow

`deploy.yml` keeps two jobs:

* **`deploy`** — automatic, on `workflow_run` (CI green on `main`). Runs
  convergence, the schema guard, baseline adoption, the *non-destructive*
  migrations, the guard again, and the web recreate. It passes **no**
  authorization flags, so it structurally cannot drop anything.
* **`destructive`** — reachable only by `workflow_dispatch`, and only for the
  migration `0002_remove_legacy_wishlist`. It:
  1. rejects a `ref` that is not a full 40-character SHA, so the code that runs
     is pinned to a reviewed commit;
  2. rejects placeholder evidence;
  3. **queries the `production-destructive` environment and fails closed if it
     has no required reviewers** — an environment without reviewers admits
     anyone, which would make the job self-authorising while appearing gated;
  4. shares the `production-deploy` concurrency group with the safe deploy, so
     an approval cannot race a deploy that is already migrating;
  5. re-verifies the VPS SHA and a clean worktree, then re-checks the backup
     reference immediately before the destructive call.

The backup reference is a **per-run input**, not a repository variable, because
a stale variable would still assert that a snapshot exists weeks later.

### Level 2 — the runner

A migration declares itself with `DESTRUCTIVE = True`. The runner then refuses
unless **all three** are supplied, and re-verifies them itself:

```
--authorize-destructive 0002_remove_legacy_wishlist   # this exact migration
--backup-ref <snapshot reference>                    # ≥8 chars, not a placeholder
--backup-verified-by <operator who restored it>
```

Design properties:

* **Per-migration.** The authorization names one version, so it cannot become a
  blanket pass for a future, different destructive migration — that one needs
  its own id.
* **Not environment-driven.** Nothing here is read from the environment, so a
  deploy cannot widen its own authority with a leftover variable.
* **Placeholders rejected.** `yes`, `true`, `none`, `TODO`, `snapshot`, … are
  refused by name, as are references shorter than 8 characters.
* **Checked before `run()`**, so no code path reaches a destructive migration
  without passing the check.
* **Refusal is total.** Exit nonzero, nothing applied, no partial merge, no
  `DROP`, and an error naming what was missing.
* **Audited afterwards.** The ledger row for a destructive migration stores the
  version, backup reference and verifier.

`0001` declares `DESTRUCTIVE = False` and keeps the ordinary execution path.

### Required operator procedure

1. Create a snapshot or `pg_dump` of production.
2. **Restore it somewhere and confirm it works.** A file that exists is not a
   backup that restores.
3. Add required reviewers to the `production-destructive` environment
   (Settings → Environments) if that has not been done.
4. Dispatch the `destructive` workflow with the SHA, the snapshot reference and
   your name.

---

## 6b. The ledger bootstrap (`init-ledger`)

### Why it exists

The first production deploy after the F8 rollout failed, and the reason was an
ordering bug that every individual step was correct about.

`schema_migrations` is declared by the model, so bounded convergence saw it as a
**missing table**. But it is new infrastructure, not one of the eleven
historical gaps, so convergence did exactly what it was designed to do: refused
before any DDL, reporting `schema_migrations` as unexpected drift. Because it
refused, it created nothing at all — so `import_source_mapping` stayed missing
too, and the nightly release-data sync then failed during `import app`.

The tempting fix is to add `schema_migrations` to `EXPECTED_REPAIR_SET`. That is
wrong: that set is the reviewed list of **historical** table gaps, and widening
it would let unreviewed tables be created by a historical repair script. It is
also unnecessary — the ledger is not a data repair at all, it is the
precondition for recording repairs.

### What it guarantees

* Creates **exactly one** table, `schema_migrations`, with the declared schema.
* Never calls `db.create_all()` / `metadata.create_all()`.
* Writes **no rows**: no baseline, and no migration recorded as applied.
* Idempotent: absent -> created; present with the declared schema -> no mutation.
* Present with an incompatible schema -> **fails closed**. A drifted ledger
  would make every later version and checksum comparison meaningless, and
  repairing it could destroy the only record of what has been applied, so this
  is never done speculatively.
* Requires `--allow-production` for a remote target, exactly like the other
  write commands, and refuses a remote target without it.

Type comparison is by **type family**, not by rendered name: the same logical
type is `DATETIME` to SQLAlchemy, `TIMESTAMP` to PostgreSQL and
`TIMESTAMP WITHOUT TIME ZONE` in PostgreSQL's catalog. Comparing names reported
a spurious mismatch on a ledger the bootstrap had just created. String *length*
is still compared, because `VARCHAR(64)` and `VARCHAR(128)` are genuinely
different schemas.

It is a runner command, not a registered migration: the ledger must exist before
anything can be recorded in it, so it cannot be recorded in it.

---

## 7. Deploy sequence

`.github/workflows/deploy.yml` runs, in order, failing before web is recreated
at any step:

0. `scripts/migrate.py init-ledger --allow-production` — create ONLY the
   ledger table if absent. **Must precede convergence**, which otherwise
   refuses on `schema_migrations` as unexpected drift. See §6b.
1. `migrate_schema_convergence.py` — one-time additive creation of the 11
   declared-but-missing tables.
2. `utils.schema_guard` — prove the schema matches the models.
3. `scripts/migrate.py adopt-legacy-baseline --allow-production` — once; a no-op
   afterwards. Never before the guard passes: the baseline asserts the schema
   was verified.
4. `scripts/migrate.py upgrade --allow-production` — every pending
   **non-destructive** migration. Any destructive migration is reported as
   `DEFERRED` and left pending.
5. `utils.schema_guard` — prove convergence.
6. `scripts/migrate.py status --allow-production` — report the history.

The destructive migration is a **separate, separately-approved deployment**
(`workflow_dispatch` + the `production-destructive` environment). See §6a.

The ordering of 3 before 4 matters: the baseline requires a passing guard, and
the guard cannot pass until convergence has created the missing tables.

---

## 7a. The release-data sync and schema readiness

`sync-watchlist-release-data.yml` runs `scripts/sync_watchlist_release_data.py`,
which imports `app`; that runs the startup schema guard. So when the schema was
not ready the sync refused — correctly, but with an opaque traceback and no
statement of what an operator should do.

The workflow now runs a **readiness preflight first**, from the same one-off
image the sync will use:

1. `python -m utils.schema_guard` — read-only, and exempts itself from the
   startup guard, so it neither writes nor bypasses anything;
2. on failure: four `::error::` lines saying the schema is not ready, that the
   sync did **not** run, and that a deployment carrying the required migration
   must complete first — then `exit 1`.

Deliberately **not** done: no `SKIP_SCHEMA_GUARD` anywhere in the sync workflow,
no `create_all`, no manual creation of `import_source_mapping`. The sync command
itself is unchanged and still starts with the normal guard enabled, so a missing
required table still prevents the synchronization code from running.

The job also joins the `production-deploy` concurrency group (shared with the
deploy workflow, `cancel-in-progress: false`) so a scheduled sync cannot race the
schema transition, and it keeps its daily 02:30 UTC schedule and manual trigger.
Failure visibility is unchanged: the workflow still opens an issue on failure.

The sync is **blocked** until a deployment carrying the required migration has
completed successfully. It will not self-heal.

---

## 8. Known limitations

Stated plainly so they are not mistaken for covered ground.

- **The PostgreSQL paths are executed, in CI.** `tests/test_migration_postgres.py`
  runs against a real `postgres:17` service (pinned to match production's 17.11)
  in the `postgres-migrations` job, selected with `pytest -m postgres`. It covers
  advisory-lock acquisition/release, cross-session exclusion, two competing
  runners, transactional-DDL rollback, ledger correctness on failure, retry,
  merge+DROP rollback, and the schema guard. Run it locally with
  `FRAMEIQ_TEST_POSTGRES_URL=postgresql://... pytest -m postgres`.
  It is excluded from the default run because it creates and drops tables, and
  because SQLite cannot prove transactional DDL.
- **`0002`'s destructive merge has never been run against production.** It is
  tested on SQLite against a faithful reproduction of the production shape, and
  the production row has been inspected read-only — but no verified, restorable
  snapshot exists yet, so it must not be deployed. See §5.
- **The 15 viewed-only pairs remain an unresolved data discrepancy** after
  `0001`. They are preserved, not reconciled, and closing them needs an explicit
  product/data policy. See §5.
- **`0002` refuses to drop on a conflicting row**, which is safe but means a
  future legacy row that conflicts will halt the deploy rather than resolve
  itself. That is intentional; resolving such a row is a data decision, not a
  migration's.
- **DDL rollback is only guaranteed on PostgreSQL.** See §3. Verified there.
- **pysqlite may commit around DDL**, so the SQLite suite must never be cited as
  evidence of PostgreSQL's transactional-DDL behaviour. Three SQLite tests skip
  deliberately for this reason and point at the PostgreSQL suite.
- **No `ON CONFLICT` clause is used in `0002`**, deliberately: it would need
  dialect-specific handling for no gain on a table this small, and the
  `NOT EXISTS` guard is explicit and portable.
- **The schema guard is compared against the declared models, not against a
  historical intent.** If a model is wrong, the guard will happily confirm a
  wrong schema. It checks parity, not correctness.
