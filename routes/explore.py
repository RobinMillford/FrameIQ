"""F11 — the /discover page: facet UI over the TMDb-backed catalogue.

The page itself performs NO TMDb request. It canonicalises the query string
(server-side, via api.discovery), hands the browser exactly those validated
parameters, and the browser replays them through the existing validated proxy
(``routes/tmdb_proxy.py``) which keeps its path allowlist, cache and rate
limit. So discovery stays on the one existing upstream path and gains no
per-card network call.
"""
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required

from api.discovery import (
    clamp_page, config_to_query, discover_path, genre_options,
    build_discover_params, parse_discovery_args, SORT_LABELS,
)
from extensions import limiter

explore_bp = Blueprint('explore', __name__)


@explore_bp.route('/explore')
def explore_page():
    """Discovery page with the active facets reflected in the URL."""
    config, notices = parse_discovery_args(request.args)
    page = clamp_page(request.args.get('page', 1))

    media_type = config['media_type']
    sorts = [s for s in _sorts_for(media_type)]

    return render_template(
        'explore.html',
        config=config,
        params=build_discover_params(config, page),
        path=discover_path(media_type),
        genres=genre_options(media_type),
        sorts=[{'value': s, 'label': SORT_LABELS.get(s, s)} for s in sorts],
        chips=_chips(config),
        query=config_to_query(config),
        notices=notices,
        page=page,
        # Pre-resolved display values: the template must never rely on a
        # Jinja attribute lookup for a key that may legitimately be absent.
        year_from_value=(config.get('year_from') if config.get('year_from')
                         is not None else ''),
        year_to_value=(config.get('year_to') if config.get('year_to')
                       is not None else ''),
        min_rating_value=(config.get('min_rating')
                          if config.get('min_rating') is not None else 0),
        rating_display=('Any' if config.get('min_rating') is None
                        else '%.1f' % config['min_rating']),
        selected_genres=set(config.get('genres') or []),
        presets=_preset_payload() if current_user.is_authenticated else [],
    )


def _sorts_for(media_type):
    from api.discovery import sorts_for
    return sorts_for(media_type)


def _chips(config):
    from api.discovery import summarize
    return summarize(config)


@explore_bp.route('/explore/presets', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def save_discovery_preset():
    """Save the current facet combination as a discovery preset.

    Uses the existing SmartList CRUD contract and persistence. The row's
    ``scope`` is a non-nullable compatibility placeholder; the ``kind``/
    ``discovery`` pair in filters_json is what makes it a preset.
    """
    from models import db
    from models.smart_lists import SmartList
    from api.smart_lists import validate_config
    from api.discovery import (
        DISCOVERY_KIND, DISCOVERY_COMPAT_SCOPE, DISCOVERY_COMPAT_SORT,
        validate_discovery_config, DiscoveryError,
    )

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Give this discovery filter a name.'}), 400

    raw = data.get('config')
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except ValueError:
            return jsonify({'error': 'Malformed discovery configuration.'}), 400

    try:
        discovery = validate_discovery_config(
            raw or {}, source='saved discovery')
        filters = validate_config(
            DISCOVERY_COMPAT_SCOPE,
            {'kind': DISCOVERY_KIND, 'discovery': discovery},
            DISCOVERY_COMPAT_SORT)
    except (ValueError, DiscoveryError) as exc:
        return jsonify({'error': str(exc)}), 400

    preset = SmartList(
        user_id=current_user.id,
        name=name[:200],
        description=(data.get('description') or '').strip()[:500] or None,
        scope=DISCOVERY_COMPAT_SCOPE,
        filters=filters,
        sort=DISCOVERY_COMPAT_SORT,
        is_public=False,
    )
    db.session.add(preset)
    db.session.commit()
    return jsonify({'success': True,
                    'preset': _preset_dict(preset, config=discovery)}), 201


@explore_bp.route('/explore/presets/<int:preset_id>', methods=['DELETE'])
@login_required
@limiter.limit("30 per minute")
def delete_discovery_preset(preset_id):
    """Delete one of the owner's presets. Owner-only, 404 for anyone else."""
    from models import db
    from models.smart_lists import SmartList
    from api.smart_lists import is_discovery_preset

    preset = SmartList.query.filter_by(
        id=preset_id, user_id=current_user.id).first()
    if not preset or not is_discovery_preset(preset):
        return jsonify({'error': 'Saved discovery not found'}), 404
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'success': True})


def _preset_payload():
    """The signed-in user's discovery presets for the sidebar."""
    from models.smart_lists import SmartList
    from api.smart_lists import is_discovery_preset
    from api.discovery import validate_discovery_config, DiscoveryError

    rows = (SmartList.query
            .filter_by(user_id=current_user.id)
            .order_by(SmartList.created_at.desc())
            .all())
    presets = []
    for row in rows:
        if not is_discovery_preset(row):
            continue
        try:
            config = validate_discovery_config(
                row.filters.get('discovery') or {},
                source='saved discovery')
        except DiscoveryError:
            presets.append(_preset_dict(row, config=None))
            continue
        presets.append(_preset_dict(row, config=config))
    return presets


def _preset_dict(row, config=None):
    payload = {'id': row.id, 'name': row.name,
               'description': row.description, 'is_discovery': True}
    payload['query'] = config_to_query(config) if config else None
    return payload


@explore_bp.route('/explore/reset')
def reset_discovery():
    """Explicit Reset target: returns to an unfiltered discovery page."""
    return redirect(url_for('explore.explore_page'))


@explore_bp.route('/explore/presets/<int:preset_id>/restore')
@login_required
def restore_discovery(preset_id):
    """Reopen a saved discovery preset in Discover.

    Restoring never queries the user's local media: it re-serialises the
    stored, re-validated TMDb parameters into the discovery URL and redirects
    there, where the TMDb proxy evaluates them.
    """
    from models.smart_lists import SmartList
    from api.smart_lists import is_discovery_preset
    from api.discovery import validate_discovery_config, DiscoveryError

    preset = SmartList.query.filter_by(
        id=preset_id, user_id=current_user.id).first()
    # Owner-scoped, and a non-preset is indistinguishable from a missing row.
    if not preset or not is_discovery_preset(preset):
        return render_template(
            'error.html', message='Saved discovery not found'), 404

    try:
        config = validate_discovery_config(
            preset.filters.get('discovery') or {},
            source='saved discovery')
    except DiscoveryError as exc:
        # An outdated/unreadable config degrades to the plain page rather than
        # 500ing or silently applying unsupported filters.
        return render_template(
            'error.html',
            message=(f'That saved discovery filter can no longer be used: '
                     f'{exc}')), 400

    return redirect(url_for('explore.explore_page',
                            **config_to_query(config)))