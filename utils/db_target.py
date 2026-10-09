"""Single source of truth for deciding which database a command may touch.

Why this exists
---------------
Schema-writing commands (migrations, baseline adoption, the development
bootstrap) run against whatever ``DATABASE_URL`` says. During Task F7 a
diagnostic ``python -c "from app import app ..."`` executed with a production
``DATABASE_URL`` and attempted DDL against Neon. It rolled back, but reaching a
live database through a module import is a hazard that must be designed out.

Earlier guards lived outside the repository and were lost when ``/tmp`` was
cleared -- which is not a defence, it is an accident waiting to happen. This
module lives in the repository, is imported by every schema-writing command,
and is the only place the decision is made.

The bypass rule
---------------
There is **no environment variable that disables this guard**. Production
access requires an explicit keyword argument passed by an operator running an
explicit command:

    allow_production=True

An alternative configuration variable would let an unexpected startup path
re-enable production writes without anyone choosing to. A keyword argument
cannot be set by importing a module, so the only way to get it is to run the
command on purpose.

Classification is not a single ``"production" in hostname`` test. A remote
PostgreSQL on an unrecognised host is treated as production-like by default,
so a new vendor hostname is protected until someone classifies it.
"""
import os
from dataclasses import dataclass
from urllib.parse import urlparse

# Hosts that are unambiguously local and safe for scratch/test work.
_LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'})

# Known managed-database host markers. These make the refusal message specific
# rather than a generic "that looks remote".
_MANAGED_HOST_MARKERS = (
    'neon.tech', 'neon.build', 'amazonaws.com', 'rds.amazonaws', 'supabase.co',
    'azure.com', 'cloudsql', 'digitalocean.com', 'cockroachlabs.com',
)


class UnsafeTargetError(RuntimeError):
    """The requested database target is not permitted for this operation.

    Carries an operator-readable message; never contains credentials.
    """


@dataclass(frozen=True)
class DbTarget:
    """A classified database target. Never holds a usable password."""

    raw: str
    scheme: str
    host: str | None
    database: str | None
    user: str | None

    @property
    def is_sqlite(self):
        return self.scheme.startswith('sqlite')

    @property
    def is_local(self):
        """A file, or a loopback host. Safe for scratch/test work."""
        if self.is_sqlite:
            return True
        return (self.host or '').lower() in _LOCAL_HOSTS

    @property
    def is_managed(self):
        lowered = (self.host or '').lower()
        return any(marker in lowered for marker in _MANAGED_HOST_MARKERS)

    @property
    def is_production_like(self):
        """Anything that is not obviously local.

        A remote PostgreSQL on an unrecognised host counts, so a hostname this
        module has never seen is protected rather than assumed safe.
        """
        if self.is_sqlite:
            return False
        if not self.raw:
            return True
        return not self.is_local

    def safe_identity(self):
        """A printable identity with the password removed.

        Used in every log line and error message, so a migration transcript can
        be pasted into an issue without leaking credentials.
        """
        if not self.raw:
            return '<unset>'
        if self.is_sqlite:
            # sqlite:///path — no credentials exist, but keep only the path.
            return 'sqlite:%s' % (self.database or '')
        host = self.host or '<no-host>'
        port = ''
        try:
            parsed = urlparse(self.raw)
            if parsed.port:
                port = ':%d' % parsed.port
        except (TypeError, ValueError):
            pass
        return '%s://%s%s/%s' % (self.scheme, host, port,
                                 self.database or '<no-db>')


def classify(url):
    """Classify a database URL without opening a connection."""
    if not url:
        return DbTarget(raw='', scheme='', host=None, database=None, user=None)
    parsed = urlparse(url)
    # SQLite URLs put the path in `path`; for postgres the database is also in
    # `path` but after a leading slash.
    database = (parsed.database if hasattr(parsed, 'database')
                else None) or (parsed.path or '').lstrip('/') or None
    return DbTarget(raw=url, scheme=parsed.scheme or '',
                    host=parsed.hostname, database=database,
                    user=parsed.username)


def resolve_url(explicit=None):
    """The database URL a command should use.

    ``explicit`` (e.g. a ``--database-url`` flag) wins over the environment so a
    command can be pointed somewhere specific without mutating global state.
    """
    return explicit or os.environ.get('DATABASE_URL', '')


def require_writable_target(purpose, explicit_url=None, allow_production=False):
    """Return a classified target, or refuse.

    Used by every command that can WRITE schema. Local targets are always
    allowed. Remote targets require ``allow_production=True``, which only an
    operator passing it to an explicit command can supply.
    """
    url = resolve_url(explicit_url)
    target = classify(url)

    if not target.raw:
        raise UnsafeTargetError(
            'No DATABASE_URL is configured, so %s cannot run. Refusing rather '
            'than guessing at a target.' % purpose)

    if target.is_production_like and not allow_production:
        kind = 'managed database' if target.is_managed else 'remote database'
        raise UnsafeTargetError(
            'Refusing to run %s against a %s (%s).\n'
            '  Schema-writing commands must be pointed at a local database '
            'unless an operator explicitly passes allow_production.\n'
            '  There is no environment variable that unlocks this: if a '
            'process can reach here without choosing to, it is a bug.'
            % (purpose, kind, target.safe_identity()))

    return target


def require_scratch_target(purpose, explicit_url=None):
    """Return a classified target only if it is obviously local.

    Stricter than :func:`require_writable_target` with no override at all. This
    is for test and development tooling, where there is never a legitimate
    reason to touch a remote database -- so there is no way to ask for one.
    """
    target = require_writable_target(purpose, explicit_url)
    if not target.is_local:
        raise UnsafeTargetError(
            'Refusing to run %s: it requires a local scratch database, and '
            '%s is remote. Test tooling must never reach a real database.'
            % (purpose, target.safe_identity()))
    return target
