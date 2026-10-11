"""
Password hashing and session tokens - deliberately the SAME scheme Vett
uses (bcrypt, HMAC-signed stateless session tokens), not just a similar
one. Both products verify credentials against the same `users` table, so
using an incompatible hash scheme here would mean a password set via Vett
couldn't be verified by Statement Generator, defeating the point of a
shared identity table.

Uses the `bcrypt` library directly rather than through passlib. Vett's own
hashing goes through passlib's bcrypt backend, but passlib produces
standard bcrypt hash strings ($2b$...) - the same universal format the
`bcrypt` library reads and writes directly, since bcrypt is a well-defined
algorithm/format, not something passlib invented. So hashes are still
fully cross-verifiable between the two products; what changed is just
which library computes them. This avoids a real, current passlib/bcrypt
version-compatibility bug (passlib is effectively unmaintained, and
bcrypt>=4.1 removed an attribute passlib's own version-detection code
depends on - confirmed by hitting exactly this failure directly).

The session token format and SESSION_SECRET_KEY env var name also match
Vett's exactly. Neither product's cookie currently sets an explicit
`domain=`, so a session isn't shared across subdomains yet - but because
the token format and signing secret are identical, turning that on later
(once both products sit under one parent domain) is a one-line change to
each set_cookie call, not a rearchitecture.
"""
import os
import hmac
import secrets
import logging

import bcrypt

logger = logging.getLogger(__name__)


def _load_session_secret() -> str:
    """SESSION_SECRET_KEY is REQUIRED in production.

    A random per-process fallback is fine for local development, but in
    production it silently logs every user out on every deploy/restart and
    makes cross-product SSO with Vett impossible (each product would
    generate its own random key, never matching). Rather than warn and
    carry on, refuse to start - a missing secret is a deployment mistake
    that should be loud, not a runtime surprise.

    Set it to the SAME value Vett uses.
    """
    secret = os.getenv("SESSION_SECRET_KEY")
    if secret:
        return secret

    if os.getenv("APP_ENV", "production") == "production":
        raise RuntimeError(
            "SESSION_SECRET_KEY is not set. It is required when APP_ENV=production "
            "(the default). Set it to the same value Vett uses, or set APP_ENV=development "
            "for local work."
        )

    logger.warning(
        "SESSION_SECRET_KEY is not set - using a random key for this process only "
        "(allowed because APP_ENV is not 'production'). Sessions will not survive a restart."
    )
    return secrets.token_hex(32)


SESSION_SECRET_KEY = _load_session_secret()

# bcrypt has a hard 72-byte input limit (silently truncates or raises,
# depending on the binding) - truncate deliberately and consistently here
# rather than let an unexpectedly-long password behave differently on
# hash vs. verify.
_MAX_PASSWORD_BYTES = 72


def _prepare(plain: str) -> bytes:
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(plain: str) -> str:
    hashed = bcrypt.hashpw(_prepare(plain), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except Exception:
        return False


def make_session_token(user_id: str) -> str:
    """<user_id>:<nonce>:<hmac_hex> - tamper-evident, no DB lookup needed to validate."""
    nonce = secrets.token_urlsafe(16)
    payload = f"{user_id}:{nonce}"
    sig = hmac.new(SESSION_SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}:{sig}"


def decode_session_token(token: str) -> str | None:
    """Returns the user_id if the token is valid, else None."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        user_id, nonce, sig = parts
        payload = f"{user_id}:{nonce}"
        expected = hmac.new(SESSION_SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return user_id
    except Exception:
        return None