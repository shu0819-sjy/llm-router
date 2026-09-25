"""OpenAI-compatible error envelopes, upstream error sanitization, and request
boundary enforcement (body size, chat-schema bounds, concurrency).

Boundary behavior (v0.3 hardening):
- ``sanitize_client_error_message`` bounds and redacts any text destined for a
  client-facing error envelope (length cap, control-char flattening, secret
  pattern redaction). Detailed diagnostics stay in logs, correlated by
  X-Request-Id.
- An unhandled-exception handler returns a generic bounded 500 envelope while
  logging the full traceback with request correlation.
- ``RequestBoundaryMiddleware`` (pure ASGI) enforces request body size (413),
  chat request schema bounds (400), and chat concurrency (429), all as
  OpenAI-shaped envelopes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics.request_id import REQUEST_ID_HEADER, request_id_from_request

logger = logging.getLogger("llm_router.errors")

# ---------------------------------------------------------------------------
# Client-facing error sanitization
# ---------------------------------------------------------------------------

MAX_CLIENT_ERROR_MESSAGE_CHARS = 512

# Patterns that must never reach a client error envelope. Deliberately
# over-approximate: long token-ish runs are redacted even when they might be
# innocuous upstream text.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\s*[=:]\s*\S+"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s]+"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)


def sanitize_client_error_message(
    message: Any,
    *,
    max_chars: int = MAX_CLIENT_ERROR_MESSAGE_CHARS,
) -> str:
    """
    Bound and redact a message before it is returned to an API client.

    - Flattens control characters (blocks log/header injection).
    - Redacts key-like / credential-like substrings to ``[redacted]``.
    - Truncates to ``max_chars``.
    """
    text = "" if message is None else str(message)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text or "error"


def openai_error_body(
    message: str,
    *,
    type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "message": message,
        "type": type,
        "param": param,
        "code": code,
    }
    err.update(extra)
    return {"error": err}


def openai_error_response(
    status_code: int,
    message: str,
    *,
    type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_body(message, type=type, code=code, param=param, **extra),
        headers=headers,
    )


def bounded_error_response(
    status_code: int,
    message: Any,
    *,
    type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """openai_error_response with the message sanitized/bounded for clients."""
    return openai_error_response(
        status_code,
        sanitize_client_error_message(message),
        type=type,
        code=code,
        param=param,
        headers=headers,
        **extra,
    )


def _sanitize_unwrapped_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Sanitize the message inside an already-OpenAI-shaped HTTPException detail."""
    err = detail.get("error")
    if isinstance(err, dict) and "message" in err:
        err["message"] = sanitize_client_error_message(err.get("message"))
    return detail


def _unwrap_detail(detail: Any) -> dict[str, Any] | None:
    """If FastAPI HTTPException.detail already carries an OpenAI error, unwrap it."""
    if isinstance(detail, dict) and "error" in detail and isinstance(detail["error"], dict):
        return detail
    if isinstance(detail, dict) and {"message", "type"} <= set(detail.keys()):
        return {"error": detail}
    return None


def install_openai_exception_handlers(app: FastAPI) -> None:
    """Map common FastAPI errors onto top-level `{error: {...}}` envelopes."""
    if getattr(app.state, "_openai_error_handlers_installed", False):
        return

    async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        status_code = int(getattr(exc, "status_code", 500) or 500)
        detail = getattr(exc, "detail", str(exc))
        headers = getattr(exc, "headers", None)
        unwrapped = _unwrap_detail(detail)
        if unwrapped is not None:
            return JSONResponse(
                status_code=status_code,
                content=_sanitize_unwrapped_detail(unwrapped),
                headers=headers,
            )
        message = detail if isinstance(detail, str) else str(detail)
        type_ = "invalid_request_error" if status_code < 500 else "api_error"
        return openai_error_response(
            status_code,
            sanitize_client_error_message(message),
            type=type_,
            code=str(status_code),
            headers=headers,
        )

    # FastAPI HTTPException (subclass) + Starlette base, so neither path leaks `detail`.
    app.add_exception_handler(HTTPException, _http_exception_handler)
    try:
        from starlette.exceptions import HTTPException as StarletteHTTPException

        if StarletteHTTPException is not HTTPException:
            app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    except Exception:  # pragma: no cover
        pass

    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Bounded, sanitized 500 for clients; full traceback in logs, request-correlated."""
        rid = request_id_from_request(request)
        logger.exception(
            "unhandled error request_id=%s method=%s path=%s error=%r",
            rid,
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal server error",
                    "type": "api_error",
                    "param": None,
                    "code": "internal_error",
                }
            },
            headers={REQUEST_ID_HEADER: rid},
        )

    app.add_exception_handler(Exception, _unhandled_exception_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        loc = ""
        msg = "Invalid request"
        if errors:
            first = errors[0]
            loc_parts = [str(p) for p in first.get("loc", ()) if p != "body"]
            loc = ".".join(loc_parts)
            msg = str(first.get("msg") or msg)
            if loc:
                msg = f"{msg} ({loc})"
        return openai_error_response(
            422,
            sanitize_client_error_message(msg),
            type="invalid_request_error",
            code="invalid_request",
            param=loc or None,
        )

    app.state._openai_error_handlers_installed = True


def ensure_openai_handlers(request: Request) -> None:
    """Router dependency: install OpenAI error envelopes on first /v1 hit."""
    install_openai_exception_handlers(request.app)


# ---------------------------------------------------------------------------
# Request boundary middleware (body size, chat-schema bounds, concurrency)
# ---------------------------------------------------------------------------


class RequestBoundaryMiddleware:
    """
    Pure-ASGI boundary enforcement for every HTTP request.

    Guarantees (all responses are bounded OpenAI-shaped envelopes):

    1. Request bodies larger than ``max_body_bytes`` → 413
       (``request_too_large``), checked via Content-Length when present and
       via accumulated chunk sizes otherwise.
    2. ``POST /v1/chat/completions`` bodies with more than ``max_messages``
       messages or ``max_tools`` tools → 400 (``too_many_messages`` /
       ``too_many_tools``). Malformed JSON is left to FastAPI's 422 path.
    3. More than ``max_concurrent_requests`` concurrent ``/v1/chat/*``
       requests → 429 (``concurrency_limit``) with ``Retry-After: 1``.

    Limits are read per-request from ``app.state.settings`` (falling back to
    conservative built-in defaults), so wiring is simply::

        app.add_middleware(RequestBoundaryMiddleware)

    Constructor kwargs, when given, override settings (for focused tests).
    """

    DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
    DEFAULT_MAX_MESSAGES = 512
    DEFAULT_MAX_TOOLS = 64
    DEFAULT_MAX_CONCURRENT = 64

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int | None = None,
        max_messages: int | None = None,
        max_tools: int | None = None,
        max_concurrent_requests: int | None = None,
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._max_messages = max_messages
        self._max_tools = max_tools
        self._max_concurrent_requests = max_concurrent_requests
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_size: int | None = None

    # -- limits ---------------------------------------------------------------

    def _limits(self, scope: Scope) -> tuple[int, int, int, int]:
        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)

        def pick(explicit: int | None, name: str, default: int) -> int:
            if explicit is not None:
                return max(1, int(explicit))
            value = getattr(settings, name, None) if settings is not None else None
            if value:
                return max(1, int(value))
            return default

        return (
            pick(self._max_body_bytes, "max_body_bytes", self.DEFAULT_MAX_BODY_BYTES),
            pick(self._max_messages, "max_messages", self.DEFAULT_MAX_MESSAGES),
            pick(self._max_tools, "max_tools", self.DEFAULT_MAX_TOOLS),
            pick(
                self._max_concurrent_requests,
                "max_concurrent_chat_requests",
                self.DEFAULT_MAX_CONCURRENT,
            ),
        )

    def _get_semaphore(self, size: int) -> asyncio.Semaphore:
        if self._semaphore is None or self._semaphore_size != size:
            self._semaphore = asyncio.Semaphore(size)
            self._semaphore_size = size
        return self._semaphore

    # -- ASGI entry point ------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - lifespan/websocket passthrough
            await self.app(scope, receive, send)
            return

        max_body, max_messages, max_tools, max_concurrent = self._limits(scope)
        path = scope.get("path") or ""

        # Concurrency gate for expensive upstream chat work.
        gate = path.startswith("/v1/chat")
        semaphore = self._get_semaphore(max_concurrent)
        if gate and semaphore.locked():
            await self._reject(
                scope,
                receive,
                send,
                429,
                "Too many concurrent requests; retry shortly.",
                type_="rate_limit_error",
                code="concurrency_limit",
                headers={"Retry-After": "1"},
            )
            return

        if gate:
            await semaphore.acquire()
        try:
            await self._process_body(scope, receive, send, max_body, max_messages, max_tools)
        finally:
            if gate:
                semaphore.release()

    # -- body handling ----------------------------------------------------------

    async def _process_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        max_body: int,
        max_messages: int,
        max_tools: int,
    ) -> None:
        method = (scope.get("method") or "GET").upper()
        if method not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }

        # Fast path: reject oversized Content-Length before reading the body.
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > max_body:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    f"Request body of {declared} bytes exceeds the {max_body} byte limit.",
                    code="request_too_large",
                )
                return

        # Accumulate the body (handles chunked requests without Content-Length).
        chunks: list[bytes] = []
        total = 0
        complete = False
        overflow = False
        while not complete:
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.request":
                body = message.get("body") or b""
                total += len(body)
                if total > max_body:
                    overflow = True
                    break
                chunks.append(body)
                if not message.get("more_body", False):
                    complete = True
            elif mtype == "http.disconnect":
                # Client vanished before the body was complete; nothing to answer.
                return

        if overflow:
            await self._reject(
                scope,
                receive,
                send,
                413,
                f"Request body exceeds the {max_body} byte limit.",
                code="request_too_large",
            )
            return

        body_bytes = b"".join(chunks)
        path = scope.get("path") or ""
        if complete and path == "/v1/chat/completions":
            problem = self._chat_schema_problem(body_bytes, max_messages, max_tools)
            if problem is not None:
                param, code, message_text = problem
                await self._reject(
                    scope,
                    receive,
                    send,
                    400,
                    message_text,
                    code=code,
                    param=param,
                )
                return

        # Replay the cached body downstream, then fall through to the original
        # receive channel (so disconnects during the response still propagate).
        pending: list[Message] = [{"type": "http.request", "body": body_bytes, "more_body": False}]

        async def cached_receive() -> Message:
            if pending:
                return pending.pop(0)
            return await receive()

        await self.app(scope, cached_receive, send)

    @staticmethod
    def _chat_schema_problem(
        body: bytes, max_messages: int, max_tools: int
    ) -> tuple[str, str, str] | None:
        """Return (param, code, message) when the chat body violates schema bounds."""
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Not valid JSON — FastAPI/pydantic owns that 422 path.
            return None
        if not isinstance(parsed, dict):
            return None
        messages = parsed.get("messages")
        if isinstance(messages, list) and len(messages) > max_messages:
            return (
                "messages",
                "too_many_messages",
                f"Request contains {len(messages)} messages; the limit is {max_messages}.",
            )
        tools = parsed.get("tools")
        if isinstance(tools, list) and len(tools) > max_tools:
            return (
                "tools",
                "too_many_tools",
                f"Request contains {len(tools)} tools; the limit is {max_tools}.",
            )
        return None

    # -- rejection helper ---------------------------------------------------------

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        code: str,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        rid = request_id_from_request(StarletteRequest(scope))
        response = openai_error_response(
            status_code,
            sanitize_client_error_message(message),
            type=type_,
            code=code,
            param=param,
            headers={**(headers or {}), REQUEST_ID_HEADER: rid},
        )
        await response(scope, receive, send)


__all__ = [
    "MAX_CLIENT_ERROR_MESSAGE_CHARS",
    "RequestBoundaryMiddleware",
    "bounded_error_response",
    "ensure_openai_handlers",
    "install_openai_exception_handlers",
    "openai_error_body",
    "openai_error_response",
    "sanitize_client_error_message",
]
