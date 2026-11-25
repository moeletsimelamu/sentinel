from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.detection.rules import CATALOGUE
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.rules import service
from sentinel.modules.rules.models import RuleDefinition, RuleSet, RuleVersion
from sentinel.modules.rules.schemas import (
    RuleChange,
    RuleChangeResult,
    RuleDetail,
    RuleOut,
    RuleSetMemberOut,
    RuleSetOut,
    RuleSetPage,
    RuleVersionOut,
)

rules_router = APIRouter(prefix="/rules", tags=["rules"])
rule_sets_router = APIRouter(prefix="/rule-sets", tags=["rules"])

_read = RequirePermission(Permission.RULES_READ)
_configure = RequirePermission(Permission.RULES_CONFIGURE)


def _rule_out(rule: RuleDefinition, current: RuleVersion) -> RuleOut:
    return RuleOut(
        code=rule.code,
        name=rule.name,
        description=rule.description,
        category=rule.category,
        current=RuleVersionOut.model_validate(current),
    )


def _rule_set_out(rs: RuleSet) -> RuleSetOut:
    members = sorted(rs.members, key=lambda m: m.rule_version.rule_id)
    return RuleSetOut(
        id=rs.id,
        version=rs.version,
        change_reason=rs.change_reason,
        created_by_id=rs.created_by_id,
        created_at=rs.created_at,
        members=[
            RuleSetMemberOut(
                code=m.rule_version.rule.code,
                rule_version_id=m.rule_version_id,
                version=m.rule_version.version,
                is_active=m.rule_version.is_active,
            )
            for m in members
        ],
    )


@rules_router.get("", response_model=list[RuleOut], dependencies=[Depends(_read)])
def list_rules(db: Session = Depends(get_db)) -> list[RuleOut]:
    return [_rule_out(d, v) for d, v in service.list_rules(db)]


@rules_router.get("/{code}", response_model=RuleDetail, dependencies=[Depends(_read)])
def get_rule(code: str, db: Session = Depends(get_db)) -> RuleDetail:
    rule = service.get_definition(db, code)
    versions = service.history(db, rule)
    base = _rule_out(rule, versions[0])
    return RuleDetail(
        **base.model_dump(),
        params_schema=CATALOGUE[rule.code].params_schema.model_json_schema(),
        history=[RuleVersionOut.model_validate(v) for v in versions],
    )


@rules_router.post(
    "/{code}/versions", response_model=RuleChangeResult, status_code=status.HTTP_201_CREATED
)
def change_rule(
    code: str,
    body: RuleChange,
    by: Principal = Depends(_configure),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> RuleChangeResult:
    """Create a new version of a rule (parameters, weight, severity, force_alert, active)."""
    rule, version, rule_set = service.create_version(db, code, body, by, ctx)
    return RuleChangeResult(rule=_rule_out(rule, version), rule_set=_rule_set_out(rule_set))


@rule_sets_router.get("", response_model=RuleSetPage, dependencies=[Depends(_read)])
def list_rule_sets(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> RuleSetPage:
    rows, total = service.list_rule_sets(db, limit=limit, offset=offset)
    return RuleSetPage(
        items=[_rule_set_out(r) for r in rows], total=total, limit=limit, offset=offset
    )


@rule_sets_router.get("/current", response_model=RuleSetOut, dependencies=[Depends(_read)])
def current_rule_set(db: Session = Depends(get_db)) -> RuleSetOut:
    return _rule_set_out(service.current_rule_set(db))


@rule_sets_router.get("/{rule_set_id}", response_model=RuleSetOut, dependencies=[Depends(_read)])
def get_rule_set(rule_set_id: uuid.UUID, db: Session = Depends(get_db)) -> RuleSetOut:
    return _rule_set_out(service.get_rule_set(db, rule_set_id))
