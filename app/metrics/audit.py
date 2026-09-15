"""Structured audit events for admin mutations (no secrets)."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("llm_router.audit")

_SECRET_KEYS = frozenset(
    {
        "key",
        "api_key",
        "raw_key",
        "token",
        "admin_token",
        "authorization",
        "password",
        "secret",
        "api_key_enc",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def scrub_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if k.lower() in _SECRET_KEYS:
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = scrub_detail(v)
        else:
            out[k] = v
    return out


@dataclass
class AuditEvent:
    timestamp: str
    action: str
    actor: str = "admin"
    request_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLog:
    """Process-local ring buffer + structured logger sink."""

    def __init__(self, *, maxlen: int = 500) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(
        self,
        action: str,
        *,
        actor: str = "admin",
        request_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            timestamp=_utc_now(),
            action=action,
            actor=actor,
            request_id=request_id,
            resource_type=resource_type,
            resource_id=None if resource_id is None else str(resource_id),
            detail=scrub_detail(detail),
        )
        with self._lock:
            self._events.append(event)
        logger.info(
            "audit action=%s actor=%s resource=%s/%s request_id=%s detail=%s",
            event.action,
            event.actor,
            event.resource_type,
            event.resource_id,
            event.request_id,
            event.detail,
        )
        return event

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._events)[-limit:]
        return [e.as_dict() for e in reversed(items)]
