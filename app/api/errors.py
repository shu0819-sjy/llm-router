"""OpenAI-compatible error envelope helpers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


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
                content=unwrapped,
                headers=headers,
            )
        message = detail if isinstance(detail, str) else str(detail)
        type_ = "invalid_request_error" if status_code < 500 else "api_error"
        return openai_error_response(
            status_code,
            message,
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
            msg,
            type="invalid_request_error",
            code="invalid_request",
            param=loc or None,
        )

    app.state._openai_error_handlers_installed = True


def ensure_openai_handlers(request: Request) -> None:
    """Router dependency: install OpenAI error envelopes on first /v1 hit."""
    install_openai_exception_handlers(request.app)
