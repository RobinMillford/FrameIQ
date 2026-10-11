"""F13 — share metadata helpers (Open Graph / Twitter card / JSON-LD).

Pure functions only: no database, no network, no Flask import. Everything a
template needs to build a preview is derived here so the rules are testable in
isolation and cannot drift between pages.

Deliberate omissions
--------------------
``og:image:width`` / ``og:image:height`` / ``og:image:type`` are NOT emitted.
The only image sizes FrameIQ can name are TMDb URL path segments (``w500``),
which state a width but not a verified height, and the local fallbacks are
SVG with no fixed pixel size. Crawlers render fine without them, and an
unverified dimension is worse than none — so the fields are omitted rather
than guessed.
"""
import re

#: Hosts FrameIQ is willing to advertise as a preview image. Anything else is
#: treated as untrusted and replaced by a local fallback. ``via.placeholder.com``
#: is included here ONLY so it can be rejected below: the TMDb fetchers swap in
#: a third-party placeholder when a title has no artwork, and advertising that
#: as the site's own preview image would be misleading.
_TRUSTED_IMAGE_HOSTS = ('image.tmdb.org',)
_PLACEHOLDER_HOST = 'via.placeholder.com'
_ABSOLUTE_RE = re.compile(r'^https://[^\s]+$')


def clean_text(value, limit=None):
    """Collapse whitespace and clamp length, preserving Unicode.

    Used for titles/descriptions that end up inside HTML attributes, so the
    only characters that matter are the attribute delimiters — which Jinja's
    autoescaping then handles.
    """
    if value is None:
        return ''
    text = ' '.join(str(value).split())
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def usable_image_url(url):
    """True when ``url`` is an absolute HTTPS image URL we trust.

    Rejects ``None``, empty strings, protocol-relative and relative paths
    (which would resolve against the wrong host), non-HTTPS schemes, and the
    third-party placeholder the TMDb fetchers substitute for missing art.
    """
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not _ABSOLUTE_RE.match(url):
        return False
    if _PLACEHOLDER_HOST in url:
        return False
    return any(host in url for host in _TRUSTED_IMAGE_HOSTS)


def select_image(backdrop, poster, fallback_url):
    """Prefer a backdrop, then a poster, then the local branded fallback.

    A backdrop is a better link-preview shape (wide); a poster is the next best
    thing; the fallback is a stable asset already shipped with the app.
    """
    for candidate in (backdrop, poster):
        if usable_image_url(candidate):
            return candidate.strip()
    return fallback_url


def build_description(overview, fallback, limit=200):
    """A concise description from real content, never a generic stub.

    TMDb overviews are plain text. Whitespace is collapsed and the result is
    clamped on a word boundary so the meta tag never ends mid-word.
    """
    text = clean_text(overview, limit + 40)
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    if ' ' in clipped:
        clipped = clipped[:clipped.rfind(' ')]
    return clipped.rstrip(' .,;:') + '…'


def movie_jsonld(name, description, image, url, release_date=None,
                 genre=None, director=None, cast=None):
    """schema.org/Movie built ONLY from fields the page actually has.

    No aggregateRating is ever emitted: TMDb's community score is a public
    rating and is intentionally left out rather than approximated, and a
    personal diary score must never appear here.
    """
    data = {
        '@context': 'https://schema.org',
        '@type': 'Movie',
        'name': name,
        'url': url,
    }
    if description:
        data['description'] = description
    if image:
        data['image'] = image
    if release_date:
        data['datePublished'] = release_date
    genres = [g for g in (genre or []) if g]
    if genres:
        data['genre'] = genres
    if director:
        data['director'] = {'@type': 'Person', 'name': director}
    people = [c for c in (cast or []) if c]
    if people:
        data['actor'] = [{'@type': 'Person', 'name': n} for n in people]
    return data


def tv_series_jsonld(name, description, image, url, first_air_date=None,
                     genre=None, creator=None, cast=None):
    """schema.org/TVSeries from real fields only. Same omissions as Movie."""
    data = {
        '@context': 'https://schema.org',
        '@type': 'TVSeries',
        'name': name,
        'url': url,
    }
    if description:
        data['description'] = description
    if image:
        data['image'] = image
    if first_air_date:
        data['startDate'] = first_air_date
    genres = [g for g in (genre or []) if g]
    if genres:
        data['genre'] = genres
    if creator:
        data['creator'] = {'@type': 'Person', 'name': creator}
    people = [c for c in (cast or []) if c]
    if people:
        data['actor'] = [{'@type': 'Person', 'name': n} for n in people]
    return data


def person_jsonld(name, description, image, url, job_title=None):
    """schema.org/Person for a genuinely public actor page."""
    data = {
        '@context': 'https://schema.org',
        '@type': 'Person',
        'name': name,
        'url': url,
    }
    if description:
        data['description'] = description
    if image:
        data['image'] = image
    if job_title:
        data['jobTitle'] = job_title
    return data


def webpage_jsonld(name, description, url, image=None):
    """schema.org/WebPage for the public Year-in-Review share view.

    Deliberately carries NO author, datePublished, or aggregateRating: the
    recap is derived from one person's private watch history, and publishing
    that as structured data would convert a personal aggregate into something
    that reads as a public fact.
    """
    data = {
        '@context': 'https://schema.org',
        '@type': 'WebPage',
        'name': name,
        'url': url,
    }
    if description:
        data['description'] = description
    if image:
        data['image'] = image
    return data


def names(people, limit=30):
    """Extract person names from a TMDb credits list.

    A template cannot do this inline: Jinja has no list comprehension, and the
    obvious ``[c.name for c in ...]`` is a template syntax error. Returns an
    empty list rather than None so callers can pass it straight through.
    """
    out = []
    for person in (people or [])[:limit]:
        name = person.get('name') if isinstance(person, dict) else None
        if name and isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out
