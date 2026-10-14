"""Authorization gate for the legacy ``migrates/migrate_*.py`` scripts (F22).

Why this exists
---------------
The authoritative migration runner (:mod:`scripts.migrate`) has always called
:func:`utils.db_target.require_writable_target` before opening a connection.
The older hand-written scripts in ``migrates/`` did not: each imported ``app``
at module scope, and importing ``app`` runs ``create_app()`` — which builds the
engine and runs the schema parity guard. By the time any of them reached a
statement where a guard *could* have been inserted, the process was already
connected to whatever ``DATABASE_URL`` resolved to.

So the check has to happen before the import, not after it. That is why this
exists as a callable that each legacy script invokes as its first executable
statement, ahead of ``from app import ...``.

What it guarantees
------------------
* Classification is delegated to :func:`utils.db_target.require_writable_target`
  — the single authoritative implementation. This module deliberately contains
  **no** hostname heuristic of its own, and inventing a second, weaker copy is
  exactly the failure mode being fixed.
* A remote target is refused unless the caller passes the same explicit
  ``--allow-production`` command-line flag the approved runner uses.
  There is no environment variable, dotenv value, or ``SKIP_SCHEMA_GUARD``
  setting that grants production writes.
* An unset or unparseable target **fails closed**.
* The resolved URL is never logged in full. Only
  :meth:`DbTarget.safe_identity` is ever printed, which strips credentials.

What it does NOT do
-------------------
It does not decide whether a legacy script is still needed, and it does not
run migrations. It is a gate, not a migration framework: :mod:`migrates.registry`
plus :mod:`scripts.migrate` remain authoritative for anything new.
"""
import os
import sys


def _ensure_repo_on_path():
    """Make ``utils.db_target`` importable however the script was launched.

    Some legacy scripts already insert the repository root into ``sys.path``;
    others rely on the caller's working directory. Resolving the path from this
    file's own location means the guard does not depend on ``cwd`` — which is
    the same reason :mod:`scripts.migrate` bootstraps its own path.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return repo_root


_ensure_repo_on_path()

from utils.db_target import (  # noqa: E402  (import after path bootstrap)
    UnsafeTargetError,
    require_writable_target,
)


#: Flag the approved runner also accepts. Kept as a single constant so the two
#: entrypoints cannot drift apart.
ALLOW_PRODUCTION_FLAG = '--allow-production'


def _operator_requested_production(argv=None):
    """True only when ``--allow-production`` was passed on the command line.

    Reading ``sys.argv`` (never the environment) is what makes authorization
    something a person must type. An environment variable could be set by an
    unrelated startup path, which is the failure this guard exists to prevent.
    """
    argv = sys.argv if argv is None else argv
    return ALLOW_PRODUCTION_FLAG in list(argv)


def authorize_legacy_migration(script_path=None, argv=None, echo=None):
    """Classify the configured target, or refuse before any side effect.

    Call this from the legacy script *before* ``from app import ...``, before
    constructing an engine, and before touching ``db``.

    An ``UnsafeTargetError`` raised here propagates out of module scope, so a
    refusal terminates the command with a non-zero status. That is the intended
    behaviour: a refusal that could be caught and downgraded to a warning would
    not be a refusal.

    :param script_path: usually ``__file__``; used only for the error message.
    :param argv: override for testing; defaults to the real ``sys.argv``.
    :param echo: override for testing; defaults to :func:`print`.
    :returns: the classified :class:`~utils.db_target.DbTarget`.
    :raises utils.db_target.UnsafeTargetError: when the target is remote and
        the operator did not pass ``--allow-production``.
    """
    name = os.path.basename(script_path or '<unknown script>')
    allow_production = _operator_requested_production(argv)
    target = require_writable_target(
        'the legacy migration %s' % name,
        allow_production=allow_production)

    # safe_identity() never contains a password, so this line is safe to paste
    # into an issue or a deploy transcript.
    (echo or print)('Target: %s' % target.safe_identity())
    return target


__all__ = [
    'ALLOW_PRODUCTION_FLAG',
    'UnsafeTargetError',
    'authorize_legacy_migration',
]