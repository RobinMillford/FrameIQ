"""Account data export endpoints (Task F5).

    GET /api/account/export/json   → application/json   (complete export)
    GET /api/account/export/csv    → application/zip    (per-domain CSVs)

Design notes
------------
**GET, read-only.** Export mutates nothing: no progress update, no watched
date write, no notification, no profile touch. ``tests/test_account_export.py``
asserts the DB session stays clean across a full export. CSRF is therefore
not required — Flask-WTF only guards unsafe methods, and there is no unsafe
method here.

**Current session user only.** Both routes read ``current_user`` and pass that
object straight into the serializer. There is no ``user_id`` query parameter,
no path segment, and no admin override, so there is no code path that can
export another account. The serializer also takes a ``User`` rather than an
id specifically so a caller cannot accidentally pass someone else's id.

**Highly private.** Every response carries ``Cache-Control: no-store`` (plus
``private`` and ``Pragma: no-cache``) so no browser, intermediary or CDN can
replay a personalized export to another user. nginx in this repo has no
active ``proxy_cache`` (all of it is commented out in nginx/nginx.conf), so
application headers alone are sufficient and no infrastructure change is
warranted.

**Bounded and logged without payload.** One ``logger.info`` per export naming
the format, row count and duration. Never the payload — no email, no review
text, no notes, no CSV bytes.

**Failure is safe.** A generation error returns a generic 500 to the user and
logs the diagnostic server-side; a partially written ZIP is unlinked rather
than served.
"""
import logging
import os
import time

from flask import current_app, send_file
from flask_login import current_user, login_required

from api.account_export import (build_csv_bundle_zip, build_export,
                                export_filename_json, serialize_json)
from extensions import limiter
from routes._main_bp import main

logger = logging.getLogger(__name__)

# Export reads every row the user owns, so it is far heavier than a normal
# page render. This matches an existing repo policy (routes/auth.py uses the
# same shape for /profile/recommendations) and still allows a user to pull the
# JSON and the bundle in one sitting.
EXPORT_RATE_LIMIT = "5 per minute; 20 per hour"

# Applied to every export response.
_PRIVATE_HEADERS = {
    'Cache-Control': 'private, no-store, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0',
}


def _log_export(fmt, row_count, elapsed_ms, user_id):
    """One line per export: format, size, duration, user id.

    Row COUNT is not payload; email/review text/notes/CSV bytes never appear.
    """
    logger.info("account export: user=%s format=%s rows=%d duration_ms=%.1f",
                user_id, fmt, row_count, elapsed_ms)


@main.route('/api/account/export/json', methods=['GET'])
@login_required
@limiter.limit(EXPORT_RATE_LIMIT)
def export_account_json():
    """Complete machine-readable export of the current user's data."""
    started = time.perf_counter()
    try:
        export = build_export(current_user)
        payload = serialize_json(export)
    except Exception:
        logger.exception("account export (json) failed for user %s",
                         current_user.id)
        return ("We could not generate your export. Please try again.", 500)

    elapsed_ms = (time.perf_counter() - started) * 1000
    # Row count comes from the payload we are actually shipping, so the log
    # can never disagree with the response.
    total_rows = sum(count for count in export['counts'].values()
                     if isinstance(count, int))
    _log_export('json', total_rows, elapsed_ms, current_user.id)

    response = current_app.response_class(payload, mimetype='application/json')
    response.headers['Content-Disposition'] = (
        'attachment; filename="%s"' % export_filename_json())
    response.headers.update(_PRIVATE_HEADERS)
    return response


@main.route('/api/account/export/csv', methods=['GET'])
@login_required
@limiter.limit(EXPORT_RATE_LIMIT)
def export_account_csv():
    """ZIP bundle of the per-domain CSVs plus a README describing them."""
    started = time.perf_counter()
    try:
        path, filename = build_csv_bundle_zip(current_user)
    except Exception:
        logger.exception("account export (csv) failed for user %s",
                         current_user.id)
        return ("We could not generate your export. Please try again.", 500)

    # The temp archive holds the user's entire account: delete it as soon as
    # the response is closed, and also if the client disconnects mid-transfer.
    def _cleanup(_=None):
        try:
            os.unlink(path)
        except OSError:
            logger.warning("account export: temp archive already removed",
                           exc_info=True)

    response = send_file(path, mimetype='application/zip',
                         as_attachment=True, download_name=filename,
                         conditional=False)
    response.call_on_close(_cleanup)

    # send_file sets its own Cache-Control for file responses; force the
    # privacy headers after the fact.
    response.headers.update(_PRIVATE_HEADERS)

    elapsed_ms = (time.perf_counter() - started) * 1000
    size_kb = os.path.getsize(path) // 1024 if os.path.exists(path) else 0
    _log_export('csv_bundle', size_kb, elapsed_ms, current_user.id)
    return response