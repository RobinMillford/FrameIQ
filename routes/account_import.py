"""Data Portability Import Center — HTTP layer (Tasks F6, F7).

    POST   /api/account/import/<source>/preview          classify, writes NOTHING
    POST   /api/account/import/<source>/apply            the only write path
    POST   /api/account/import/<source>/search           explicit local title search
    GET    /api/account/import/mappings                  this user's mappings
    POST   /api/account/import/mappings                  create/update one mapping
    DELETE /api/account/import/mappings/<int:mapping_id> remove one mapping

Every mutation is POST or DELETE, authenticated, CSRF-protected by the app's
existing ``CSRFProtect``, rate-limited through the shared
``extensions.limiter``, and strictly scoped to the current session user. There
is no ``user_id`` parameter anywhere and no path segment that could name
another account: the user is ``current_user``, full stop. A mapping id from
another account is deleted by a WHERE clause that includes ``user_id``, so it
matches nothing rather than deleting someone else's row.

Why POST for a read-only preview: the file arrives as multipart body, and a GET
cannot carry one. It also keeps preview and apply visibly identical in shape,
so the CSRF guard cannot be forgotten on one of them.

Failures are 4xx with a safe reason. A malformed upload is a user error, not a
500, and the response never echoes server internals or another user's data.
"""
import json
import logging

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from api.imports import (SOURCES, MediaIndex, apply_import, delete_mapping,
                         list_mappings, media_payload, preview, save_mapping,
                         search_local, summary_payload)
from api.imports.mappings import MappingError
from api.imports.uploads import UploadRejected
from extensions import limiter
from models import db
from routes._main_bp import main

logger = logging.getLogger(__name__)

# An import parses and can write thousands of rows, so it is far heavier than
# a normal request. "5 per minute; 20 per hour" is an existing repo policy
# (routes/auth.py uses the same shape for /profile/recommendations) and leaves
# room for a preview/apply pair plus a retry.
IMPORT_RATE_LIMIT = "5 per minute; 20 per hour"

# Candidate search and mapping edits are cheap local reads/writes, but they are
# still user-triggered endpoints, so they get their own tighter budget rather
# than riding the import allowance (which is spent per file, not per click).
RESOLVE_RATE_LIMIT = "30 per minute; 200 per hour"

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


def _selections_from_form():
    """Read the user's per-title choices from the multipart form.

    Sent as a JSON object rather than repeated form fields so a 500-row import
    does not depend on request parsing limits for repeated keys, and so the
    value can be validated as one structure. An absent or blank field simply
    means "no choices yet", which is the normal first-preview case.
    """
    raw = request.form.get('selections')
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise UploadRejected('The selections you sent were not valid JSON.',
                             status=400)
    if not isinstance(parsed, dict):
        raise UploadRejected('Selections must be an object.', status=400)
    # Bound the number of choices so a huge body cannot turn one request into
    # an unbounded loop of media lookups.
    if len(parsed) > 5000:
        raise UploadRejected('Too many selections in one request.', status=400)
    return parsed


def _flag(name):
    """A boolean form field, defaulting to off when absent or malformed."""
    return str(request.form.get(name, '')).strip().lower() in (
        '1', 'true', 'yes', 'on')


def _validate_source(source):
    if source not in SOURCES:
        raise UploadRejected('Unknown import source.', status=404)
    return source


def _handle(fn):
    """Shared error mapping: 4xx + safe reason, never a 500 with detail."""
    try:
        report = fn()
    except UploadRejected as exc:
        # Covers ImportRejected and SelectionRejected (both subclass it).
        return jsonify({'error': exc.reason}), exc.status
    except MappingError as exc:
        # A rejected mapping is a user error with a message worth showing.
        return jsonify({'error': str(exc)[:200]}), 400
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
    """Classify an import without writing anything.

    Accepts ``selections`` so the preview reflects the user's choices, but
    never persists them: a preview that wrote mappings would turn a look at a
    file into a durable change to the account.
    """
    def _run():
        _validate_source(source)
        filename, payload = _read_upload()
        return summary_payload(
            preview(current_user.id, source, filename, payload,
                    selections=_selections_from_form()))
    return _handle(_run)


@main.route('/api/account/import/<source>/apply', methods=['POST'])
@login_required
@limiter.limit(IMPORT_RATE_LIMIT)
def import_apply(source):
    """Apply an import. The only endpoint that writes user data.

    ``save_mappings`` persists a mapping for every identity that resolved. It is
    opt-in: freezing a heuristic match into a permanent override should be a
    choice, not a side effect of importing.
    """
    def _run():
        _validate_source(source)
        filename, payload = _read_upload()
        return summary_payload(
            apply_import(current_user.id, source, filename, payload,
                         selections=_selections_from_form(),
                         save_mappings=_flag('save_mappings')))
    return _handle(_run)


@main.route('/api/account/import/<source>/search', methods=['POST'])
@login_required
@limiter.limit(RESOLVE_RATE_LIMIT)
def import_search(source):
    """Explicit, user-initiated LOCAL title search.

    Deliberately offline: this searches titles already in FrameIQ and nothing
    else. A film that has never been added has no candidates, and saying so is
    more honest than silently reaching for a third-party API during an import.
    """
    def _run():
        _validate_source(source)
        payload = request.get_json(silent=True) or {}
        query = str(payload.get('query') or '').strip()
        if not query:
            raise UploadRejected('Type something to search for.', status=400)
        if len(query) > 200:
            raise UploadRejected('That search is too long.', status=400)
        media_type = payload.get('media_type')
        if media_type not in (None, '', 'movie', 'tv'):
            raise UploadRejected('That is not a valid title type.', status=400)
        media_type = media_type or None
        index = MediaIndex()
        results = search_local(index, query, media_type)
        return {
            'query': query,
            'results': [media_payload(item) for item in results],
        }
    return _handle(_run)


@main.route('/api/account/import/mappings', methods=['GET'])
@login_required
@limiter.limit(RESOLVE_RATE_LIMIT)
def import_mappings_list():
    """List this user's mappings, including any that have gone stale."""
    def _run():
        source = request.args.get('source')
        if source and source not in SOURCES:
            raise UploadRejected('Unknown import source.', status=404)
        return {'mappings': list_mappings(current_user.id, source)}
    return _handle(_run)


@main.route('/api/account/import/mappings', methods=['POST'])
@login_required
@limiter.limit(RESOLVE_RATE_LIMIT)
def import_mappings_save():
    """Create or update one mapping.

    The user is never sent as a parameter: the mapping is written for
    ``current_user`` no matter what the body claims.
    """
    def _run():
        payload = request.get_json(silent=True) or {}
        source = payload.get('source')
        if source not in SOURCES:
            raise UploadRejected('Unknown import source.', status=400)
        media_type = payload.get('media_type')
        if media_type not in ('movie', 'tv'):
            raise UploadRejected('A mapping must target a movie or a TV show.',
                                 status=400)
        source_key = payload.get('source_key')
        if not isinstance(source_key, str) or not source_key.strip():
            raise UploadRejected('A mapping needs a source key.', status=400)
        try:
            media_id = int(payload.get('media_id'))
        except (TypeError, ValueError):
            raise UploadRejected('That selection has no valid title.',
                                 status=400)
        source_title = payload.get('source_title')
        if source_title is not None and not isinstance(source_title, str):
            source_title = None
        mapping = save_mapping(current_user.id, source, media_type,
                               source_key.strip(), media_id,
                               source_title=source_title)
        db.session.commit()
        return {'mapping': {
            'id': mapping.id,
            'source': mapping.source,
            'media_type': mapping.media_type,
            'source_key': mapping.source_key,
            'source_title': mapping.source_title,
            'media_id': mapping.media_id,
        }}
    return _handle(_run)


@main.route('/api/account/import/mappings/<int:mapping_id>',
            methods=['DELETE'])
@login_required
@limiter.limit(RESOLVE_RATE_LIMIT)
def import_mappings_delete(mapping_id):
    """Remove one of the current user's mappings.

    This removes import bookkeeping only. No diary entry, review or watched
    episode is touched: those are the user's history and outlive the mapping
    that happened to create them.
    """
    def _run():
        delete_mapping(current_user.id, mapping_id)
        db.session.commit()
        return {'deleted': mapping_id}
    return _handle(_run)