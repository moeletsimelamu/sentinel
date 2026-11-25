"""Rule configuration. Every change creates a new rule version and a new rule-set
snapshot in one transaction, with a mandatory reason, and is audited."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sentinel.core.errors import ConflictError, NotFoundError, ValidationFailedError
from sentinel.detection.engine import (
    ConfiguredRule,
    InvalidRuleParamsError,
    UnknownRuleError,
    validate_params,
)
from sentinel.detection.rules import Severity
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.rules.models import RuleDefinition, RuleSet, RuleSetMember, RuleVersion
from sentinel.modules.rules.schemas import RuleChange

# Serialises rule-set snapshots so two concurrent edits cannot each miss the other.
_RULE_SET_LOCK = 0x53_45_4E_54_52_55_4C  # arbitrary constant ("SENTRUL")


def current_versions(db: Session) -> dict[int, RuleVersion]:
    """Latest version of every rule, keyed by rule id."""
    latest = (
        select(RuleVersion.rule_id, func.max(RuleVersion.version).label("v"))
        .group_by(RuleVersion.rule_id)
        .subquery()
    )
    rows = db.scalars(
        select(RuleVersion).join(
            latest,
            (RuleVersion.rule_id == latest.c.rule_id) & (RuleVersion.version == latest.c.v),
        )
    ).all()
    return {r.rule_id: r for r in rows}


def list_rules(db: Session) -> list[tuple[RuleDefinition, RuleVersion]]:
    defs = db.scalars(select(RuleDefinition).order_by(RuleDefinition.id)).all()
    versions = current_versions(db)
    return [(d, versions[d.id]) for d in defs]


def get_definition(db: Session, code: str) -> RuleDefinition:
    rule = db.scalar(select(RuleDefinition).where(RuleDefinition.code == code))
    if rule is None:
        raise NotFoundError("Rule not found")
    return rule


def history(db: Session, rule: RuleDefinition) -> Sequence[RuleVersion]:
    return db.scalars(
        select(RuleVersion)
        .where(RuleVersion.rule_id == rule.id)
        .order_by(RuleVersion.version.desc())
    ).all()


def current_rule_set(db: Session) -> RuleSet:
    rs = db.scalar(select(RuleSet).order_by(RuleSet.version.desc()).limit(1))
    if rs is None:  # pragma: no cover - seeded by migration 0003
        raise NotFoundError("No rule set configured")
    return rs


def get_rule_set(db: Session, rule_set_id: Any) -> RuleSet:
    rs = db.get(RuleSet, rule_set_id)
    if rs is None:
        raise NotFoundError("Rule set not found")
    return rs


def list_rule_sets(db: Session, *, limit: int, offset: int) -> tuple[Sequence[RuleSet], int]:
    total = db.scalar(select(func.count()).select_from(RuleSet)) or 0
    rows = db.scalars(
        select(RuleSet).order_by(RuleSet.version.desc()).limit(limit).offset(offset)
    ).all()
    return rows, total


def configured_rules(rule_set: RuleSet) -> list[ConfiguredRule]:
    """Turn a stored rule set into engine input. Params are re-validated on load."""
    out = []
    for member in sorted(rule_set.members, key=lambda m: m.rule_version.rule_id):
        v = member.rule_version
        out.append(
            ConfiguredRule(
                rule_version_id=v.id,
                code=v.rule.code,
                params=validate_params(v.rule.code, v.params),
                weight=v.weight,
                severity=Severity(v.severity),
                force_alert=v.force_alert,
                is_active=v.is_active,
            )
        )
    return out


def _snapshot(v: RuleVersion) -> dict[str, Any]:
    return {
        "version": v.version,
        "params": v.params,
        "weight": v.weight,
        "severity": v.severity,
        "force_alert": v.force_alert,
        "is_active": v.is_active,
    }


def create_version(
    db: Session, code: str, change: RuleChange, by: Principal, ctx: RequestContext
) -> tuple[RuleDefinition, RuleVersion, RuleSet]:
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _RULE_SET_LOCK})
    rule = get_definition(db, code)
    versions = current_versions(db)
    current = versions[rule.id]
    if change.expected_version != current.version:
        raise ConflictError(
            "This rule was changed by someone else. Reload and try again.",
            code="stale_version",
            details={"current_version": current.version},
        )

    params = {**current.params, **(change.params or {})}
    try:
        validated = validate_params(code, params)
    except UnknownRuleError:  # pragma: no cover - definitions mirror the catalogue
        raise NotFoundError("Rule not found") from None
    except InvalidRuleParamsError as exc:
        raise ValidationFailedError(
            "Invalid rule parameters", code="invalid_params", details=exc.errors
        ) from None

    new = RuleVersion(
        rule_id=rule.id,
        version=current.version + 1,
        params=validated.model_dump(),
        weight=change.weight if change.weight is not None else current.weight,
        severity=(change.severity or Severity(current.severity)).value,
        force_alert=change.force_alert if change.force_alert is not None else current.force_alert,
        is_active=change.is_active if change.is_active is not None else current.is_active,
        change_reason=change.change_reason,
        created_by_id=by.id,
    )
    before, after = audit.diff(_snapshot(current), _snapshot(new))
    if set(before) == {"version"}:
        raise ValidationFailedError("The change does not modify the rule", code="no_changes")

    db.add(new)
    db.flush()
    versions[rule.id] = new
    latest_set = current_rule_set(db)
    rule_set = RuleSet(
        version=latest_set.version + 1,
        change_reason=f"{code} v{new.version}: {change.change_reason}",
        created_by_id=by.id,
    )
    db.add(rule_set)
    db.flush()
    for v in versions.values():
        db.add(RuleSetMember(rule_set_id=rule_set.id, rule_version_id=v.id))

    actor = Actor(by.id, by.role.value)
    audit.record(
        db,
        actor=actor,
        ctx=ctx,
        action="rule.version_created",
        entity_type="rule",
        entity_id=code,
        before=before,
        after={
            **after,
            "change_reason": change.change_reason,
            "rule_set_version": rule_set.version,
        },
    )
    db.commit()
    db.refresh(rule_set)
    return rule, new, rule_set
