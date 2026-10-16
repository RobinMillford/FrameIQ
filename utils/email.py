"""
Email utilities — token generation and email dispatch.

Tokens use itsdangerous URLSafeTimedSerializer (no DB storage needed).
Email sending degrades gracefully: if MAIL_SERVER is not configured,
the link is logged at WARNING level so dev/test flows still work.
"""
import hashlib
import logging
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import current_app, url_for
from flask_mail import Message
from extensions import mail

logger = logging.getLogger(__name__)

_RESET_SALT    = 'password-reset-salt'
_VERIFY_SALT   = 'email-verify-salt'
_TOKEN_MAX_AGE = 3600   # 1 hour


# ── Token helpers ─────────────────────────────────────────────────────────────

def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'])


def password_fingerprint(password_hash: str | None) -> str:
    """A short tag binding a reset token to the credential it was issued for.

    F55: the reset token used to carry only the email address and an expiry, so
    it was replayable for the whole hour after a successful reset. Binding it
    to a digest of the *current* password hash makes the token self-invalidating
    the moment the password changes — no database row, no cleanup job, and it
    works for every outstanding token at once.

    The digest is over the stored scrypt hash, never the password itself, and
    the value travels inside an HMAC-signed payload, so it is neither readable
    nor a credential. Truncation to 128 bits is ample: this is a change
    detector, not a secret.
    """
    return hashlib.sha256((password_hash or '').encode('utf-8')).hexdigest()[:32]


def generate_reset_token(email: str, password_hash: str | None) -> str:
    """Mint a reset token bound to the user's current credential."""
    payload = {'e': email, 'p': password_fingerprint(password_hash)}
    return _serializer().dumps(payload, salt=_RESET_SALT)


def verify_reset_token(token: str) -> dict | None:
    """Decode a reset token, or return None if it is unusable.

    Returns ``{'email': ..., 'fingerprint': ...}``. The caller must still
    compare the fingerprint against the user's current password hash — that
    comparison is what makes the token single-use.

    A payload that is not this dict shape is rejected outright. That includes
    the legacy bare-email payload, so tokens minted before F55 fail closed
    instead of remaining replayable for whatever is left of their hour.
    """
    try:
        payload = _serializer().loads(token, salt=_RESET_SALT,
                                      max_age=_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    except Exception:
        # A malformed token must be "invalid", never a 500.
        return None

    if not isinstance(payload, dict):
        return None
    email = payload.get('e')
    fingerprint = payload.get('p')
    if not isinstance(email, str) or not email:
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    return {'email': email, 'fingerprint': fingerprint}


def generate_verify_token(email: str) -> str:
    return _serializer().dumps(email, salt=_VERIFY_SALT)


def verify_email_token(token: str) -> str | None:
    """Returns email if valid, None if expired/invalid."""
    try:
        return _serializer().loads(token, salt=_VERIFY_SALT, max_age=_TOKEN_MAX_AGE * 24)
    except (SignatureExpired, BadSignature):
        return None


# ── Sending helpers ───────────────────────────────────────────────────────────

def _send(subject: str, recipient: str, body: str) -> bool:
    """Send email; falls back to logger if MAIL_SERVER not configured."""
    if not current_app.config.get('MAIL_SERVER'):
        logger.warning(
            "MAIL_SERVER not set — email NOT sent to %s.\nSubject: %s\nBody:\n%s",
            recipient, subject, body
        )
        return False
    try:
        msg = Message(subject, recipients=[recipient], body=body)
        mail.send(msg)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", recipient, e)
        return False


def send_password_reset_email(user) -> bool:
    token = generate_reset_token(user.email, user.password_hash)
    link  = url_for('auth.reset_password', token=token, _external=True)
    body  = (
        f"Hi {user.username},\n\n"
        f"You requested a password reset for your FrameIQ account.\n\n"
        f"Click the link below to reset your password (expires in 1 hour):\n{link}\n\n"
        "If you did not request this, you can safely ignore this email.\n\n"
        "— FrameIQ Team"
    )
    return _send("Reset your FrameIQ password", user.email, body)


def send_verification_email(user) -> bool:
    token = generate_verify_token(user.email)
    link  = url_for('auth.verify_email', token=token, _external=True)
    body  = (
        f"Hi {user.username},\n\n"
        f"Welcome to FrameIQ! Please verify your email address:\n{link}\n\n"
        "This link expires in 24 hours.\n\n"
        "— FrameIQ Team"
    )
    return _send("Verify your FrameIQ email", user.email, body)
