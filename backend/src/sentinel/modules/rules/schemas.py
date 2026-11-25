from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sentinel.detection.rules import Severity


class RuleVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    params: dict[str, Any]
    weight: int
    severity: Severity
    force_alert: bool
    is_active: bool
    change_reason: str
    created_by_id: uuid.UUID | None
    created_at: datetime


class RuleOut(BaseModel):
    code: str
    name: str
    description: str
    category: str
    current: RuleVersionOut


class RuleDetail(RuleOut):
    params_schema: dict[str, Any]
    history: list[RuleVersionOut]


class RuleChange(BaseModel):
    """A change to one rule. Omitted fields keep their current values; ``params`` is
    merged over the current parameters and validated against the rule's schema."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="The version you are editing (409 if stale)")
    change_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)
    ]
    params: dict[str, Any] | None = None
    weight: int | None = Field(default=None, ge=0, le=100)
    severity: Severity | None = None
    force_alert: bool | None = None
    is_active: bool | None = None


class RuleSetMemberOut(BaseModel):
    code: str
    rule_version_id: uuid.UUID
    version: int
    is_active: bool


class RuleSetOut(BaseModel):
    id: uuid.UUID
    version: int
    change_reason: str
    created_by_id: uuid.UUID | None
    created_at: datetime
    members: list[RuleSetMemberOut]


class RuleSetPage(BaseModel):
    items: list[RuleSetOut]
    total: int
    limit: int
    offset: int


class RuleChangeResult(BaseModel):
    rule: RuleOut
    rule_set: RuleSetOut
