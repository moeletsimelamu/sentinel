"""Ingestion endpoints.

CSV upload takes the file as the raw request body (``Content-Type: text/csv``) rather
than multipart: it streams with a hard size cap and needs no extra parser dependency.
The permission check runs before a single byte of the body is read.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_db
from sentinel.core.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from sentinel.core.permissions import Permission
from sentinel.core.rate_limit import ingestion_limit, limiter
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.ingestion import service
from sentinel.modules.ingestion.models import BatchStatus, ImportSource, IssueOutcome
from sentinel.modules.ingestion.schemas import (
    ApiIngestRequest,
    ApiIngestResult,
    ImportBatchOut,
    ImportBatchPage,
    IssueReason,
    RowIssueOut,
    RowIssuePage,
)
from sentinel.modules.ingestion.validation import is_valid_source_system

imports_router = APIRouter(prefix="/imports", tags=["ingestion"])
ingest_router = APIRouter(prefix="/transactions", tags=["ingestion"])

_import = RequirePermission(Permission.INGESTION_IMPORT)
_read = RequirePermission(Permission.TRANSACTIONS_READ)

CSV_MEDIA_TYPES = frozenset(
    {
        "text/csv",
        "application/csv",
        "text/plain",
        "application/vnd.ms-excel",  # what Windows browsers send for .csv files
        "application/octet-stream",
    }
)


@dataclass(frozen=True)
class CsvUpload:
    content: bytes
    principal: Principal


async def read_csv_upload(
    request: Request,
    principal: Principal = Depends(_import),
    settings: Settings = Depends(get_settings),
) -> CsvUpload:
    """Read the body in chunks, enforcing the size limit while streaming."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in CSV_MEDIA_TYPES:
        raise UnsupportedMediaTypeError(
            "Send the CSV file as the request body with Content-Type: text/csv"
        )
    limit = settings.import_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(
            f"The file exceeds the {limit // (1024 * 1024)} MB limit",
            details={"max_bytes": limit},
        )
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise PayloadTooLargeError(
                f"The file exceeds the {limit // (1024 * 1024)} MB limit",
                details={"max_bytes": limit},
            )
    if not bytes(buffer).strip():
        raise ValidationFailedError("The file is empty", code="empty_file")
    return CsvUpload(bytes(buffer), principal)


# ------------------------------------------------------------------- imports


@imports_router.post("", response_model=ImportBatchOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(ingestion_limit)
def upload_csv(
    request: Request,
    upload: CsvUpload = Depends(read_csv_upload),
    source_system: str = Query(default="sentinel_generator", max_length=32),
    filename: str | None = Query(default=None, max_length=255),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> ImportBatchOut:
    """Queue a CSV file for validation. Poll ``GET /imports/{id}`` for the result."""
    source_system = source_system.strip()
    if not is_valid_source_system(source_system):
        raise ValidationFailedError(
            "source_system must be 2-32 lowercase letters, digits, '_' or '-'",
            code="invalid_source_system",
        )
    batch = service.create_csv_import(
        db,
        settings,
        content=upload.content,
        filename=filename,
        source_system=source_system,
        by=upload.principal,
        ctx=ctx,
    )
    return ImportBatchOut.model_validate(batch)


@imports_router.get("", response_model=ImportBatchPage, dependencies=[Depends(_read)])
def list_imports(
    status: BatchStatus | None = None,
    source: ImportSource | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ImportBatchPage:
    rows, total = service.list_batches(db, status=status, source=source, limit=limit, offset=offset)
    return ImportBatchPage(
        items=[ImportBatchOut.model_validate(b) for b in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@imports_router.get("/{batch_id}", response_model=ImportBatchOut, dependencies=[Depends(_read)])
def get_import(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> ImportBatchOut:
    return ImportBatchOut.model_validate(service.get_batch(db, batch_id))


@imports_router.get(
    "/{batch_id}/issues", response_model=RowIssuePage, dependencies=[Depends(_read)]
)
def list_import_issues(
    batch_id: uuid.UUID,
    outcome: IssueOutcome | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> RowIssuePage:
    """The rejected-record report: rejected rows, skipped duplicates, possible duplicates."""
    rows, total = service.list_issues(db, batch_id, outcome=outcome, limit=limit, offset=offset)
    return RowIssuePage(
        items=[RowIssueOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@imports_router.get(
    "/{batch_id}/issues.csv",
    response_class=Response,
    dependencies=[Depends(_read)],
)
def export_import_issues(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    """Download the row-issue report as CSV (built in memory; bounded by the row limit)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row_number", "outcome", "external_id", "codes", "messages"])
    for issue in service.iter_issues(db, batch_id):
        writer.writerow(
            [
                issue.row_number,
                issue.outcome,
                csv_safe(issue.external_id),
                csv_safe("; ".join(r["code"] for r in issue.reasons)),
                csv_safe("; ".join(r["message"] for r in issue.reasons)),
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="import-{batch_id}-issues.csv"'},
    )


# --------------------------------------------------------------- REST ingest


@ingest_router.post("", response_model=ApiIngestResult)
@limiter.limit(ingestion_limit)
def ingest_transactions(
    request: Request,
    body: ApiIngestRequest,
    by: Principal = Depends(_import),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> ApiIngestResult:
    """Validate and store up to ``api_ingest_max_records`` transactions synchronously."""
    batch, outcome = service.ingest_api_records(
        db, settings, source_system=body.source_system, records=body.records, by=by, ctx=ctx
    )
    return ApiIngestResult(
        batch=ImportBatchOut.model_validate(batch),
        issues=[
            RowIssueOut(
                row_number=i.row_number,
                outcome=i.outcome,
                external_id=i.external_id,
                transaction_id=i.transaction_id,
                reasons=[IssueReason.model_validate(r) for r in i.reasons],
                raw_row=i.raw_row,
            )
            for i in outcome.issues
        ],
    )
