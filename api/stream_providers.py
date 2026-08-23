"""
Stream provider registry — builds embed URLs for multiple streaming sources.

Providers are ordered by preference; the first entry is the default source.
All providers are keyed by TMDb IDs.
"""
import os

# ── Provider definitions ─────────────────────────────────────────────────────
# Each provider declares:
#   label    — human-readable name shown in the source switcher
#   base     — embed host (also used for postMessage origin allowlisting)
#   movie    — URL template for movies ({base} and {tmdb_id})
#   tv       — URL template for TV ({base}, {tmdb_id}, {season}, {episode})

RIVESTREAM = 'https://www.rivestream.app'

PROVIDERS = {
    'rive': {
        'label': 'Server 1 · Rive',
        'base': RIVESTREAM,
        'movie': '{base}/embed?type=movie&id={tmdb_id}',
        'tv': '{base}/embed?type=tv&id={tmdb_id}&season={season}&episode={episode}',
        # undocumented; harmless if ignored
        'resume_param': 't',
    },
    'vidking': {
        'label': 'Server 2 · VidKing',
        'base': 'https://www.vidking.net',
        'movie': '{base}/embed/movie/{tmdb_id}',
        'tv': '{base}/embed/tv/{tmdb_id}/{season}/{episode}',
        'resume_param': 't',  # documented convention
    },
    'vidy': {
        'label': 'Server 3 · Vidy',
        # apex 301-redirects to www — point straight at the final host
        'base': 'https://www.vidy.st',
        'movie': '{base}/movie/{tmdb_id}',
        'tv': '{base}/tv/{tmdb_id}/{season}/{episode}',
        'resume_param': 'progress',  # documented: start at N seconds
    },
    'oneembed': {
        'label': 'Server 4 · 1Embed',
        'base': 'https://1embed.cc',
        'movie': '{base}/embed/movie/{tmdb_id}',
        'tv': '{base}/embed/tv/{tmdb_id}/{season}/{episode}',
        # no documented resume param
    },
}

DEFAULT_PROVIDER = os.getenv('STREAM_PROVIDER', 'rive')

# Origins trusted for player postMessage progress events
ALLOWED_ORIGINS = sorted({p['base'] for p in PROVIDERS.values()})


def build_embed_url(provider_key, media_type, tmdb_id,
                    season=None, episode=None, resume_time=0):
    """Build an embed URL for the given provider and media.

    Returns None if the provider is unknown or required params are missing.
    """
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return None

    if media_type == 'tv':
        if season is None or episode is None:
            return None
        url = provider['tv'].format(
            base=provider['base'], tmdb_id=int(tmdb_id),
            season=int(season), episode=int(episode),
        )
    else:
        url = provider['movie'].format(
            base=provider['base'], tmdb_id=int(tmdb_id),
        )

    if resume_time > 0:
        param = provider.get('resume_param', 't')
        separator = '&' if '?' in url else '?'
        url += f'{separator}{param}={int(resume_time)}'
    return url


def get_sources(media_type, tmdb_id, season=None, episode=None, resume_time=0):
    """Return the full ordered source list with pre-built URLs.

    The default provider comes first; every other provider follows in
    registry order so templates can render a fallback switcher directly.
    """
    default_key = (
        DEFAULT_PROVIDER if DEFAULT_PROVIDER in PROVIDERS else next(iter(PROVIDERS))
    )
    ordered_keys = [default_key] + [k for k in PROVIDERS if k != default_key]

    sources = []
    for key in ordered_keys:
        sources.append({
            'key': key,
            'label': PROVIDERS[key]['label'],
            'url': build_embed_url(
                key, media_type, tmdb_id, season, episode, resume_time
            ),
        })
    return [s for s in sources if s['url']]
