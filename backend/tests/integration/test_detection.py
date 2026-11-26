"""Import → features → rules → assessments, rule versioning, and re-scoring."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from sentinel.core.config import get_settings
from sentinel.core.permissions import Role
from sentinel.detection.rules import CATALOGUE
from sentinel.modules.alerts import jobs as alert_jobs
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.rules.models import RuleDefinition, RuleVersion
from tests.conftest import AuthHeaders
from tests.integration.test_ingestion import HEADER, run_worker

pytestmark = pytest.mark.integration

JHB = "Johannesburg,ZA,-26.2041,28.0473,dev-1"
ROWS = [
    *[
        f"H{d:02d},C-DET,2026-05-{d:02d}T12:00:00Z,100.00,ZAR,purchase,card_present,"
        f"groceries,completed,{JHB}"
        for d in range(1, 13)
    ],
    f"BIG,C-DET,2026-05-13T12:00:00Z,60000.00,ZAR,purchase,online,electronics,completed,{JHB}",
    "LON,C-DET,2026-05-13T12:30:00Z,150.00,ZAR,purchase,card_present,restaurants,completed,"
    "London,GB,51.5074,-0.1278,dev-1",
    "D1,C-DET,2026-05-14T09:00:00Z,50.00,ZAR,purchase,online,gift_cards,declined,,,,,",
    "D2,C-DET,2026-05-14T09:01:00Z,50.00,ZAR,purchase,online,gift_cards,declined,,,,,",
    "D3,C-DET,2026-05-14T09:02:00Z,50.00,ZAR,purchase,online,gift_cards,declined,,,,,",
    "OK,C-DET,2026-05-14T09:05:00Z,50.00,ZAR,purchase,online,gift_cards,completed,,,,,",
]
CSV_BODY = (HEADER + ",city,country,lat,lon,device_id\n" + "\n".join(ROWS) + "\n").encode()

EXPECTED_HITS = {
    "BIG": {"LARGE_AMOUNT_ABSOLUTE", "LARGE_AMOUNT_RELATIVE"},
    "LON": {"IMPOSSIBLE_TRAVEL"},
    "D1": {"HIGH_RISK_CATEGORY_SHIFT"},
    "D2": set(),
    "D3": {"RAPID_REPEAT"},
    "OK": {"FAILED_ATTEMPTS", "RAPID_REPEAT", "VELOCITY_RATIO_24H"},
}


@pytest.fixture
def analyst(auth_headers: AuthHeaders) -> dict[str, str]:
    return auth_headers(Role.ANALYST)


@pytest.fixture
def admin(auth_headers: AuthHeaders) -> dict[str, str]:
    return auth_headers(Role.ADMIN)


@pytest.fixture
def scored(client: TestClient, analyst: dict[str, str], db: Session) -> dict[str, Any]:
    """Upload the scenario and let the worker ingest and score it."""
    resp = client.post(
        "/api/v1/imports",
        content=CSV_BODY,
        headers={**analyst, "Content-Type": "text/csv"},
        params={"source_system": "detection_test"},
    )
    batch_id = resp.json()["id"]
    run_worker(db)
    batch = client.get(f"/api/v1/imports/{batch_id}", headers=analyst).json()
    txns = client.get(
        "/api/v1/transactions", params={"batch_id": batch_id, "limit": 200}, headers=analyst
    ).json()["items"]
    return {"batch": batch, "ids": {t["external_id"]: t["id"] for t in txns}}


def assessments(client: TestClient, headers: dict[str, str], txn_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/api/v1/transactions/{txn_id}/assessments", headers=headers)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()
    return items


def hit_codes(assessment: dict[str, Any]) -> set[str]:
    return {h["rule_code"] for h in assessment["hits"]}


def test_import_is_scored_end_to_end(
    client: TestClient, analyst: dict[str, str], scored: dict[str, Any]
) -> None:
    batch, ids = scored["batch"], scored["ids"]
    assert batch["accepted_count"] == len(ROWS)
    assert batch["scoring_job_id"]
    run = client.get(f"/api/v1/detection/runs/{batch['scoring_job_id']}", headers=analyst).json()
    assert run["status"] == "succeeded" and run["rescore"] is False
    assert run["result"]["assessed"] == len(ROWS)
    assert run["result"]["hits_by_rule"]["RAPID_REPEAT"] == 2

    for ext_id, expected in EXPECTED_HITS.items():
        (a,) = assessments(client, analyst, ids[ext_id])
        assert hit_codes(a) == expected, ext_id
        assert a["mode"] == "rules_only" and a["model_score"] is None
        assert a["feature_set_version"] == "behaviour-v2"

    for d in range(1, 13):
        (a,) = assessments(client, analyst, ids[f"H{d:02d}"])
        assert a["hits"] == [] and a["risk_score"] == 0 and a["risk_category"] == "low"


def test_assessment_content_is_explainable(
    client: TestClient, analyst: dict[str, str], scored: dict[str, Any]
) -> None:
    (big,) = assessments(client, analyst, scored["ids"]["BIG"])
    assert big["risk_score"] == 55 and big["risk_category"] == "medium"
    assert big["reasons"][0].startswith("Amount ZAR 60,000.00 is at or above")
    assert big["reasons"][1].startswith("Amount ZAR 60,000.00 is 600.0x this customer's")
    relative = next(h for h in big["hits"] if h["rule_code"] == "LARGE_AMOUNT_RELATIVE")
    assert relative["observed"] == {"amount_vs_median": 600.0, "history_count_90d": 12}
    assert relative["threshold"] == {"min_multiple": 8.0, "min_history": 10}
    assert big["features"]["cust_median_amount_zar_90d"] == 100.0
    assert all("fraud" not in r.lower() for r in big["reasons"])

    (lon,) = assessments(client, analyst, scored["ids"]["LON"])
    assert lon["force_alert"] is True and lon["risk_score"] == 35
    assert lon["features"]["km_from_prev_txn"] > 9000


def test_scoring_is_idempotent_on_retry(scored: dict[str, Any], db: Session) -> None:
    summary = alert_jobs.process_score_batch(
        db, get_settings(), {"batch_id": scored["batch"]["id"]}
    )
    assert summary["assessed"] == 0


# ------------------------------------------------------------ rule config


def test_rule_catalogue_endpoints(client: TestClient, auth_headers: AuthHeaders) -> None:
    auditor = auth_headers(Role.AUDITOR)
    rules = client.get("/api/v1/rules", headers=auditor).json()
    assert [r["code"] for r in rules] == list(CATALOGUE)
    assert all(r["current"]["version"] == 1 for r in rules)
    detail = client.get("/api/v1/rules/IMPOSSIBLE_TRAVEL", headers=auditor).json()
    assert detail["current"]["force_alert"] is True
    assert "min_speed_kmh" in detail["params_schema"]["properties"]
    assert client.get("/api/v1/rules/NOPE", headers=auditor).status_code == 404
    current = client.get("/api/v1/rule-sets/current", headers=auditor).json()
    assert current["version"] == 1 and len(current["members"]) == 12
    config = client.get("/api/v1/scoring-config", headers=auditor).json()
    assert config["version"] == 1 and config["alert_threshold"] == 70


def test_seeded_catalogue_matches_code(db: Session) -> None:
    rows = db.execute(
        select(RuleDefinition, RuleVersion)
        .join(RuleVersion, RuleVersion.rule_id == RuleDefinition.id)
        .where(RuleVersion.version == 1)
    ).all()
    seeded = {d.code: (d, v) for d, v in rows}
    assert set(seeded) == set(CATALOGUE)
    for code, rule in CATALOGUE.items():
        d, v = seeded[code]
        assert (d.name, d.description, d.category) == (rule.name, rule.description, rule.category)
        assert v.params == rule.params_schema().model_dump()
        assert (v.weight, v.severity, v.force_alert) == (
            rule.default_weight,
            rule.default_severity.value,
            rule.default_force_alert,
        )


def _change(client: TestClient, headers: dict[str, str], code: str, **body: Any) -> Any:
    payload = {"expected_version": 1, "change_reason": "Tuning after validation review", **body}
    return client.post(f"/api/v1/rules/{code}/versions", json=payload, headers=headers)


def test_rule_change_creates_version_and_rule_set_and_is_audited(
    client: TestClient, admin: dict[str, str], db: Session
) -> None:
    resp = _change(client, admin, "FAILED_ATTEMPTS", params={"min_failed": 4}, weight=30)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rule"]["current"]["version"] == 2
    assert body["rule"]["current"]["params"] == {"min_failed": 4}
    assert body["rule"]["current"]["weight"] == 30
    assert body["rule_set"]["version"] == 2
    member = next(m for m in body["rule_set"]["members"] if m["code"] == "FAILED_ATTEMPTS")
    assert member["version"] == 2
    others = [m["version"] for m in body["rule_set"]["members"] if m["code"] != "FAILED_ATTEMPTS"]
    assert others == [1] * 11

    detail = client.get("/api/v1/rules/FAILED_ATTEMPTS", headers=admin).json()
    assert [v["version"] for v in detail["history"]] == [2, 1]

    entry = db.scalars(select(AuditLog).where(AuditLog.action == "rule.version_created")).one()
    assert entry.entity_id == "FAILED_ATTEMPTS"
    assert entry.before == {"params": {"min_failed": 3}, "version": 1, "weight": 25}
    assert entry.after is not None and entry.after["params"] == {"min_failed": 4}
    assert entry.after["change_reason"] == "Tuning after validation review"


def test_rule_change_conflicts_and_validation(client: TestClient, admin: dict[str, str]) -> None:
    assert _change(client, admin, "VELOCITY_1H", is_active=False).status_code == 201
    stale = _change(client, admin, "VELOCITY_1H", is_active=True)  # still says version 1
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {"current_version": 2}

    bad = _change(client, admin, "UNUSUAL_HOUR", params={"min_rarity": 2})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_params"
    unknown_param = _change(client, admin, "UNUSUAL_HOUR", params={"surprise": 1})
    assert unknown_param.status_code == 422

    same = _change(client, admin, "UNUSUAL_HOUR", params={"min_rarity": 0.97})
    assert same.status_code == 422 and same.json()["error"]["code"] == "no_changes"

    short = _change(client, admin, "UNUSUAL_HOUR", weight=5, change_reason="why")
    assert short.status_code == 422
    assert _change(client, admin, "NOPE", weight=5).status_code == 404


def test_analysts_cannot_change_rules(client: TestClient, analyst: dict[str, str]) -> None:
    assert _change(client, analyst, "VELOCITY_1H", weight=1).status_code == 403


def test_rescoring_uses_the_new_rule_set_and_keeps_history(
    client: TestClient,
    analyst: dict[str, str],
    admin: dict[str, str],
    scored: dict[str, Any],
    db: Session,
) -> None:
    assert _change(client, admin, "FAILED_ATTEMPTS", params={"min_failed": 4}).status_code == 201
    run = client.post(
        "/api/v1/detection/runs", json={"batch_id": scored["batch"]["id"]}, headers=analyst
    )
    assert run.status_code == 202 and run.json()["status"] == "queued"
    run_worker(db)
    done = client.get(f"/api/v1/detection/runs/{run.json()['id']}", headers=analyst).json()
    assert done["status"] == "succeeded" and done["result"]["rule_set_version"] == 2
    assert done["result"]["assessed"] == len(ROWS)

    newest, original = assessments(client, analyst, scored["ids"]["OK"])
    assert hit_codes(original) == EXPECTED_HITS["OK"]
    assert hit_codes(newest) == EXPECTED_HITS["OK"] - {"FAILED_ATTEMPTS"}
    assert newest["rule_set_id"] != original["rule_set_id"]
    actions = db.scalars(select(AuditLog.action).where(AuditLog.action.like("detection.%"))).all()
    assert actions == ["detection.run_requested"]


def test_detection_run_for_unknown_batch(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/detection/runs", json={"batch_id": str(uuid.uuid4())}, headers=analyst
    )
    assert resp.status_code == 404


def test_auditors_cannot_start_runs(client: TestClient, auth_headers: AuthHeaders) -> None:
    resp = client.post(
        "/api/v1/detection/runs",
        json={"batch_id": str(uuid.uuid4())},
        headers=auth_headers(Role.AUDITOR),
    )
    assert resp.status_code == 403


# ------------------------------------------------------------ immutability


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE rule_versions SET weight = 0",
        "DELETE FROM rule_set_members",
        "UPDATE rule_sets SET change_reason = 'x'",
        "UPDATE risk_assessments SET risk_score = 0",
        "DELETE FROM rule_hits",
    ],
)
def test_detection_history_is_append_only(
    scored: dict[str, Any], db: Session, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text(statement))
