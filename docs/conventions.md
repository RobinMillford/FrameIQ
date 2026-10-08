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