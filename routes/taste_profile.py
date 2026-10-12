"""Taste DNA API route (Feature #6, Phase 14) — private presentation only.

GET /api/taste-profile exposes the authenticated user's own TasteProfile as
a human-readable presentation object built by api.taste_profile.taste_dna().
The route adds only:

- authentication (flask-login) — anonymous users get redirected/401; the
  session user is authoritative and the ONLY input. There is deliberately
  no user_id/username/profile_id parameter, so inspecting another user's
  taste is impossible by construction
- a bounded rate limit (single cached profile read per request — cheap)
- one batched read resolving actor display names to existing /actor/ URLs
  (F14); skipped entirely when the profile has no actor affinity

The response is a PRESENTATION MODEL: strength labels instead of raw
weights, no raw JSON. Actors are exposed as a ready-made relative URL, not a
raw person id, so the presentation contract stays id-free. A user
without a profile gets 200 {available: false, state: "cold_start"} — that
is a normal state, not an error. Nothing is computed, mutated, or fetched
from the network here.
"""
import logging

from flask import Blueprint, jsonify
from flask_login import login_required, current_user

from extensions import limiter
import api.taste_profile as taste_profile

logger = logging.getLogger(__name__)

taste_profile_bp = Blueprint('taste_profile', __name__)

# Read-only, one bounded DB read per request.
RATE_LIMIT = "60 per minute"


@taste_profile_bp.route('/api/taste-profile')
@login_required
@limiter.limit(RATE_LIMIT)
def api_taste_profile():
    """The authenticated user's own Taste DNA (presentation model)."""
    try:
        profile = taste_profile.get_profile(current_user.id)
    except Exception:
        logger.error("Taste DNA load failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    if profile is None:
        return jsonify({'available': False, 'state': 'cold_start'})

    try:
        # F14: resolve actor names to existing /actor/ URLs BEFORE formatting,
        # so the formatter stays pure. One extra batched query at most, and
        # zero when the profile carries no actor affinity (the common case).
        # No user_id input, no network, no recompute — read-only as before.
        actor_links = taste_profile.resolve_actor_links(
            (profile.actor_affinity or {}).keys())
        # F19: one batched lookup for the whole director set (zero queries
        # when there is no director affinity), each resolved to at most one
        # LOCAL Director row so no ambiguous name can become a wrong link.
        director_links = taste_profile.resolve_director_links(
            (profile.director_affinity or {}).keys())
        dna = taste_profile.taste_dna(
            profile, actor_links=actor_links, director_links=director_links)
    except Exception:
        logger.error("Taste DNA presentation failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    if dna is None:
        # Profile row exists but carries no positive evidence yet —
        # present the same neutral cold-start contract.
        return jsonify({'available': False, 'state': 'cold_start'})
    return jsonify(dna)
