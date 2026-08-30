"""Shared-token access control.

DataQ started as a laptop tool, where the only client is the person running it.
The moment it is reachable from the internet that assumption inverts: the API can
read server-side files, write uploads, delete datasets, and spend money on the
Anthropic key. None of that should be anonymous.

The design is deliberately the smallest thing that is actually safe for a
single-user deployment: one shared token, checked in constant time, on every
route except the health probe. No accounts, no sessions, no expiry -- because
there is one user, and inventing more would be more surface, not less.

``require_auth`` is the safety catch. A hosted deployment sets it, and the app
then refuses to start without a token, so an instance cannot end up on a public
URL wide open because someone forgot a variable.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .users import parse_users, read_session

# Railway (and any other platform) probes this unauthenticated to decide whether
# the deployment is live. It reveals only the storage mode and a plugin count.
# /api/auth/login is necessarily open: it is where a credential is exchanged for
# a session, so requiring one to reach it would be a closed loop.
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login"})

PROTECTED_PREFIX = "/api/"


class MisconfiguredAuth(RuntimeError):
    """Raised at startup when auth is required but no token is set."""


def check_settings(settings) -> None:
    """Fail fast rather than serve an unprotected API.

    Called during app construction: a misconfigured deployment should never
    reach the point of accepting a request.
    """
    # The built-in account deliberately does not satisfy this. Its hash is in
    # the repository, so every clone knows the account exists and shares its
    # password -- fine as a starting point on a laptop, not something a public
    # deployment should be able to rely on without saying so.
    if settings.require_auth and not (settings.auth_token or parse_users(settings.users)):
        raise MisconfiguredAuth(
            "DATAQ_REQUIRE_AUTH is set but there is no deployment credential: "
            "no DATAQ_AUTH_TOKEN and no DATAQ_USERS. The built-in account does "
            "not count -- its password hash is in the source, so it is not a "
            "secret. Set one, or unset DATAQ_REQUIRE_AUTH for a local instance "
            "nobody else can reach."
        )


def extract_token(request: Request) -> str | None:
    """Bearer header, or a query parameter for EventSource.

    The browser's EventSource cannot set headers, and ``/api/jobs/{id}/stream``
    is an SSE endpoint, so the token has to be able to travel in the query
    string for that one case. It is the same secret either way; the tradeoff is
    that it can appear in server logs.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token")


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require a session or the shared token on every API route but the probe.

    Two credentials, because they answer different questions. A person logs in
    and gets a session; a script, a probe or a `curl` in the deploy docs carries
    the shared token. Either is sufficient, and an instance may configure only
    one.
    """

    def __init__(self, app, token: str | None, secret: bytes | None) -> None:
        super().__init__(app)
        self._token = token
        self._secret = secret

    def _allows(self, supplied: str) -> str | None:
        """The identity a credential establishes, or None."""
        if self._secret is not None:
            user = read_session(supplied, self._secret)
            if user is not None:
                return user
        # compare_digest keeps the check constant-time; a plain == leaks the
        # token a character at a time to anyone willing to measure.
        if self._token and hmac.compare_digest(supplied, self._token):
            return "token"
        return None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # The SPA and its assets are not secrets; the data behind /api is.
        if not path.startswith(PROTECTED_PREFIX) or path in PUBLIC_PATHS:
            return await call_next(request)

        supplied = extract_token(request)
        identity = self._allows(supplied) if supplied else None
        if identity is None:
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Routes that want to know who is asking can read it; none do yet, since
        # every user sees the same data.
        request.state.user = identity
        return await call_next(request)
