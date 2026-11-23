from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: int | None
