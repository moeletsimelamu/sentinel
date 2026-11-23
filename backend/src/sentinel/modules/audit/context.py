"""Who performed an action and from which request — passed into every write service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request

from sentinel.core.config import Settings, get_settings
from sentinel.core.security import hash_identifier


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID | None
    role: str | None

    @classmethod
    def system(cls) -> Actor:
        return cls(id=None, role="system")


@dataclass(frozen=True)
class RequestContext:
    request_id: str | None
    ip_hash: str | None

    @classmethod
    def offline(cls) -> RequestContext:
        return cls(request_id=None, ip_hash=None)


def get_request_context(
    request: Request, settings: Settings = Depends(get_settings)
) -> RequestContext:
    ip = request.client.host if request.client else None
    return RequestContext(
        request_id=getattr(request.state, "request_id", None),
        ip_hash=hash_identifier(settings, ip) if ip else None,
    )
