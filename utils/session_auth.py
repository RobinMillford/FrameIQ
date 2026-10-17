"""F55 — versioned session / remember-me identity.

Why this exists
---------------
Flask's default session is a **client-side signed cookie** and Flask-Login's
remember-me cookie is likewise a signed payload holding nothing but the user
id. Neither is recorded server-side, so neither can be "deleted" when a
password is reset: there is simply nothing on the server to remove.

The one server-side handle that exists is a column on the user. This module
puts the value of that column *inside the identity string* that both cookies
already carry, so a password reset that bumps the column invalidates every
outstanding session and remember-me cookie at once.

How the identity string works
-----------------------------
Flask-Login writes the remember cookie from ``str(session['_user_id'])`` and
reads it back through the ``user_loader`` callback. Both the session cookie and
the remember cookie therefore travel through the same field, so versioning that
one string versions both credentials:

    "42"        -> legacy, issued before this change. REJECTED.
    "42|3"      -> user 42, session_version 3.

Legacy values are rejected rather than upgraded. Accepting them would mean a
cookie minted before a reset stays usable, which is precisely the property
this is meant to remove. The cost is that deploying this logs everyone out
once; that is the intended, fail-closed behaviour.

``parse_session_identity`` returns ``None`` for anything malformed or for a
version that no longer matches, and the caller (the ``user_loader`` in
``app.py``) then treats the request as unauthenticated.
"""
from __future__ import annotations

# Bumped with the F55 deployment; only ever increased.
IDENTITY_VERSION = 1


def session_identity(user) -> str:
    """The versioned identity string for ``user``.

    This is ``User.get_id()``, and it is the single source of that value:
    Flask-Login writes ``session['_user_id']`` from it and mints the
    remember-me cookie from the same field, so both credentials are versioned
    without any call site having to remember to do it.

    ``getattr`` rather than a bare attribute read so a process that reaches
    this before migration 0006 has run degrades to version 0 instead of
    raising on every login.
    """
    version = int(getattr(user, 'session_version', 0) or 0)
    return '%d|%d' % (int(user.id), version)


def parse_session_identity(identity) -> tuple[int, int] | None:
    """Split an identity string into ``(user_id, session_version)``.

    Returns ``None`` for anything that is not a well-formed ``"<int>|<int>"``
    pair, including the bare legacy form. Callers must treat ``None`` as
    "not authenticated" rather than falling back to a partial parse.
    """
    if not isinstance(identity, str):
        return None
    user_id, sep, version = identity.rpartition('|')
    if not sep:
        return None
    try:
        return int(user_id), int(version)
    except (TypeError, ValueError):
        return None


def bump_session_version(user) -> int:
    """Raise the user's session version, revoking every issued credential.

    Callers must ``db.session.commit()``. Returns the new value.
    """
    current = int(getattr(user, 'session_version', 0) or 0)
    new_value = current + 1
    user.session_version = new_value
    return new_value