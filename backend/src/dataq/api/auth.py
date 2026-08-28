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

# Railway (and any other platform) probes this unauthenticated to decide whether
# the deployment is live. It reveals only the storage mode and a plugin count.
PUBLIC_PATHS = frozenset({"/api/health"})

PROTECTED_PREFIX = "/api/"


class MisconfiguredAuth(RuntimeError):
    """Raised at startup when auth is required but no token is set."""


def check_settings(settings) -> None:
    """Fail fast rather than serve an unprotected API.

    Called during app construction: a misconfigured deployment should never
    reach the point of accepting a request.
    """
    if settings.require_auth and not settings.auth_token:
        raise MisconfiguredAuth(
            "DATAQ_REQUIRE_AUTH is set but DATAQ_AUTH_TOKEN is empty. Set a token, "
            "or unset DATAQ_REQUIRE_AUTH for a local instance that nobody else "
            "can reach."
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
    """Require a shared token on every API route but the health probe."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # The SPA and its assets are not secrets; the data behind /api is.
        if not path.startswith(PROTECTED_PREFIX) or path in PUBLIC_PATHS:
            return await call_next(request)

        supplied = extract_token(request)
        # compare_digest keeps the check constant-time; a plain == leaks the
        # token a character at a time to anyone willing to measure.
        if supplied is None or not hmac.compare_digest(supplied, self._token):
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
