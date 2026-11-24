"""CSV and REST ingestion end to end: upload → worker → batch summary → row-issue report."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.permissions import Role
from sentinel.main import app
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.ingestion.models import ImportBatch, ImportFile
from sentinel.modules.ingestion.router import csv_safe
from sentinel.modules.transactions.models import Customer, Transaction
from sentinel.worker import default_handlers, process_next
from tests.conftest import AuthHeaders

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transactions_malformed.csv"
HEADER = (
    "external_id,customer_ref,occurred_at,amount,currency,txn_type,channel,merchant_category,status"
)
CSV = {"Content-Type": "text/csv"}

Upload = Callable[..., dict[str, Any]]


def run_worker(db: Session) -> int:
    """Drain the queue in-process, as the worker would."""
    settings = get_settings()
    handlers = default_handlers(settings)
    n = 0
    while process_next(db, handlers, settings=settings, worker="test-worker"):
        n += 1
    return n


@pytest.fixture
def analyst(auth_headers: AuthHeaders) -> dict[str, str]:
    return auth_headers(Role.ANALYST)


@pytest.fixture
def upload(client: TestClient, analyst: dict[str, str], db: Session) -> Upload:
    def _upload(content: bytes, *, process: bool = True, **params: str) -> dict[str, Any]:
        resp = client.post(
            "/api/v1/imports",
            content=content,
            headers={**analyst, **CSV},
            params={"filename": "upload.csv", **params},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "queued"
        if process:
            run_worker(db)
            resp = client.get(f"/api/v1/imports/{resp.json()['id']}", headers=analyst)
        body: dict[str, Any] = resp.json()
        return body

    return _upload


def _issues(client: TestClient, headers: dict[str, str], batch_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/api/v1/imports/{batch_id}/issues", headers=headers)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


# ------------------------------------------------------------------- CSV


def test_malformed_fixture_import_summary(
    client: TestClient, analyst: dict[str, str], upload: Upload, db: Session
) -> None:
    batch = upload(FIXTURE.read_bytes())
    assert batch["status"] == "completed"
    assert batch["row_count"] == 15
    assert batch["accepted_count"] == 5
    assert batch["rejected_count"] == 9
    assert batch["duplicate_count"] == 1
    assert batch["possible_duplicate_count"] == 1
    assert batch["ignored_columns"] == ["is_fraud"]
    assert batch["file_error"] is None
    assert batch["started_at"] and batch["finished_at"]

    by_line = {i["row_number"]: i for i in _issues(client, analyst, batch["id"])}
    expected = {
        5: ("rejected", "not_positive"),
        6: ("rejected", "invalid_choice"),
        7: ("rejected", "required"),
        8: ("rejected", "missing_timezone"),
        9: ("duplicate", "duplicate_in_file"),
        10: ("possible_duplicate", "same_content_in_file"),
        11: ("rejected", "invalid_choice"),
        12: ("rejected", "out_of_range"),
        13: ("rejected", "in_future"),
        14: ("rejected", "sensitive_data_detected"),
        15: ("rejected", "malformed_row"),
    }
    assert {n: (i["outcome"], i["reasons"][0]["code"]) for n, i in by_line.items()} == expected
    # Field-level detail and redaction in the report
    assert by_line[5]["reasons"][0]["field"] == "amount"
    assert by_line[5]["raw_row"]["amount"] == "-15.00"
    assert by_line[14]["raw_row"]["city"] == "[REDACTED]"
    assert by_line[14]["raw_row"]["device_id"] == "[REDACTED]"
    assert "is_fraud" not in by_line[5]["raw_row"]  # label columns are never stored
    assert by_line[10]["transaction_id"] is not None  # accepted, but flagged
    assert by_line[9]["raw_row"] is None

    # Accepted rows are stored and normalised; customers are created pseudonymously
    txns = db.scalars(
        select(Transaction).where(Transaction.batch_id == uuid.UUID(batch["id"]))
    ).all()
    assert sorted(t.external_id for t in txns) == [
        "TX-0001",
        "TX-0002",
        "TX-0003",
        "TX-0008",
        "TX-0014",
    ]
    tx2 = next(t for t in txns if t.external_id == "TX-0002")
    assert tx2.currency == "ZAR"
    assert tx2.occurred_at.astimezone(UTC).hour == 11  # 13:40+02:00 stored as 11:40 UTC
    assert tx2.device_id_hash and "dev-a1" not in tx2.device_id_hash
    refs = set(db.scalars(select(Customer.external_ref)))
    assert {"C-1001", "C-1002", "C-1006"} <= refs

    # Raw upload is purged after processing
    assert db.get(ImportFile, uuid.UUID(batch["id"])) is None


def test_reimporting_the_same_file_is_idempotent(upload: Upload, db: Session) -> None:
    first = upload(FIXTURE.read_bytes())
    second = upload(FIXTURE.read_bytes())
    assert second["identical_to_batch_id"] == first["id"]
    assert second["accepted_count"] == 0
    # The 5 accepted rows + the in-file duplicate are all duplicates now
    assert second["duplicate_count"] == 6
    assert second["rejected_count"] == 9
    count = db.scalar(
        text("SELECT count(*) FROM transactions WHERE source_system = :s"),
        {"s": "sentinel_generator"},
    )
    assert count == 5


def test_same_ids_from_another_source_system_are_separate(upload: Upload) -> None:
    upload(FIXTURE.read_bytes())
    other = upload(FIXTURE.read_bytes(), source_system="branch_feed")
    assert other["accepted_count"] == 5
    # Content now matches the first system's rows → all flagged as possible duplicates
    assert other["possible_duplicate_count"] == 5


def test_possible_duplicate_against_existing_data(
    client: TestClient, analyst: dict[str, str], upload: Upload
) -> None:
    row = "TX-A,C-9,2026-04-01T10:00:00Z,10.00,ZAR,purchase,online,groceries,completed"
    upload(f"{HEADER}\n{row}\n".encode())
    batch = upload(f"{HEADER}\n{row.replace('TX-A', 'TX-B')}\n".encode())
    assert batch["accepted_count"] == 1 and batch["possible_duplicate_count"] == 1
    (issue,) = _issues(client, analyst, batch["id"])
    assert issue["reasons"][0]["code"] == "same_content_as_existing"
    assert issue["reasons"][0]["related_transaction_id"]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"external_id,amount\nTX-1,1\n", "missing_columns"),
        (f"{HEADER},card_number\nx\n".encode(), "forbidden_columns"),
        ("é".encode("latin-1") + b"\n", "invalid_encoding"),
    ],
)
def test_unusable_file_is_rejected_as_a_whole(
    upload: Upload, db: Session, content: bytes, code: str
) -> None:
    batch = upload(content)
    assert batch["status"] == "rejected"
    assert batch["file_error"]["code"] == code
    assert batch["accepted_count"] == 0
    assert db.get(ImportFile, uuid.UUID(batch["id"])) is None


def test_upload_requires_csv_content_type(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/imports", content=b"a,b\n", headers={**analyst, "Content-Type": "image/png"}
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"


def test_upload_size_limit(client: TestClient, analyst: dict[str, str]) -> None:
    small = get_settings().model_copy(update={"import_max_bytes": 1024})

    def _settings() -> Settings:
        return small

    app.dependency_overrides[get_settings] = _settings
    resp = client.post("/api/v1/imports", content=b"x" * 2048, headers={**analyst, **CSV})
    assert resp.status_code == 413
    assert resp.json()["error"]["details"] == {"max_bytes": 1024}


def test_empty_upload_rejected(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.post("/api/v1/imports", content=b"  \n", headers={**analyst, **CSV})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_file"


def test_invalid_source_system(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/imports",
        content=FIXTURE.read_bytes(),
        headers={**analyst, **CSV},
        params={"source_system": "Not Valid!"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_source_system"


def test_filename_is_sanitised(upload: Upload) -> None:
    batch = upload(FIXTURE.read_bytes(), process=False, filename="..\\..\\evil\x07.csv")
    assert batch["filename"] == "evil.csv"


def test_upload_and_completion_are_audited(upload: Upload, db: Session) -> None:
    batch = upload(FIXTURE.read_bytes())
    entries = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == batch["id"]).order_by(AuditLog.id)
    ).all()
    assert [e.action for e in entries] == ["import.uploaded", "import.completed"]
    assert entries[0].actor_role == "analyst"
    assert entries[1].actor_role == "system"
    assert entries[1].after is not None and entries[1].after["accepted_count"] == 5


def test_issue_report_filter_pagination_and_csv_export(
    client: TestClient, analyst: dict[str, str], upload: Upload
) -> None:
    batch = upload(FIXTURE.read_bytes())
    url = f"/api/v1/imports/{batch['id']}/issues"
    page = client.get(url, params={"outcome": "rejected", "limit": 4}, headers=analyst).json()
    assert page["total"] == 9 and len(page["items"]) == 4
    assert all(i["outcome"] == "rejected" for i in page["items"])

    export = client.get(f"{url}.csv", headers=analyst)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    lines = export.text.strip().splitlines()
    assert lines[0] == "row_number,outcome,external_id,codes,messages"
    assert len(lines) == 1 + 11
    assert "4111" not in export.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("=SUM(A1)", "'=SUM(A1)"), ("+1", "'+1"), ("@x", "'@x"), ("TX-1", "TX-1"), (None, "")],
)
def test_export_cells_neutralise_formulas(value: str | None, expected: str) -> None:
    assert csv_safe(value) == expected


def test_import_history_lists_newest_first(
    client: TestClient, analyst: dict[str, str], upload: Upload, auth_headers: AuthHeaders
) -> None:
    first = upload(FIXTURE.read_bytes())
    second = upload(b"nonsense", process=True)
    auditor = auth_headers(Role.AUDITOR)
    page = client.get("/api/v1/imports", headers=auditor).json()
    ids = [b["id"] for b in page["items"]]
    assert ids.index(second["id"]) < ids.index(first["id"])
    rejected = client.get("/api/v1/imports", params={"status": "rejected"}, headers=auditor)
    assert all(b["status"] == "rejected" for b in rejected.json()["items"])


def test_unknown_import_is_404(client: TestClient, analyst: dict[str, str]) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/imports/{missing}", headers=analyst).status_code == 404
    assert client.get(f"/api/v1/imports/{missing}/issues.csv", headers=analyst).status_code == 404


def test_reprocessing_a_finished_batch_is_a_no_op(upload: Upload, db: Session) -> None:
    from sentinel.modules.ingestion import service

    batch = upload(FIXTURE.read_bytes())
    service.process_csv_batch(db, get_settings(), {"batch_id": batch["id"]})
    stored = db.get(ImportBatch, uuid.UUID(batch["id"]))
    assert stored is not None and stored.accepted_count == 5


# ------------------------------------------------------------------- REST


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "external_id": "API-1",
        "customer_ref": "C-API",
        "occurred_at": "2026-05-01T08:00:00Z",
        "amount": "19.99",
        "currency": "EUR",
        "txn_type": "purchase",
        "channel": "online",
        "merchant_category": "subscriptions",
        "status": "completed",
    }
    record.update(overrides)
    return record


def test_rest_ingestion_reports_each_record(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/transactions",
        headers=analyst,
        json={
            "source_system": "partner_api",
            "records": [
                _record(),
                _record(external_id="API-2", amount=-1),
                _record(external_id="API-1"),
                _record(external_id="API-3", card_number="4111111111111111"),
                _record(external_id="API-4", amount=42.1, extra_field="ignored"),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch"]["source"] == "api"
    assert body["batch"]["accepted_count"] == 2
    assert body["batch"]["rejected_count"] == 2
    assert body["batch"]["duplicate_count"] == 1
    assert body["batch"]["ignored_columns"] == ["extra_field"]
    codes = {i["row_number"]: i["reasons"][0]["code"] for i in body["issues"]}
    assert codes == {2: "not_positive", 3: "duplicate_in_file", 4: "forbidden_field"}
    assert "4111111111111111" not in resp.text

    listed = client.get(
        "/api/v1/transactions", params={"batch_id": body["batch"]["id"]}, headers=analyst
    ).json()
    amounts = {t["external_id"]: t["amount"] for t in listed["items"]}
    assert amounts == {"API-1": "19.99", "API-4": "42.10"}


def test_rest_ingestion_record_limit(client: TestClient, analyst: dict[str, str]) -> None:
    many = [
        _record(external_id=f"API-{i}") for i in range(get_settings().api_ingest_max_records + 1)
    ]
    resp = client.post(
        "/api/v1/transactions",
        headers=analyst,
        json={"source_system": "partner_api", "records": many},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "too_many_records"


def test_rest_ingestion_request_shape(client: TestClient, analyst: dict[str, str]) -> None:
    for body in (
        {"source_system": "partner_api", "records": []},
        {"source_system": "BAD NAME", "records": [_record()]},
        {"source_system": "partner_api", "records": [_record()], "unexpected": 1},
    ):
        assert client.post("/api/v1/transactions", headers=analyst, json=body).status_code == 422


# ------------------------------------------------------------- transactions


def test_transaction_list_filters_and_keyset_pagination(
    client: TestClient, analyst: dict[str, str], upload: Upload
) -> None:
    rows = "\n".join(
        f"TX-P{i},C-PAGE,2026-06-0{i}T10:00:00Z,{i}.00,ZAR,purchase,online,groceries,completed"
        for i in range(1, 6)
    )
    upload(f"{HEADER}\n{rows}\n".encode())
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"customer_ref": "C-PAGE", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/transactions", params=params, headers=analyst).json()
        seen.extend(t["external_id"] for t in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ["TX-P5", "TX-P4", "TX-P3", "TX-P2", "TX-P1"]

    filtered = client.get(
        "/api/v1/transactions",
        params={"customer_ref": "C-PAGE", "min_amount": "2", "max_amount": "3"},
        headers=analyst,
    ).json()
    assert [t["external_id"] for t in filtered["items"]] == ["TX-P3", "TX-P2"]

    detail = client.get(f"/api/v1/transactions/{filtered['items'][0]['id']}", headers=analyst)
    assert detail.status_code == 200
    assert detail.json()["customer_ref"] == "C-PAGE"
    assert "device_id_hash" not in detail.json()


def test_bad_cursor_is_422(client: TestClient, analyst: dict[str, str]) -> None:
    resp = client.get("/api/v1/transactions", params={"cursor": "not-a-cursor"}, headers=analyst)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_transactions_are_immutable(upload: Upload, db: Session) -> None:
    upload(FIXTURE.read_bytes())
    txn = db.scalars(select(Transaction).limit(1)).one()
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("UPDATE transactions SET amount = 1 WHERE id = :id"), {"id": txn.id})
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": txn.id})
