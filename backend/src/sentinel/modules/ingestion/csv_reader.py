"""CSV decoding and file-level checks.

Pure: bytes in, rows out. File-level problems (encoding, header, size) raise
``FileRejectedError`` and reject the whole file; row-level problems are left to
``validation.validate_record`` so they can be reported line by line.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from sentinel.modules.ingestion.validation import (
    KNOWN_FIELDS,
    REQUIRED_FIELDS,
    is_forbidden_field,
    normalise_field_name,
)

MAX_FIELD_CHARS = 4096
MAX_COLUMNS = 64


class FileRejectedError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class CsvRow:
    line_number: int
    values: dict[str, str]
    # Set when the row had more or fewer cells than the header.
    shape_error: str | None = None


@dataclass
class ParsedCsv:
    columns: list[str]
    ignored_columns: list[str]
    rows: list[CsvRow] = field(default_factory=list)


def parse_csv(content: bytes, *, max_rows: int) -> ParsedCsv:
    if not content.strip():
        raise FileRejectedError("empty_file", "The file is empty")
    if b"\x00" in content:
        raise FileRejectedError("invalid_encoding", "The file contains NUL bytes; is it a CSV?")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejectedError(
            "invalid_encoding",
            "The file must be UTF-8 encoded",
            {"byte_offset": exc.start},
        ) from None

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover - guarded by the empty check above
        raise FileRejectedError("empty_file", "The file is empty") from None
    except csv.Error as exc:
        raise FileRejectedError("malformed_csv", f"Unreadable header: {exc}") from None

    if len(header) > MAX_COLUMNS:
        raise FileRejectedError("too_many_columns", f"At most {MAX_COLUMNS} columns are allowed")
    columns = [normalise_field_name(h) for h in header]
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise FileRejectedError(
            "duplicate_columns", "Column names must be unique", {"columns": duplicates}
        )
    forbidden = sorted(h.strip() for h in header if is_forbidden_field(h))
    if forbidden:
        raise FileRejectedError(
            "forbidden_columns",
            "The file contains columns for card, bank-credential or personal data, which "
            "Sentinel does not accept. Remove them and upload again.",
            {"columns": forbidden},
        )
    missing = [f for f in REQUIRED_FIELDS if f not in columns]
    if missing:
        raise FileRejectedError(
            "missing_columns", "Required columns are missing", {"columns": missing}
        )
    ignored = sorted(c for c in columns if c not in KNOWN_FIELDS)

    parsed = ParsedCsv(columns=columns, ignored_columns=ignored)
    width = len(columns)
    try:
        for cells in reader:
            if not cells or all(not c.strip() for c in cells):
                continue  # blank line
            if len(parsed.rows) >= max_rows:
                raise FileRejectedError(
                    "too_many_rows", f"At most {max_rows:,} data rows per file are allowed"
                )
            if any(len(c) > MAX_FIELD_CHARS for c in cells):
                raise FileRejectedError(
                    "malformed_csv",
                    f"A value on line {reader.line_num} exceeds {MAX_FIELD_CHARS} characters",
                )
            shape_error = None
            if len(cells) != width:
                shape_error = f"Expected {width} values, found {len(cells)}"
            values = {
                col: cells[i]
                for i, col in enumerate(columns)
                if col in KNOWN_FIELDS and i < len(cells)
            }
            parsed.rows.append(CsvRow(reader.line_num, values, shape_error))
    except csv.Error as exc:
        raise FileRejectedError(
            "malformed_csv", f"Could not parse line {reader.line_num}: {exc}"
        ) from None
    if not parsed.rows:
        raise FileRejectedError("no_rows", "The file has a header but no data rows")
    return parsed
