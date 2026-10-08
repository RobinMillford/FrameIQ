# Engineering Conventions

Rules that are not obvious from the code, are enforced by CI, or were learned
the hard way. Each one names the failure it prevents.

---

## Response cleanup: never rely on `call_on_close` for temporary files

**Rule.** Do not assume Flask's `response.call_on_close(...)` will run your
cleanup for a streamed / `send_file` response.

**Why.** During Task F5 the CSV bundle was written to `tempfile.mkstemp()` and
scheduled for deletion with `response.call_on_close()`. Probing a single,
fully-consumed download showed the callback **never fired** — the archive was
never removed. The box had accumulated **87 ZIP files in `/tmp`, each holding a
complete account export**. Nothing failed, no test failed, and no log recorded
it; the guard step in the workflow is unrelated to response lifecycle.

**Do this instead, in order of preference:**

1. **Do not create a temporary file.** Build bounded generated output in an
   `io.BytesIO` and pass that to `send_file`. This is what
   `api/account_export.py::build_csv_bundle` does, and it removes the whole
   class of bug rather than hardening one instance of it.
2. **If disk-backed streaming is genuinely required** — output larger than you
   want to buffer, or a true multi-gigabyte stream — then use an *explicitly
   verified* lifecycle:
   - unlink via a mechanism you have actually observed firing (an explicit
     `try/finally` around the response, or `os.unlink` immediately after opening
     the handle and relying on POSIX keeping the inode alive through the fd),
     **not** an untested close-hook;
   - name the temp file so it is attributable (`tempfile.mkstemp(prefix=...)`);
   - open it with restrictive permissions (`mkstemp` defaults to `0600`);
   - and **add a regression test** that asserts the file is gone — or, better,
     that it was never created.
3. **Never log the payload** when a generation step fails. Log a message and
   the exception; the payload is the user's private data.

`tests/test_account_export.py::test_bundle_never_touches_disk` is the regression
test. It asserts the *strong* property — downloading creates no file at all —
rather than "the file is removed afterwards".

If you find `call_on_close` in application code that cleans up something on
disk, treat it as a bug until you have verified it fires. Do not assume it
works because it reads correctly.

---

## Decorative separators: do not use 7+ `=`, `<` or `>` runs

**Rule.** In tracked text files, build visual rules from `-` or from the
box-drawing characters the codebase already uses (`─`, `═`). Reserve runs of
`=` (and `<` / `>`) for actual merge-conflict markers.

**Why.** The CI whitespace/conflict-marker gate scans *added* lines with
`^\+([^+].*)?(<{7}|={7}|>{7})` — seven or more of those characters **anywhere**
in an added line fails the build, not only at the start of the line. An
ASCII-art heading underline in the F5 bundle README
(`api/account_export.py`, the `README.txt` template) tripped it and had to be
replaced with hyphens.

The gate is deliberately conservative and must stay that way — it is a guard
against shipping unresolved conflict text. Adapt the content, not the guard.

---

## Offline TMDb placeholder: one canonical value

**Rule.** The offline suite uses exactly `test-tmdb-key`, in all three places
it appears:

| Location | Role |
|---|---|
| `tests/conftest.py` | `setdefault` local default |
| `.github/workflows/ci-cd.yml` | `env:` for the CI test step |
| `tests/test_tmdb_offline.py` | the assertion in `test_tmdb_key_is_a_throwaway_placeholder` |

**Why.** `conftest.py` uses `os.environ.setdefault`, which does **not** override
an existing variable. Any workflow that *sets* `TMDB_API_KEY` therefore wins,
and the local default cannot mask a mismatch — the contract test sees the
workflow's value and fails. This happened: CI shipped
`ci-offline-placeholder` while the contract expected `test-tmdb-key`.

Changing the assertion to accept a list of placeholders would destroy the test's
only purpose, which is to guarantee CI uses the one known-harmless value. If
you need a different value, change it in all three places.

The ordinary suite must never require `secrets.TMDB_API_KEY`.
`tests/test_deploy_workflow.py` enforces that. Real-TMDb coverage is opt-in via
`@pytest.mark.tmdb` and needs a real credential from the environment.

---

## Generated/derived data is never canonical

**Rule.** Export and import both read from the model that is the source of
truth, and anything recomputed goes in an explicitly labelled section.

Concretely, in this codebase:

- TV episode history is `TVEpisodeWatch`, never `TVShowProgress` counters and
  never the `user_viewed` mirror.
- Movie watch history is `DiaryEntry`; `user_viewed` is a mirror.
- `ratings.csv` is a **projection** of rating fields that live on their owning
  domain rows, not a second authority.
- Engagement counters, `ListAnalytics` and `TasteProfile` are derived.

**Why.** F4 established that `TVShowProgress.total_episodes` is the canonical
*aired* count, which cannot be rebuilt without TMDb. Shipping it as history
would present a stale derived number as fact. See `docs/export-format.md` §7.

An importer must go through the canonical write path (currently
`mark_episode_watched_core`) rather than inserting rows directly, or it will
skip eligibility gating, counter sync and completion gating.

---

## File uploads are untrusted input

**Rule.** Treat every uploaded byte as hostile:

- authenticate; scope strictly to the current session user and never accept a
  client-supplied user id;
- require CSRF on any state-changing request, and use POST only — a GET must
  never mutate;
- bound the upload size **before** parsing;
- validate the extension and sniff the actual container rather than trusting
  the client-supplied filename;
- for archives, inspect members and reject absolute paths and `..` traversal;
  read members in memory instead of extracting to arbitrary filesystem paths;
- never `pickle`, never a dynamic import, never shell out, never render an
  uploaded path as a template.

**Why.** Extraction-to-path bugs are a standard, remotely exploitable class, and
the cheapest defence is never extracting.

---

## Decorative rules in documentation

Long markdown tables and section lists are fine, but avoid underlining a heading
with 7 or more `=` characters (see the separator rule above).

## Import resolution must never auto-select (Task F7)

FrameIQ refuses to guess which title an external record means. Two rules follow
from that, and both are load-bearing:

**Nothing is auto-selected and nothing is auto-skipped.** An `unresolved` or
`ambiguous` row stays `unresolved` until the user picks a candidate or
explicitly skips it. A default that looks like progress is a guess.

**A saved mapping outranks a fresh choice.** Order of trust is:

    saved mapping > in-panel choice > external id > unique title

A user who saved `Stalker → 1979` and ticks `2002` for one row still gets 1979.
Silently re-resolving behind a mapping is the surprise the feature exists to
prevent; changing one is an explicit edit or delete.

## Do not put portability metadata in canonical history models

Import bookkeeping lives in its own table (`import_source_mapping`), never as
extra columns on `DiaryEntry`, `TVEpisodeWatch`, `MediaItem` or
`TVShowProgress`. Those are canonical: F1-F4 correctness, TV eligibility and
every statistics statement read their shape. Adding `letterboxd_uri` to a diary
row would put import concerns inside the records the product treats as truth.

## Preview must not write — including mappings

`preview()` writes no rows and persists no mappings, even when the request
carries the user's choices. A durable answer must not appear as a side effect
of *looking* at a file. `apply_import` owns every write.

## Production schema is changed only through migrations

The production web process performs **no DDL**. The architecture is:

    BUILD EXACT SHA -> DEPENDENCY HEALTH -> MIGRATION -> SCHEMA GUARD
    -> WEB START -> HEALTH -> SMOKE

Never:

    WEB START -> implicit table creation -> hope the schema is right

`app.py` used to call `db.create_all()` on every start. It does not any more.
Schema creation is off by default and only happens when a caller explicitly sets
`FRAMEIQ_AUTO_CREATE_SCHEMA=1`, which logs a warning when used.

Two consequences worth stating plainly:

- **A web worker does not own the schema.** A forgotten migration now stops the
  deploy, rather than "fixing itself" on the next boot and hiding the omission.
- **The parity guard became load-bearing.** With implicit creation gone, an
  unprepared schema makes startup *fail loudly* instead of quietly repairing
  itself. That is deliberate: failing to boot on a bad schema is correct, and is
  the same posture the guard already took for missing columns.

`create_all()` is still legitimate in explicitly controlled contexts, and each
existing one was left alone because it already was one:

| Where | Why it is fine |
|---|---|
| `tests/conftest.py` | Session fixture on a throwaway temp SQLite, created explicitly. |
| `scripts/verify_*_browser.py` | Each boots a throwaway SQLite and creates its own schema explicitly. |
| `scripts/bootstrap_dev_schema.py` | The explicit local/test path. Prints its target before connecting and refuses production. |
| `migrates/*.py` | Run deliberately as a named one-off step, never at web start. |

## Do not reach a database through a module import

Importing `app` executes `create_app()`, which used to execute
`db.create_all()`. **Any** diagnostic that imported the app therefore inherited
DDL authority over whatever `DATABASE_URL` pointed at.

Never run an app-importing Python diagnostic while `DATABASE_URL` points at a
production database. Before any command that could import the application or
initialise SQLAlchemy, print and confirm the target, and run it through a
wrapper that hard-fails on a non-scratch URL.

For inspecting a live database, use
`scripts/check_production_schema_readonly.py`. It never imports the app, opens a
plain driver connection, and sets the session `READ ONLY` so the server itself
rejects any write — a guarantee rather than a promise in a docstring.

## PostgreSQL index names are schema-scoped, not table-scoped

Two different tables cannot declare the same index or constraint name in
PostgreSQL. This is invisible in development on SQLite, which permits the
duplicate, and it fails *mid-transaction* — so one colliding name rolls back
every other statement sharing that transaction.

`TasteProfile` and the legacy `UserTasteProfile` table both declared
`idx_taste_profile_updated`. The legacy table is created by
`migrate_week4_discovery.py` and is deliberately not declared by any model, so
"who owns this name?" answered from model metadata returns *nobody*. The
collision could therefore only be found by a failing `CREATE INDEX`.

Rules that follow:

- Before creating a table, read the **live catalog** for the names it wants, not
  the models. `migrate_schema_convergence.py` does this and aborts with the
  conflicting names before attempting any DDL.
- `tests/test_startup_schema_safety.py` asserts schema-wide uniqueness of every
  declared index and constraint name, which is the only check that catches this
  on a SQLite-based test suite.
- Do not "fix" a name collision by editing a legacy table. Rename the newer
  declaration. The legacy tables are depended on by migrations that promise not
  to disturb them.

## Repair drift with a migration, never by hand

If production schema drift is found, the repair is a new named idempotent
migration. Do not `CREATE`/`ALTER`/`DROP` production by hand and do not
`DROP INDEX`/`DROP TABLE`/`CASCADE` on a hunch. Diagnosis is read-only; the
change happens later through the normal migration-first deploy.

There is no migration *version table* in this repository. Idempotency comes from
inspecting the live catalog, which means nothing can report "which migrations
has production run?" — so a migration must be safe to run against a database in
any state, including one that is fully current.

## Incident note: F7 accidental production DDL probe

While probing why a migration appeared to be a no-op during Task F7, a
`python -c` snippet was run without overriding `DATABASE_URL`, which pointed at
the production Neon database. `from app import app` ran `create_all()` and
attempted DDL. It failed on its first statement (`DuplicateTable`, from the
index-name collision above) and the transaction **rolled back**.

Verified afterwards: no table had been added, removed or altered, and the
database's table count was unchanged.

Two changes came out of it, and both are now enforced by tests:

1. Production no longer creates its own schema, so an app import has no DDL
   authority at all.
2. `scripts/check_production_schema_readonly.py` gives a safe way to inspect a
   live database, and `scripts/bootstrap_dev_schema.py` refuses a
   production-looking target before opening a connection.

No credentials, hostnames or connection strings are recorded in this note.

