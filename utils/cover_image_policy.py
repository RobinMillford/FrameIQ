"""List cover image policy (Feature F39).

Why this exists
---------------
``UserList.cover_image`` is a ``db.String(500)`` written by exactly one route,
``PUT /api/lists/<id>/cover``, which stored ``data.get('cover_image')``
verbatim. Any authenticated list owner could therefore store an arbitrary
string of up to 500 characters, and it was then interpolated into an ``<img
src>``. A stored value naming an attacker-controlled host makes every visitor's
browser issue a request to that host, which discloses the visitor's IP and
referrer and can be used as a read receipt for the page.

This module validates at the request boundary so an unusable value cannot be
stored in the first place.

The contract, traced rather than assumed
----------------------------------------
* The only client that sets this field is ``static/js/list-enhancements.js``,
  which uploads a chosen file through ``FileReader.readAsDataURL`` — i.e. it
  emits a ``data:`` URL. That path cannot work: a data URL for any real image
  is far longer than the 500-character column (the client code itself says
  "not recommended for production"), and since F38 the renderer
  (``FrameIQDOM.safeImageUrlFromHosts``) refuses ``data:`` for exactly the
  reasons below. F39 therefore does **not** add ``data:`` support; it reports
  the feature as broken rather than half-supporting it.
* No server path ever populates the field automatically. List creation does not
  set it and the clone route only copies an existing value.
* ``templates/list_detail.html`` renders the stored value into ``<img src>``
  through a Jinja variable, so F38's ``${...}``-based static rule could not see
  it. ``cover_image_url`` below is a render-time filter for that site, which
  also protects rows written before this policy existed without a migration.

Accepted
--------
1. A root-relative path (``/static/images/x.jpg``). Same origin by
   construction, so no cross-origin request is possible.
2. An ``https://`` URL whose host is exactly ``image.tmdb.org`` and which
   carries no embedded credentials.

F41 — one contract, two layers
------------------------------
``safeImageUrlFromHosts`` in ``static/js/dom-safety.js`` implements the SAME
rule for rendering, and the two are held in agreement by
``test_server_and_browser_policies_agree_on_the_edge_case_corpus``. The three
points where they previously diverged, and the resolution:

* ``https://image.tmdb.org./x.jpg`` — the trailing dot used to be stripped and
  accepted here, while ``new URL(...).hostname`` keeps the dot so the renderer
  refused it. A stored-but-dead value. Now refused by both.
* ``https://image.tmdb.org:8443/...`` — refused here, but ``hostname`` excludes
  the port so the renderer accepted it. Now refused by both.
* ``/x?y.jpg`` — refused here, but the renderer's root-relative branch passed
  it through. Now refused by both.

In every case the resolution is "refuse", so neither layer can hold a value the
other will not act on. ``tmdbImageUrl`` carries the same port rule so the
shared module has one host policy rather than two.

Everything else is refused. In particular a value is not accepted merely
because it starts with ``http``: ``http://`` is refused rather than upgraded,
``data:`` and ``blob:`` are refused, and a lookalike host such as
``image.tmdb.org.evil.example`` does not match.

Defense in depth
----------------
The render paths validate too, and must keep doing so. This module removes the
ability to *store* an off-host value; it is not a replacement for validating at
the point of use, because a row may predate the policy.
"""
import re

#: The one approved remote image origin. Kept identical to the browser-side
#: allowlist in `FrameIQDOM.safeImageUrlFromHosts(['image.tmdb.org'])` so the
#: server never accepts something the renderer would refuse, nor refuses
#: something it would render.
ALLOWED_HOSTS = ('image.tmdb.org',)

#: Matches the column definition, `models/lists.py`.
MAX_LENGTH = 500

#: C0 controls, space and DEL. Browsers strip these before resolving a URL, so
#: they are the classic way to smuggle a scheme past a prefix check.
_URL_SMUGGLING_RE = re.compile(r'[\x00-\x20\x7f]')

_SCHEME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:')

#: Raised for input that is not an acceptable cover image. The route turns this
#: into a 400; the Jinja filter treats it as "render the placeholder".


class InvalidCoverImage(ValueError):
    """The value is not an acceptable list cover image."""


def _absolute_url_problems(raw):
    """Problems with a value that carries a scheme, or [] when it is fine."""
    if not raw.lower().startswith('https://'):
        return ['cover image must be https, a root-relative path, or an '
                'approved image host']

    scheme, _, rest = raw.partition('://')
    del scheme
    authority = rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]

    if '@' in authority:
        return ['cover image must not embed credentials']

    host = authority.lower()
    port = ''
    if ':' in host:
        host, _, port = host.partition(':')
    # Compare the host, not the raw authority. A trailing dot is deliberately
    # NOT stripped: `new URL('https://image.tmdb.org./x').hostname` is
    # `image.tmdb.org.`, so the renderer refuses it. Accepting it here would
    # store a value the renderer can never display. Refuse it instead of
    # normalising it, so both layers agree.
    if host not in ALLOWED_HOSTS:
        return ['cover image host is not allowed; use one of: %s'
                % ', '.join(ALLOWED_HOSTS)]
    if port and port != '443':
        # A non-default port reaches a different service on the same host.
        # Stricter than the browser-side allowlist on purpose: the server only
        # ever narrows what can be stored, so the two cannot drift into the
        # server accepting something the renderer would refuse.
        return ['cover image must not specify a port']
    return []


def _root_relative_problems(raw):
    """Problems with a root-relative value, or [] when it is fine."""
    problems = []
    if '\\' in raw:
        problems.append('cover image path must not contain a backslash')
    if '..' in raw:
        problems.append('cover image path must not contain ".."')
    if _SCHEME_RE.match(raw[1:]):
        problems.append('cover image path must not look like a scheme')
    if '?' in raw or '#' in raw:
        problems.append('cover image path must not contain a query or fragment')
    return problems


def cover_image_problems(value):
    """Return the reasons ``value`` is unacceptable; empty means acceptable.

    Returning every problem rather than the first is what lets the API give a
    client an actionable message, and lets the tests assert on a specific
    reason instead of a boolean.
    """
    if value is None:
        return ['cover image is required']
    if not isinstance(value, str):
        return ['cover image must be a string']

    raw = value.strip()
    if not raw:
        return ['cover image is required']

    # Whole-value checks run BEFORE the shape branches below, and return
    # immediately. An earlier version accumulated them into `problems` and then
    # hit an early `return` in the branch helpers, which silently discarded
    # them: "/static/a b.jpg" and "/static/a\x01b.jpg" were accepted. A smuggling
    # character defeats every host check, so it has to be settled first.
    if _URL_SMUGGLING_RE.search(raw):
        return ['cover image must not contain whitespace or control characters']

    if len(raw) > MAX_LENGTH:
        return ['cover image must be at most %d characters' % MAX_LENGTH]

    # Protocol-relative: "//host/path" resolves to another origin. Reported on
    # its own because a leading `//` matches no scheme pattern, and letting it
    # fall through would mis-describe the problem.
    if raw.startswith('//'):
        return ['protocol-relative cover images are not allowed']

    if _SCHEME_RE.match(raw):
        return _absolute_url_problems(raw)

    if raw.startswith('/'):
        return _root_relative_problems(raw)

    return ['cover image must be a root-relative path or an https URL on an '
            'approved image host']


def is_valid_cover_image(value):
    """True when ``value`` is an acceptable cover image."""
    return not cover_image_problems(value)


def normalize_cover_image(value):
    """Return the canonical stored form, or raise :class:`InvalidCoverImage`.

    Normalization is intentionally minimal — surrounding whitespace is trimmed
    and nothing else is rewritten. Re-serialising an accepted URL risks
    changing its meaning; the values that matter are already canonical because
    the column has only ever been written by this route.
    """
    problems = cover_image_problems(value)
    if problems:
        raise InvalidCoverImage('; '.join(problems))
    return value.strip()


def cover_image_url(value, fallback=''):
    """Jinja filter: the value if it is renderable, else ``fallback``.

    This is the render-time half of the policy. It is what keeps
    ``templates/list_detail.html`` safe for rows stored before this policy
    existed, without a migration or a rewrite of stored data.
    """
    try:
        return normalize_cover_image(value)
    except InvalidCoverImage:
        return fallback