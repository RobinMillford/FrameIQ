"""Personal statistics API (Feature #8, Phase 3) — private read-only adapter.

GET /api/statistics exposes the authenticated user's own canonical viewing
statistics (Feature #8 Phase 2 service) as JSON. The route is an ADAPTER
around api.statistics.get_statistics(): it adds

- authentication (flask-login) — anonymous users get redirected/401; the
  session user is authoritative and the ONLY identity input. There is
  deliberately no user_id/username/email parameter, so reading another
  user's statistics is impossible by construction
- query-parameter parsing for the Phase 2 window contract:

      /api/statistics               → current calendar year
      /api/statistics?year=2025     → the 2025 calendar year
      /api/statistics?lifetime=true → all history

  and nothing else. Arbitrary start/end query parameters are NOT exposed.
- clean 400 responses for malformed input (never a traceback):
  non-integer years, out-of-range years (canonical service validation),
  and the year + lifetime combination, which the canonical service would
  silently resolve (lifetime wins) — the API rejects it explicitly
  instead of guessing

The response body is the canonical service's presentation-safe dict
(no database IDs, no ORM objects, no debug fields) plus the resolved
"period" for UI labeling. Nothing is computed, cached, or mutated here,
and the service performs zero network calls.

Also exposes GET /api/year-in-review (Feature #8 Phase 8): a private
adapter around api.year_in_review.build_year_in_review(), the canonical
story transformation over ONE get_statistics() call. Year-only scope;
invalid years are rejected (clean 400) before the builder is ever
invoked. Responses carry Cache-Control: private, no-store (§40).

Feature #8 Phase 9 adds the share surface, opt-in and revocable:

- POST   /api/year-in-review/share  — create (or reuse) the active share
  for one year of the session user's recap; returns the share URL with
  the raw opaque token, which is NOT persisted anywhere
- DELETE /api/year-in-review/share  — revoke the active share for one
  year; public links die immediately and generically (404)

Share creation validates the year FIRST, then builds the recap exactly
once to fail closed on empty years (§30), and only then creates the
share record. No user_id override exists; global CSRF stays enabled on
both mutations (§40). The share layer never queries the watch-history
or media tables directly and never calls the statistics service itself
(§36) — it only consumes the canonical builder's output.
"""
import logging

from datetime import datetime

from flask import Blueprint, jsonify, request, url_for
from flask_login import login_required, current_user

from extensions import limiter
from models import db, YearInReviewShare
import api.statistics as statistics_service
import api.year_in_review as year_in_review_service
import api.year_in_review_share as year_in_review_share_service

logger = logging.getLogger(__name__)

statistics_bp = Blueprint('statistics', __name__)

# Read-only adapters (5→8 bounded SQL statements through the service).
RATE_LIMIT = "60 per minute"

# §40: private recap — one user's recap must never be cached for another.
_PRIVATE_NO_STORE = {'Cache-Control': 'private, no-store'}


_LIFETIME_TRUE = ('true', '1', 'yes')
_LIFETIME_FALSE = ('false', '0', 'no')


def _parse_window_params():
    """Parse ?year= / ?lifetime= into the service window arguments.

    Returns (error_response_or_None, lifetime_or_None, year_or_None).
    Both params absent → (None, None, None): the service's current-year
    default applies.
    """
    raw_year = request.args.get('year')
    raw_lifetime = request.args.get('lifetime')

    lifetime = None
    if raw_lifetime is not None:
        value = raw_lifetime.strip().lower()
        if value in _LIFETIME_TRUE:
            lifetime = True
        elif value in _LIFETIME_FALSE:
            lifetime = False
        else:
            return (jsonify(
                {'error': "lifetime must be 'true' or 'false'"}), 400), \
                None, None

    year = None
    if raw_year is not None:
        try:
            year = int(raw_year.strip())
        except (ValueError, TypeError):
            return (jsonify({'error': 'year must be an integer'}), 400), \
                None, None

    if year is not None and lifetime:
        # The canonical service resolves this silently (lifetime wins);
        # the API refuses the ambiguity instead of picking a side.
        return (jsonify(
            {'error': 'year and lifetime are mutually exclusive'}), 400), \
            None, None

    return None, lifetime, year


@statistics_bp.route('/api/statistics')
@login_required
@limiter.limit(RATE_LIMIT)
def api_statistics():
    """The authenticated user's own viewing statistics (canonical service)."""
    error, lifetime, year = _parse_window_params()
    if error is not None:
        return error

    try:
        if lifetime:
            stats = statistics_service.get_statistics(
                current_user.id, lifetime=True)
        else:
            stats = statistics_service.get_statistics(
                current_user.id, year=year)
    except ValueError as exc:
        # Canonical service validation (malformed / out-of-range years).
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.error("Statistics load failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    if lifetime:
        period = {'type': 'lifetime', 'year': None}
    else:
        period = {'type': 'year',
                  'year': year if year is not None
                  else datetime.now().year}
    return jsonify({'period': period, **stats})


@statistics_bp.route('/api/year-in-review')
@login_required
@limiter.limit(RATE_LIMIT)
def api_year_in_review():
    """The authenticated user's own Year in Review (canonical story model).

    Pure adapter: exactly ONE build_year_in_review() call per request —
    the builder performs exactly ONE get_statistics() call, so the whole
    request is auth → validation → builder → JSON (§8). No watch-history,
    media, external, preference, or feedback access happens here;
    invalid years are rejected before any data work runs (§5).
    """
    raw_year = request.args.get('year')
    if raw_year is None:
        # §4: the documented current-calendar-year default (statistics
        # route convention) — no second default is invented.
        year = datetime.now().year
    else:
        try:
            year = int(raw_year.strip())
        except (ValueError, TypeError):
            return jsonify({'error': 'year must be an integer'}), 400

    try:
        recap = year_in_review_service.build_year_in_review(
            current_user.id, year)
    except ValueError as exc:
        # Canonical validation: out-of-range / non-calendar years.
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.error("Year in review failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    response = jsonify(recap)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


# ══════════════════════════════════════════════════════════════════════════
# Year-in-Review share lifecycle (Feature #8, Phase 9) — opt-in, revocable
# ══════════════════════════════════════════════════════════════════════════

def _parse_share_year():
    """Strict integer-year parse for the share JSON body.

    §5/§31: JSON coercion (e.g. true → 1, 2026.0 → 2026) is refused so
    the private and public routes share one consistent year semantic.
    Returns (year, error_response) — exactly one is non-None.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or 'year' not in body:
        return None, (jsonify({'error': "JSON body with 'year' required"}),
                      400)
    raw_year = body['year']
    if isinstance(raw_year, bool) or not isinstance(raw_year, int):
        return None, (jsonify({'error': 'year must be an integer'}), 400)
    return raw_year, None


@statistics_bp.route('/api/year-in-review/share', methods=['POST'])
@login_required
@limiter.limit(RATE_LIMIT)
def api_year_in_review_share_create():
    """Create (or reuse) the active share for one year of the own recap.

    Call graph (§12): auth → strict year parse → canonical validation →
    exactly ONE build_year_in_review() (empty years fail closed, §30) →
    create/reuse the share record → share URL. The builder alone calls
    the statistics service; this route never does (§36). CSRF stays
    enabled (§40) and no user_id parameter exists (§11).
    """
    year, error = _parse_share_year()
    if error is not None:
        return error

    try:
        recap = year_in_review_service.build_year_in_review(
            current_user.id, year)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.error("Share creation failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    if not recap.get('available'):
        # §30: never create a public token pointing at an empty story.
        return jsonify({'error': 'nothing to share for this year'}), 400

    # Regeneration semantics (§14): creating a share for a year that
    # already has an active one revokes the old token and issues a fresh
    # one — old links die immediately. The partial unique index on
    # unrevoked (user_id, year) backs this one-active-share invariant.
    existing = YearInReviewShare.query.filter_by(
        user_id=current_user.id, year=year, revoked_at=None).first()
    if existing is None:
        raw_token = year_in_review_share_service.generate_share_token()
        share = YearInReviewShare(
            user_id=current_user.id, year=year,
            token_hash=year_in_review_share_service.hash_share_token(
                raw_token))
        db.session.add(share)
    else:
        share = existing
        share.revoked_at = datetime.utcnow()  # old token dies now
        raw_token = year_in_review_share_service.generate_share_token()
        replacement = YearInReviewShare(
            user_id=current_user.id, year=year,
            token_hash=year_in_review_share_service.hash_share_token(
                raw_token))
        db.session.add(replacement)
        share = replacement
    db.session.commit()

    share_url = url_for('main.year_in_review_share_page', token=raw_token,
                        _external=True)
    logger.info(
        "Year in review share %s for user %s year %s",
        'reissued' if existing is not None else 'created',
        current_user.id, year)
    return jsonify({
        'year': year,
        'share_url': share_url,
        'active': True,
    })


@statistics_bp.route('/api/year-in-review/share', methods=['DELETE'])
@login_required
@limiter.limit(RATE_LIMIT)
def api_year_in_review_share_revoke():
    """Revoke the session user's active share for one year.

    Owner-only (§13): the lookup is scoped to the session user, so
    another user's share can neither be found nor revoked. Revocation
    is instant — the public route re-checks revoked_at on every view.
    Idempotent: revoking a year with no active share is a no-op.
    """
    year, error = _parse_share_year()
    if error is not None:
        return error

    share = YearInReviewShare.query.filter_by(
        user_id=current_user.id, year=year, revoked_at=None).first()
    if share is not None:
        share.revoked_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'year': year, 'active': False})
