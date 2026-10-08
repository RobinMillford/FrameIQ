"""Data Portability Import Center — HTTP layer (Task F6).

    POST /api/account/import/<source>/preview   classify, writes NOTHING
    POST /api/account/import/<source>/apply     the only operation that writes

Both are POST-only, authenticated, CSRF-protected by the app's existing
``CSRFProtect``, rate-limited through the shared ``extensions.limiter``, and
strictly scoped to the current session user. There is no ``user_id`` parameter
and no path segment that could name another account: the user is
``current_user``, full stop.

Why POST for a read-only preview: the file arrives as multipart body, and a
GET cannot carry one. It also keeps preview and apply visibly identical in
shape, so the CSRF guard cannot be forgotten on one of them.

Failures are 4xx with a safe reason. A malformed upload is a user error, not a
500, and the response never echoes server internals or another user's data.
"""
import logging

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from api.imports import SOURCES, apply_import, preview, summary_payload
from api.imports.uploads import UploadRejected
from extensions import limiter
from routes._main_bp import main

logger = logging.getLogger(__name__)

# An import parses and can write thousands of rows, so it is far heavier than
# a normal request. "5 per minute; 20 per hour" is an existing repo policy
# (routes/auth.py uses the same shape for /profile/recommendations) and leaves
# room for a preview/apply pair plus a retry.
IMPORT_RATE_LIMIT = "5 per minute; 20 per hour"

_PRIVATE = {'Cache-Control': 'private, no-store, max-age=0',
            'Pragma': 'no-cache'}

# Server-side backstop. Flask-WTF's MAX_FORM_MEMORY_SIZE is 500 KB, which is
# plenty for our small control fields, but Flask's own
# MAX_CONTENT_LENGTH (5 MB) is what really bounds a multipart body. Read the
# live config rather than hard-coding, so tightening one tightens both.


def _max_upload_bytes():
    configured = current_app.config.get('MAX_CONTENT_LENGTH')
    if configured:
        return int(configured)
    return 5 * 1024 * 1024


def _read_upload():
    """Pull the uploaded file into memory with a hard bound.

    ``request.files`` is already bounded by MAX_CONTENT_LENGTH, so a hostile
    multi-gigabyte body is refused by Werkzeug before this runs. The explicit
    length check is a second, self-documenting gate: it does not depend on
    that config staying where it is.
    """
    upload = request.files.get('file')
    if upload is None:
        raise UploadRejected('No file was uploaded.')
    filename = upload.filename or ''
    payload = upload.read(_max_upload_bytes() + 1)
    if len(payload) > _max_upload_bytes():
        raise UploadRejected('That file is too large.', status=413)
    return filename, payload


def _validate_source(source):
    if source not in SOURCES:
        raise UploadRejected('Unknown import source.', status=404)
    return source


def _handle(fn):
    """Shared error mapping: 4xx + safe reason, never a 500 with detail."""
    try:
        report = fn()
    except UploadRejected as exc:
        # Covers ImportRejected too (it subclasses UploadRejected).
        return jsonify({'error': exc.reason}), exc.status
    except ValueError as exc:
        # A structurally invalid source document (e.g. "shows" is not a list).
        logger.info('Import source structure rejected: %s', exc)
        return jsonify({'error': str(exc)[:200]}), 400
    except Exception:
        logger.exception('Import failed unexpectedly')
        return jsonify({'error': 'We could not process that file.'}), 500
    response = jsonify(report)
    response.headers.update(_PRIVATE)
    return response


@main.route('/api/account/import/<source>/preview', methods=['POST'])
@login_required
@limiter.limit(IMPORT_RATE_LIMIT)
def import_preview(source):
    """Classify an import without writing anything."""
    def _run():
        _validate_source(source)
        filename, payload = _read_upload()
        return summary_payload(
            preview(current_user.id, source, filename, payload))
    return _handle(_run)


@main.route('/api/account/import/<source>/apply', methods=['POST'])
@login_required
@limiter.limit(IMPORT_RATE_LIMIT)
def import_apply(source):
    """Apply an import. The only endpoint that writes user data."""
    def _run():
        _validate_source(source)
        filename, payload = _read_upload()
        return summary_payload(
            apply_import(current_user.id, source, filename, payload))
    return _handle(_run)