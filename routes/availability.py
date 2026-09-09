"""Where-to-Watch + My Services routes (Feature 03)."""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from extensions import limiter
from api.availability import (
    all_region_providers,
    get_availability,
    match_my_services,
    normalize_region,
)
from models import db, UserStreamingService

availability_bp = Blueprint('availability', __name__)

_MAX_SERVICES = 20


def _user_service_ids(user_id, region):
    rows = (UserStreamingService.query
            .filter_by(user_id=user_id, region=region)
            .all())
    return [row.provider_id for row in rows]


@availability_bp.route('/api/media/<media_type>/<int:tmdb_id>/availability')
def media_availability(media_type, tmdb_id):
    """Normalized where-to-watch availability for a title.

    Anonymous users get plain availability; authenticated users additionally
    get their My Services match. Region: ?region= overrides, else the user's
    saved region, else the app default.
    """
    if media_type not in ('movie', 'tv'):
        return jsonify({'error': 'Invalid media type'}), 400

    region = normalize_region(request.args.get('region'))
    if current_user.is_authenticated and not request.args.get('region'):
        saved = getattr(current_user, 'streaming_region', None)
        if saved:
            region = normalize_region(saved)

    availability = get_availability(media_type, tmdb_id, region)

    payload = {
        'media_type': media_type,
        'media_id': tmdb_id,
        'region': availability['region'],
        'providers': {
            'stream': availability['stream'],
            'free': availability['free'],
            'rent': availability['rent'],
            'buy': availability['buy'],
        },
        'link': availability['link'],
        'available': availability['available'],
    }

    if current_user.is_authenticated:
        my_ids = _user_service_ids(current_user.id, availability['region'])
        match = match_my_services(availability, my_ids)
        payload['my_services'] = {
            'matches': match['matches'],
            'available': match['available'],
        }
    return jsonify(payload)


@availability_bp.route('/api/me/streaming-services')
@login_required
def get_streaming_services():
    """The caller's services + the provider picker for their region."""
    region = normalize_region(
        request.args.get('region') or getattr(current_user, 'streaming_region', None)
    )
    services = _user_service_ids(current_user.id, region)
    return jsonify({
        'region': region,
        'services': services,
        'available_providers': all_region_providers(region),
    })


@availability_bp.route('/api/me/streaming-services', methods=['POST'])
@limiter.limit("30 per minute")
@login_required
def save_streaming_services():
    """Replace the caller's service set for their region (idempotent).

    Accepts JSON {"services": [provider_id, ...]} and/or
    {"region": "US"}. Provider ids must come from TMDb's provider list for
    the region — arbitrary ids are rejected.
    """
    data = request.get_json(silent=True) or {}
    region = normalize_region(
        data.get('region') or getattr(current_user, 'streaming_region', None)
    )

    # Region update is standalone and idempotent.
    if 'region' in data:
        current_user.streaming_region = region

    if 'services' not in data:
        db.session.commit()
        return jsonify({
            'region': region,
            'services': _user_service_ids(current_user.id, region),
        })

    raw = data.get('services')
    if not isinstance(raw, list):
        return jsonify({'error': 'services must be a list'}), 400
    if len(raw) > _MAX_SERVICES:
        return jsonify({'error': f'Maximum {_MAX_SERVICES} services'}), 400

    try:
        requested = {int(pid) for pid in raw}
    except (TypeError, ValueError):
        return jsonify({'error': 'services must be provider ids'}), 400

    valid_ids = {p['id'] for p in all_region_providers(region)}
    invalid = requested - valid_ids
    if invalid:
        return jsonify({
            'error': 'Unknown provider for region',
            'invalid_provider_ids': sorted(invalid),
        }), 400

    existing = {
        row.provider_id
        for row in UserStreamingService.query.filter_by(
            user_id=current_user.id, region=region).all()
    }

    to_add = requested - existing
    for pid in sorted(to_add):
        db.session.add(UserStreamingService(
            user_id=current_user.id, provider_id=pid, region=region))

    to_remove = existing - requested
    if to_remove:
        UserStreamingService.query.filter(
            UserStreamingService.user_id == current_user.id,
            UserStreamingService.region == region,
            UserStreamingService.provider_id.in_(to_remove),
        ).delete(synchronize_session=False)

    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent identical save — the unique constraint makes this a no-op.
        db.session.rollback()
        db.session.begin_nested()
        db.session.commit()

    return jsonify({
        'region': region,
        'services': sorted(_user_service_ids(current_user.id, region)),
    })


@availability_bp.route('/api/me/streaming-services/<int:provider_id>', methods=['DELETE'])
@login_required
def remove_streaming_service(provider_id):
    region = normalize_region(
        request.args.get('region') or getattr(current_user, 'streaming_region', None)
    )
    UserStreamingService.query.filter_by(
        user_id=current_user.id, provider_id=provider_id, region=region
    ).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True, 'removed': provider_id})
