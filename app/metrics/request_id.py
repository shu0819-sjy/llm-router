"""Request ID resolution and ASGI middleware helpers."""

from __future__ import annotations

import re
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Visible ASCII token, length-bounded to avoid log injection / header abuse.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
REQUEST_ID_HEADER = "X-Request-Id"


def generate_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def normalize_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or not _REQUEST_ID_RE.match(candidate):
        return None
    return candidate


def resolve_request_id(*candidates: str | None) -> str:
    for raw in candidates:
        normalized = normalize_request_id(raw)
        if normalized:
            return normalized
    return generate_request_id()


def request_id_from_request(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    header = request.headers.get("x-request-id") or request.headers.get("X-Request-Id")
    rid = resolve_request_id(header)
    request.state.request_id = rid
    return rid


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Propagate/generate X-Request-Id on every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request_id_from_request(request)
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, rid)
        return response
