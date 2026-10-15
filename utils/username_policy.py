"""Username character policy (Feature F35).

Why this exists
---------------
``User.username`` is ``db.String(80)`` and, until F35, registration applied no
character validation at all — only a uniqueness check. A username could
therefore contain a double quote, and several render paths interpolated that
value into a quoted HTML attribute. A quote terminated the attribute early and
the remainder of the string was parsed as live markup, which is a stored XSS
reachable by anyone who can register.

Those render paths are now fixed independently (see ``static/js/dom-safety.js``
and the F35 changes to the feed renderers). This module is **defense in depth**,
not the primary control: it removes the ability to *store* a value that would
break out of a context in the first place.

Scope and intent
----------------
The rule is deliberately narrow — it rejects exactly the characters that enable
HTML or attribute injection, plus characters that have no legitimate place in a
URL path component:

* ``<`` ``>``  — tag delimiters
* ``"`` ``'``  — quoted-attribute delimiters
* ``&``        — HTML entity delimiter
* whitespace and C0/DEL control characters — ambiguous inside a path component

It is an allow-list of *rejected* characters rather than an allow-list of
accepted ones, so existing usernames containing letters, digits, ``.``, ``_``,
``-`` and non-ASCII characters are unaffected.

Existing accounts
-----------------
This policy is applied only where a username is **created** (registration and
OAuth provisioning). No migration runs, no column changes, and no existing
account is renamed, disabled or invalidated. A username stored before F35 that
would fail this rule keeps working: the render paths that would have broken on
it are already safe. New registrations and new OAuth logins are checked.
"""

#: Characters that must never appear in a username.
#: ``<`` ``>`` close a tag, ``"`` ``'`` close a quoted attribute, ``&`` opens an
#: HTML entity — which is what makes an encoded payload able to decode itself
#: back into markup at render time.
FORBIDDEN_CHARS = frozenset('<>"\'' '&')


def _forbidden_set():
    """FORBIDDEN_CHARS plus whitespace and control characters."""
    chars = set(FORBIDDEN_CHARS)
    chars.update(' \t\n\r\v\f')
    for code in list(range(0x00, 0x20)) + [0x7f]:
        chars.add(chr(code))
    return frozenset(chars)


_ALL_FORBIDDEN = _forbidden_set()

#: The column is String(80); keep the value inside it.
MAX_LENGTH = 80
MIN_LENGTH = 1


def username_problems(value):
    """Every reason ``value`` is not an acceptable username.

    Returns a list of human-readable strings — empty means valid. Callers can
    show the list directly, so a rejected registration explains itself.
    """
    problems = []
    if not isinstance(value, str):
        return ['Username must be text.']

    if len(value) < MIN_LENGTH:
        problems.append('Username cannot be empty.')
    elif len(value) > MAX_LENGTH:
        problems.append('Username must be %d characters or fewer.' % MAX_LENGTH)

    bad = sorted({c for c in value if c in _ALL_FORBIDDEN})
    if bad:
        problems.append(
            'Username cannot contain: %s. These characters are used to build '
            'links and can break the page markup.'
            % ' '.join(repr(c) for c in bad))

    return problems


def is_valid_username(value):
    """True when ``value`` satisfies the policy."""
    return not username_problems(value)


def unique_valid_username(candidate, exists, fallback='user'):
    """Return a policy-compliant username that ``exists`` reports as free.

    Used by OAuth provisioning, where the username is derived from an email
    address and therefore cannot simply be rejected — an unusable provider
    value must not break someone's sign-in.

    ``exists`` is a callable taking a candidate username and returning True
    when it is already taken.
    """
    base = candidate if is_valid_username(candidate) else fallback
    base = base[:MAX_LENGTH]
    if not exists(base):
        return base

    counter = 1
    while True:
        suffix = '_%d' % counter
        trimmed = base[:MAX_LENGTH - len(suffix)]
        attempt = trimmed + suffix
        if not exists(attempt):
            return attempt
        counter += 1
