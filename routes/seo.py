"""SEO routes: robots.txt and a bounded sitemap.

The sitemap intentionally contains only static, curated URLs. It never
enumerates TMDB IDs — doing so would create millions of crawlable expensive
detail URLs.
"""
from flask import Blueprint, Response, request

seo_bp = Blueprint('seo', __name__)


@seo_bp.route('/robots.txt')
def robots():
    """Disallow API and watch pages; keep public detail pages crawlable."""
    lines = [
        "User-agent: *",
        "Disallow: /api/",
        "Disallow: /watch/",
        "",
        "Sitemap: %ssitemap.xml" % request.url_root,
    ]
    return Response("\n".join(lines) + "\n", mimetype='text/plain')


@seo_bp.route('/sitemap.xml')
def sitemap():
    """Bounded, curated sitemap of stable public pages."""
    root = request.url_root.rstrip('/')
    static_paths = [
        '/',
        '/movies',
        '/tv_shows',
        '/news',
        '/discover',
        '/login',
        '/register',
    ]
    today = __import__('datetime').date.today().isoformat()
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path in static_paths:
        body.append(
            f'  <url><loc>{root}{path}</loc><lastmod>{today}</lastmod></url>'
        )
    body.append('</urlset>')
    return Response("\n".join(body) + "\n", mimetype='application/xml')
