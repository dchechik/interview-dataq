"""Usernames and passwords, and the sessions they buy.

The shared token in :mod:`dataq.api.auth` is the right shape for one person on
one laptop, and the wrong shape for a URL you send to a colleague: there is
nothing to type into a browser, no way to tell two people apart, and revoking
access means changing the secret for everyone.

This adds the smallest thing that fixes that. A fixed list of users, each with a
password hash; a login endpoint that exchanges a password for a session token;
and a token the browser can keep. Everyone sees the same datasets -- the point
is to keep the instance off the public internet, not to model permissions.

Two decisions worth stating, because both could reasonably have gone the other
way in a prototype:

* **Passwords are hashed, not stored.** scrypt, from the standard library, with
  a per-password salt. A hard-coded list is fine; a hard-coded *password* would
  mean the repository, every clone of it, and every terminal that ever printed
  the config all hold the credential itself.
* **Sessions are signed, not stored.** A token carries its own username and
  expiry with an HMAC over both, so there is no session table to grow, and a
  restart does not log everybody out -- the signing key lives in the data
  directory, beside the data it protects.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

# scrypt parameters. These are the interactive-login numbers from the original
# paper: ~16MB and a few tens of milliseconds, which is nothing to a person
# logging in and a great deal to someone working through a stolen hash.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16

SESSION_PREFIX = "dq1"
DEFAULT_SESSION_HOURS = 24 * 14


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# The starting account, so a fresh instance is usable without configuration.
# Only the hash is here: a hard-coded list is a reasonable prototype shortcut,
# but a hard-coded password would put the credential itself in the repository,
# in every clone, and in every terminal that ever printed the file. Replace or
# extend this with DATAQ_USERS, which takes the same "name:hash" entries.
DEFAULT_USERS: dict[str, str] = {
    "dmitry": (
        "scrypt$1AEo1R6t2u61DNI3LjV2HQ$yzy4ggSnQ0bJmj3wR1NfD80R_NABW9nOtUOzYhwJ"
        "9Bjp97fARl1D_xjzZeRPS_1ojGvMJShps6IZyXbj1SfdKw"
    ),
}


def resolve_users(configured: str | None) -> dict[str, str]:
    """Configured users when there are any, the built-in one otherwise.

    Replacing rather than merging: if someone sets DATAQ_USERS on a deployment,
    leaving a default account quietly enabled alongside it would be the opposite
    of what they asked for.
    """
    return parse_users(configured) or dict(DEFAULT_USERS)


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: bytes | None = None) -> str:
    """``scrypt$<salt>$<hash>``, safe to commit and to print."""
    salt = salt or secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N,
                             r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash, in constant time.

    A malformed or empty hash is a failure rather than an error: this runs on an
    unauthenticated path, so a bad entry in the user list must not become a way
    to crash the login endpoint.
    """
    try:
        scheme, salt_b64, expected = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = _unb64(salt_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N,
                             r=SCRYPT_R, p=SCRYPT_P)
    return hmac.compare_digest(_b64(derived), expected)


def parse_users(spec: str | None) -> dict[str, str]:
    """``name:hash`` entries, separated by commas or newlines.

    Hashes contain ``$`` and base64, never ``:``, so splitting on the first
    colon is unambiguous.
    """
    users: dict[str, str] = {}
    for entry in (spec or "").replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        name, _, digest = entry.partition(":")
        if name.strip() and digest.strip():
            users[name.strip()] = digest.strip()
    return users


def authenticate(username: str, password: str, users: dict[str, str]) -> bool:
    """Whether these credentials are good.

    An unknown username is still checked against a dummy hash. Returning early
    would make a wrong username measurably faster than a wrong password, which
    is a free list of who has an account.
    """
    stored = users.get(username)
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


# A real hash of a random string, so the not-a-user path does the same work as
# the wrong-password path.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def session_secret(data_dir: Path, configured: str | None = None) -> bytes:
    """The key sessions are signed with.

    Kept in the data directory rather than generated per process, so restarting
    the server -- or a code deploy -- does not sign everyone out. Created on
    first use with owner-only permissions.
    """
    if configured:
        return configured.encode()
    path = Path(data_dir) / "session_secret"
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32).encode()
    path.write_bytes(secret)
    # A filesystem without modes still has the data directory guarding it.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return secret


def issue_session(username: str, secret: bytes, hours: int = DEFAULT_SESSION_HOURS) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + hours * 3600},
                         separators=(",", ":")).encode()
    body = _b64(payload)
    sig = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    return f"{SESSION_PREFIX}.{body}.{_b64(sig)}"


def read_session(token: str, secret: bytes) -> str | None:
    """The username a token vouches for, or None if it does not.

    Signature first, then expiry: an unsigned token's claims are not worth
    reading, including its own expiry.
    """
    try:
        prefix, body, sig = token.split(".", 2)
    except ValueError:
        return None
    if prefix != SESSION_PREFIX:
        return None
    expected = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(expected), sig):
        return None
    try:
        payload = json.loads(_unb64(body))
        if int(payload["exp"]) < time.time():
            return None
        return str(payload["u"])
    except (ValueError, KeyError, TypeError):
        return None
