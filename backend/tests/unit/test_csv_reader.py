from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.modules.ingestion.csv_reader import FileRejectedError, parse_csv

HEADER = (
    "external_id,customer_ref,occurred_at,amount,currency,txn_type,channel,merchant_category,status"
)
ROW = "TX-1,C-1,2026-03-01T09:15:00Z,10.00,ZAR,purchase,online,groceries,completed"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transactions_malformed.csv"


def _parse(text: str | bytes, max_rows: int = 100) -> object:
    content = text.encode() if isinstance(text, str) else text
    return parse_csv(content, max_rows=max_rows)


def _rejected(text: str | bytes, max_rows: int = 100) -> FileRejectedError:
    with pytest.raises(FileRejectedError) as info:
        _parse(text, max_rows)
    return info.value


def test_parses_rows_with_line_numbers() -> None:
    parsed = parse_csv(f"{HEADER}\n{ROW}\n\n{ROW.replace('TX-1', 'TX-2')}\n".encode(), max_rows=10)
    assert [r.line_number for r in parsed.rows] == [2, 4]  # blank line 3 skipped
    assert parsed.rows[0].values["external_id"] == "TX-1"
    assert parsed.ignored_columns == []


def test_header_is_normalised_and_bom_stripped() -> None:
    header = HEADER.replace("external_id", " External ID ").replace("amount", "AMOUNT")
    parsed = parse_csv(b"\xef\xbb\xbf" + f"{header}\r\n{ROW}\r\n".encode(), max_rows=10)
    assert parsed.rows[0].values["amount"] == "10.00"
    assert "external_id" in parsed.columns


def test_unknown_columns_are_ignored_and_reported() -> None:
    parsed = parse_csv(f"{HEADER},is_fraud,notes\n{ROW},1,hello\n".encode(), max_rows=10)
    assert parsed.ignored_columns == ["is_fraud", "notes"]
    assert "is_fraud" not in parsed.rows[0].values


def test_wrong_cell_count_is_a_row_error_not_a_file_error() -> None:
    parsed = parse_csv(f"{HEADER}\nTX-1,C-1\n{ROW},extra\n".encode(), max_rows=10)
    assert parsed.rows[0].shape_error == "Expected 9 values, found 2"
    assert parsed.rows[1].shape_error == "Expected 9 values, found 10"


def test_quoted_values_with_commas_and_newlines() -> None:
    row = ROW.replace("TX-1", '"TX-1"')
    parsed = parse_csv(f'{HEADER},city\n{row},"Cape Town, ""CBD""\nNorth"\n'.encode(), max_rows=5)
    assert parsed.rows[0].values["city"] == 'Cape Town, "CBD"\nNorth'


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "empty_file"),
        (b"   \n  ", "empty_file"),
        (f"{HEADER}\n".encode(), "no_rows"),
        (b"external_id,amount\nTX-1,1\n", "missing_columns"),
        (f"{HEADER},Amount\n{ROW},1\n".encode(), "duplicate_columns"),
        (f"{HEADER},card_number\n{ROW},4111111111111111\n".encode(), "forbidden_columns"),
        (f"{HEADER},customer_name\n{ROW},Jane\n".encode(), "forbidden_columns"),
        (f"{HEADER}\n{ROW}\n".encode("utf-16"), "invalid_encoding"),
        (f"{HEADER}\n".encode() + b"\xff\xfe\xfa\n", "invalid_encoding"),
        (f'{HEADER}\n"TX-1,unterminated\n'.encode(), "malformed_csv"),
    ],
)
def test_file_level_rejections(content: bytes, code: str) -> None:
    assert _rejected(content).code == code


def test_missing_columns_are_listed() -> None:
    err = _rejected("external_id,customer_ref\nTX-1,C-1\n")
    assert "amount" in err.details["columns"] and "external_id" not in err.details["columns"]


def test_forbidden_columns_are_listed_but_values_never_echoed() -> None:
    err = _rejected(f"{HEADER},CVV\n{ROW},123\n")
    assert err.details == {"columns": ["CVV"]}
    assert "123" not in str(err.as_dict())


def test_row_limit() -> None:
    body = HEADER + "\n" + "\n".join(ROW.replace("TX-1", f"TX-{i}") for i in range(4)) + "\n"
    assert len(parse_csv(body.encode(), max_rows=4).rows) == 4
    assert _rejected(body, max_rows=3).code == "too_many_rows"


def test_oversized_cell_rejected() -> None:
    assert _rejected(f"{HEADER},city\n{ROW},{'x' * 5000}\n").code == "malformed_csv"


def test_fixture_parses() -> None:
    parsed = parse_csv(FIXTURE.read_bytes(), max_rows=100)
    assert len(parsed.rows) == 15
    assert parsed.ignored_columns == ["is_fraud"]
    shape_errors = [r.line_number for r in parsed.rows if r.shape_error]
    assert shape_errors == [15]
