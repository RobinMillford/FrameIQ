"""Offline / local-diagnostic boundary (Feature F20).

Why this exists
---------------
``app.py`` runs, at MODULE SCOPE:

    load_dotenv()            # app.py:79  — fills DATABASE_URL from .env
    app = create_app()       # app.py:305 — runs on ANY `import app`
        ... ensure_schema_compatible(app)   # app.py:298 — opens a REAL connection

So the moment any diagnostic, scratch script, or shell one-liner does
``import app``, the process inherits ``.env``'s ``DATABASE_URL`` and opens a
connection to whatever that points at — in practice the production database —
before a single line of the diagnostic runs. The schema guard then reports
production drift (read-only, but still a live production connection).

``tests/conftest.py`` already solves this for pytest by setting
``DATABASE_URL`` to a throwaway SQLite file *before* any app import; that works
because ``python-dotenv`` defaults to ``override=False`` and therefore never
clobbers a variable that is already set. Nothing, however, extends that
protection to local diagnostics, which is the gap this module closes.

What this module does NOT do
----------------------------
* It does not weaken, reorder, or bypass the production schema guard. The
  ``SKIP_SCHEMA_GUARD`` escape hatch is untouched and still required for a
  freshly-created local database to boot.
* It does not change ``create_app()``, the WSGI entrypoint, or any deployment
  configuration. Production startup behaves exactly as before.
* It does not decide safety by hostname alone. ``is_local_database_url``
  requires a recognised local scheme AND, for networked drivers, a loopback
  host — anything else is treated as remote.
* It is OPT-IN. It protects a diagnostic that calls it BEFORE importing the
  application. No helper can protect an ``import app`` that has already run,
  because that import is what performs the connection — closing that gap for
  everyone would require restructuring ``app.py``'s module-scope startup,
  which is deliberately out of scope and needs its own review.

Usage (BEFORE any application import)::

    from utils.offline_diagnostics import use_offline_database
    use_offline_database()            # DATABASE_URL -> throwaway SQLite
    from app import create_app        # now safe: no production connection

or, to fail loudly instead of switching::

    from utils.offline_diagnostics import assert_offline_diagnostic
    assert_offline_diagnostic()       # raises unless the target is local
"""

import os
import tempfile
from urllib.parse import urlparse

__all__ = [
    'UnsafeDatabaseTargetError',
    'assert_local_database',
    'assert_offline_diagnostic',
    'describe_ambient_target',
    'is_local_database_url',
    'use_offline_database',
]

#: Drivers that are local by construction — no host, nothing to reach out to.
_LOCAL_SCHEMES = ('sqlite', 'sqlite3', 'memory')

#: Loopback names. A networked driver is local ONLY when its host is one of
#: these, so an arbitrary hostname can never be mistaken for "local".
#:
#: ``0.0.0.0`` is deliberately ABSENT: it is INADDR_ANY, the wildcard address
#: used for binding, not a loopback destination, so treating it as "local"
#: would weaken this guard for no benefit. ``urlparse`` strips IPv6 brackets,
#: so the canonical spelling is bare ``::1``.
_LOOPBACK_HOSTS = frozenset({
    'localhost', '127.0.0.1', '::1',
})


class UnsafeDatabaseTargetError(RuntimeError):
    """A local diagnostic was pointed at a database that is not local.

    Raised instead of connecting. Carries no part of the URL it refused, so a
    traceback cannot leak a password.
    """


def _scheme_and_host(url):
    """(scheme, hostname) without ever returning credentials."""
    text = (url or '').strip()
    if not text:
        return '', ''
    if text.startswith('sqlite:'):
        return 'sqlite', ''
    try:
        parsed = urlparse(text)
    except ValueError:
        return '', ''
    return (parsed.scheme or '').lower(), (parsed.hostname or '').lower()


def is_local_database_url(url):
    """True only when ``url`` provably points at a LOCAL database.

    Local means either a SQLite/file or memory database, or a networked driver
    bound to loopback. Everything else — including a blank URL and anything
    unparseable — is treated as REMOTE, so an unknown value fails closed.
    """
    scheme, host = _scheme_and_host(url)
    if not scheme:
        return False
    if scheme in _LOCAL_SCHEMES:
        return True
    return host in _LOOPBACK_HOSTS


def assert_local_database(url, *, context='diagnostic'):
    """Raise unless ``url`` is a local database.

    The URL is never echoed into the exception.
    """
    if not is_local_database_url(url):
        raise UnsafeDatabaseTargetError(
            '%s refused: the configured DATABASE_URL is not a local '
            'database. Point it at SQLite or a loopback host before running '
            'a local diagnostic, so no remote database is contacted.'
            % context)


def _env_file_database_url():
    """The DATABASE_URL declared in .env, or ''.

    Only the value is returned; callers must classify it and must never log
    it. This parses the file to learn WHICH database local tooling would
    inherit — it does not read or expose any credential.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '.env')
    try:
        with open(path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith('DATABASE_URL='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ''
    return ''


def describe_ambient_target():
    """Report (never print a URL) what database local tooling would inherit.

    Returns a dict with booleans and a bare host. No password, no username,
    no connection string is included, so the result is safe to log.
    """
    env_url = os.environ.get('DATABASE_URL', '')
    file_url = _env_file_database_url()
    _, env_host = _scheme_and_host(env_url)
    _, file_host = _scheme_and_host(file_url)
    return {
        'env_set': bool(env_url),
        'env_is_local': is_local_database_url(env_url),
        'env_host': env_host,
        'dotenv_set': bool(file_url),
        'dotenv_is_local': is_local_database_url(file_url),
        'dotenv_host': file_host,
    }


def assert_offline_diagnostic():
    """Fail loudly unless the CURRENT process is already on a local database.

    The check for a local diagnostic that forgot to call
    :func:`use_offline_database` first — which is the mistake F20 exists to
    catch.

    LIMITATION, stated plainly: this is an OPT-IN boundary, not a universal
    guard. It can only see a process that has not yet imported the application,
    because ``import app`` itself performs the connection. There is no
    parameter that relaxes it: a local diagnostic either points at a local
    database or raises.
    """
    assert_local_database(
        os.environ.get('DATABASE_URL', ''),
        context='offline diagnostic (call use_offline_database() BEFORE '
                'importing app)')


def use_offline_database(url=None):
    """Point this process at a LOCAL database, before importing the app.

    ``python-dotenv`` defaults to ``override=False``, so a variable set here
    survives ``load_dotenv()`` and ``create_app()`` cannot silently redirect
    the process to the database declared in ``.env``.

    Returns the local URL that was configured.
    """
    if url is None:
        fd, path = tempfile.mkstemp(prefix='frameiq_offline_', suffix='.db')
        os.close(fd)
        url = 'sqlite:///%s' % path
    assert_local_database(url, context='use_offline_database')
    os.environ['DATABASE_URL'] = url
    # A freshly created SQLite file has none of the declared tables, so the
    # read-only startup guard would refuse to boot. This is the same explicit,
    # pre-existing escape hatch tests and migrations use — it does not weaken
    # the guard for any real deployment, where it is never set.
    os.environ['SKIP_SCHEMA_GUARD'] = '1'
    os.environ.setdefault('SECRET_KEY', 'offline-diagnostic-secret-key')
    os.environ.setdefault('TMDB_API_KEY', 'offline-diagnostic-tmdb-key')
    return url