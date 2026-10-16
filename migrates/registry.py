"""The migration registry: which versioned migrations exist, and in what order.

Why a registry rather than directory scanning
---------------------------------------------
The thirty historical scripts in ``migrates/`` must NOT become versioned
migrations. Several drop tables, several rewrite data, several are already
applied, and several use an unscoped ``db.create_all()`` that would create far
more than they claim. Discovering them by scanning a directory would silently
schedule all of them for execution on the next deploy.

So the set of runnable migrations is written down here, explicitly. Adding a
migration means adding a line to this file — a deliberate, reviewable act.

Ordering is by the ``version`` string, not by filesystem order and not by
alphabetical filename. ``0001_`` < ``0002_`` because the sort key is the whole
identifier, and identifiers are zero-padded.

Historical scripts are inventoried in ``docs/migration-inventory.md`` and are
NOT registered here. Their effects were verified once, explicitly, during the
one-time legacy transition; from that point the registry is authoritative.

Each entry declares:
    version   stable identifier; also the ledger key
    module    dotted path to a module exposing ``run(connection)``
    depends_on versions that must already be applied
    summary   one line, shown by ``status``
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass(frozen=True)
class MigrationSpec:
    """One registered, runnable migration."""

    version: str
    module: str
    summary: str
    depends_on: Tuple[str, ...] = field(default=())

    def __post_init__(self):
        if not self.version:
            raise ValueError('a migration must have a version')
        if not self.module:
            raise ValueError('a migration must have a module')


# The two outstanding production data transformations found during the F8 audit,
# plus F9's additive cast schema. All are registered as ordinary forward
# migrations so they are executed by the runner, recorded in the ledger, and
# verifiable afterwards.
MIGRATIONS: Tuple['MigrationSpec', ...] = (
    MigrationSpec(
        version='0001_canonical_watched_reconcile',
        module='migrations_0001_canonical_watched',
        depends_on=(),
        summary='Reconcile derived user_viewed with canonical diary history '
                '(movies only).',
    ),
    MigrationSpec(
        version='0002_remove_legacy_wishlist',
        module='migrations_0002_remove_legacy_wishlist',
        depends_on=('0001_canonical_watched_reconcile',),
        summary='Merge the legacy user_wishlist into user_watchlist, then '
                'drop it. DESTRUCTIVE — see docs/migration-inventory.md.',
    ),
    # F9 cast persistence. depends_on 0001 and DELIBERATELY NOT 0002: 0002 is
    # destructive and stays deferred until an operator records a verified
    # restorable snapshot, so depending on it would hold this purely additive
    # schema hostage to an unrelated blocked DROP. See
    # migrates/migrations_0003_cast_persistence.py.
    MigrationSpec(
        version='0003_cast_persistence',
        module='migrations_0003_cast_persistence',
        depends_on=('0001_canonical_watched_reconcile',),
        summary='Add person/media_cast tables, MediaItem.cast_enriched_at '
                'and TasteProfile.actor_affinity_json for offline cast '
                'capture. Additive.',
    ),
    # F12 notification preferences. Same reasoning as 0003: additive, and it
    # depends on 0001 ONLY so the deferred destructive 0002 cannot block it.
    # Populates nothing — a user with no preference row resolves to documented
    # defaults at read time.
    MigrationSpec(
        version='0004_notification_preferences',
        module='migrations_0004_notification_preferences',
        depends_on=('0001_canonical_watched_reconcile',),
        summary='Add user_notification_preference (one row per user) for '
                'in-app notification categories. Additive.',
    ),
    # F32 media runtime. Same reasoning as 0003 and 0004: purely additive, and
    # it depends on 0001 ONLY so the deferred destructive 0002 cannot block it.
    # This gives media_item.runtime a registered owner. Previously the only
    # remedy was the unledgered legacy script migrates/migrate_media_runtime.py,
    # which is not a deployment mechanism and which the schema guard had no
    # declared owner for.
    MigrationSpec(
        version='0005_media_runtime',
        module='migrations_0005_media_runtime',
        depends_on=('0001_canonical_watched_reconcile',),
        summary='Add media_item.runtime (nullable INTEGER, minutes) so detail '
                'queries stop failing on a database that predates the column. '
                'Additive.',
    ),
    # F55. Password resets and credential changes must revoke sessions and
    # remember-me cookies already issued to the user. Those cookies are
    # stateless and signed, so nothing server-side can be deleted to achieve
    # it; the only handle is a per-user counter carried inside both cookies
    # (utils/session_auth). Purely additive.
    MigrationSpec(
        version='0006_user_session_version',
        module='migrations_0006_user_session_version',
        depends_on=('0001_canonical_watched_reconcile',),
        summary='Add user.session_version (NOT NULL INTEGER DEFAULT 0) so a '
                'password reset can revoke every issued session and '
                'remember-me cookie. Additive.',
    ),
)


def ordered_migrations():
    """All registered migrations in deterministic order.

    Sorted by version string. Directory listing order is never consulted, so
    the sequence cannot change because of the filesystem.
    """
    return sorted(MIGRATIONS, key=lambda spec: spec.version)


def by_version():
    """``{version: MigrationSpec}`` for O(1) lookup."""
    return {spec.version: spec for spec in ordered_migrations()}


def validate():
    """Fail closed on a malformed registry.

    Checks, in order of how damaging they would be:

    1. duplicate versions — two migrations claiming one ledger key;
    2. a dependency that names an unregistered version;
    3. a forward dependency (depending on a version that runs later), which
       would deadlock at apply time;
    4. a dependency cycle.

    Returns a list of human-readable problems. Empty means the registry is
    sound; callers must treat a non-empty list as fatal.
    """
    problems: List[str] = []
    specs = ordered_migrations()
    versions = [spec.version for spec in specs]

    seen = set()
    for version in versions:
        if version in seen:
            problems.append('duplicate migration version %r' % version)
        seen.add(version)

    registry = set(versions)
    position = {version: index for index, version in enumerate(versions)}

    for spec in specs:
        for dependency in spec.depends_on:
            if dependency not in registry:
                problems.append(
                    '%s depends on %r, which is not registered'
                    % (spec.version, dependency))
            elif position[dependency] > position[spec.version]:
                problems.append(
                    '%s depends on %r, which runs later — a forward dependency'
                    % (spec.version, dependency))

    problems.extend(_cycles(specs, position))
    return problems


def _cycles(specs, position):
    """Report dependency cycles as ``a -> b -> a``."""
    edges = {spec.version: list(spec.depends_on) for spec in specs}
    problems = []

    def walk(node, trail):
        if node in trail:
            cycle = trail[trail.index(node):] + [node]
            problems.append('dependency cycle: %s' % ' -> '.join(cycle))
            return
        for dependency in edges.get(node, ()):
            if dependency in edges:
                walk(dependency, trail + [node])

    for spec in specs:
        walk(spec.version, [])
    # De-duplicate: a cycle reports once per member that starts a walk into it.
    return list(dict.fromkeys(problems))
