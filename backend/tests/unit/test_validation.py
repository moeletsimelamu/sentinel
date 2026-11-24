"""One test (or parametrised case) per validation rule: hit, miss and boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.core.vocabulary import Channel, MerchantCategory, TxnStatus, TxnType
from sentinel.modules.ingestion.validation import (
    REQUIRED_FIELDS,
    ValidationResult,
    content_hash,
    is_forbidden_field,
    is_valid_source_system,
    looks_like_card_number,
    redact_raw_row,
    validate_record,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _hash(value: str) -> str:
    return f"h({value})"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "external_id": "TX-1",
        "customer_ref": "C-1",
        "occurred_at": "2026-03-01T09:15:00Z",
        "amount": "125.50",
        "currency": "ZAR",
        "txn_type": "purchase",
        "channel": "card_present",
        "merchant_category": "groceries",
        "status": "completed",
        "city": "Johannesburg",
        "country": "ZA",
        "lat": "-26.2041",
        "lon": "28.0473",
        "device_id": "dev-a1",
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not ...}


def _check(**overrides: Any) -> ValidationResult:
    return validate_record(_row(**overrides), now=NOW, hash_device=_hash)


def _codes(result: ValidationResult) -> set[tuple[str | None, str]]:
    return {(e.field, e.code) for e in result.errors}


def test_valid_record_is_normalised() -> None:
    result = _check(currency="zar", txn_type="PURCHASE", occurred_at="2026-03-01T11:15:00+02:00")
    assert result.ok and not result.errors
    rec = result.record
    assert rec is not None
    assert rec.currency == "ZAR"
    assert rec.txn_type == "purchase"
    assert rec.occurred_at == datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
    assert rec.amount == Decimal("125.50")
    assert rec.device_id_hash == "h(dev-a1)"
    assert rec.lat == pytest.approx(-26.2041)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
@pytest.mark.parametrize("missing", [..., None, "", "   "])
def test_required_fields(field: str, missing: Any) -> None:
    result = _check(**{field: missing})
    assert (field, "required") in _codes(result)
    assert result.record is None


def test_optional_fields_may_be_absent() -> None:
    result = _check(city=..., country=..., lat=..., lon=..., device_id=...)
    assert result.ok
    assert result.record is not None and result.record.device_id_hash is None


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("abc", "invalid_number"),
        ("NaN", "invalid_number"),
        ("Infinity", "invalid_number"),
        ("0", "not_positive"),
        ("0.00", "not_positive"),
        ("-15.00", "not_positive"),
        ("10000000.01", "too_large"),
        ("1.234", "too_many_decimals"),
        (True, "invalid_type"),
    ],
)
def test_amount_rejections(amount: Any, code: str) -> None:
    assert ("amount", code) in _codes(_check(amount=amount))


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0.01", Decimal("0.01")),
        ("10000000.00", Decimal("10000000.00")),
        ("42", Decimal("42.00")),
        (10.1, Decimal("10.10")),
        (7, Decimal("7.00")),
        ("1.50", Decimal("1.50")),
    ],
)
def test_amount_boundaries_and_types(amount: Any, expected: Decimal) -> None:
    result = _check(amount=amount)
    assert result.record is not None, result.errors
    assert result.record.amount == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("yesterday", "invalid_datetime"),
        ("2026-03-01 09:15:00", "missing_timezone"),
        ("1999-12-31T23:59:59Z", "too_old"),
        ("2026-11-24T12:06:00Z", "in_future"),
    ],
)
def test_timestamp_rejections(value: str, code: str) -> None:
    assert ("occurred_at", code) in _codes(_check(occurred_at=value))


def test_timestamp_allows_small_clock_skew() -> None:
    skewed = (NOW + timedelta(minutes=4)).isoformat()
    assert _check(occurred_at=skewed).ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "XYZ"),
        ("txn_type", "teleport"),
        ("channel", "carrier_pigeon"),
        ("merchant_category", "casino_royale"),
        ("status", "maybe"),
    ],
)
def test_choice_fields_reject_unknown_values(field: str, value: str) -> None:
    result = _check(**{field: value})
    assert (field, "invalid_choice") in _codes(result)


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("txn_type", list(TxnType)),
        ("channel", list(Channel)),
        ("merchant_category", list(MerchantCategory)),
        ("status", list(TxnStatus)),
    ],
)
def test_choice_fields_accept_every_vocabulary_value(field: str, values: list[str]) -> None:
    for value in values:
        assert _check(**{field: value}).ok, value


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"lat": "95"}, ("lat", "out_of_range")),
        ({"lat": "-90.0001"}, ("lat", "out_of_range")),
        ({"lon": "180.5"}, ("lon", "out_of_range")),
        ({"lat": "north"}, ("lat", "invalid_number")),
        ({"lat": "nan"}, ("lat", "out_of_range")),
        ({"lon": ...}, ("lon", "incomplete_coordinates")),
        ({"lat": ""}, ("lat", "incomplete_coordinates")),
        ({"country": "ZAF"}, ("country", "invalid_format")),
        ({"country": "Z1"}, ("country", "invalid_format")),
    ],
)
def test_location_rules(overrides: dict[str, Any], error: tuple[str, str]) -> None:
    assert error in _codes(_check(**overrides))


def test_location_boundaries_accepted() -> None:
    assert _check(lat="90", lon="-180").ok
    result = _check(country="za")
    assert result.record is not None and result.record.country == "ZA"


@pytest.mark.parametrize("value", ["-starts-with-dash", "has space", "x" * 65, "semi;colon"])
def test_identifier_format(value: str) -> None:
    codes = _codes(_check(external_id=value))
    assert ("external_id", "invalid_format") in codes or ("external_id", "too_long") in codes


def test_control_characters_rejected() -> None:
    assert ("city", "invalid_characters") in _codes(_check(city="Jo\x07burg"))


def test_overlong_optional_text_rejected() -> None:
    assert ("city", "too_long") in _codes(_check(city="x" * 65))


def test_non_scalar_values_rejected() -> None:
    assert ("city", "invalid_type") in _codes(_check(city={"nested": "x"}))


def test_errors_are_collected_for_every_field() -> None:
    result = _check(amount="-1", currency="XYZ", status="maybe")
    assert {f for f, _ in _codes(result)} == {"amount", "currency", "status"}


# ------------------------------------------------------------- sensitive data


@pytest.mark.parametrize(
    "value",
    ["4111111111111111", "4111 1111 1111 1111", "5500-0000-0000-0004", "378282246310005"],
)
def test_card_numbers_detected(value: str) -> None:
    assert looks_like_card_number(value)


@pytest.mark.parametrize(
    "value",
    [
        "4111111111111112",  # fails Luhn
        "1234567812345670",  # passes Luhn but no card prefix
        "411111111111",  # too short
        "TX-4111111111111111",  # not purely digits
        "Johannesburg",
    ],
)
def test_non_card_values_not_flagged(value: str) -> None:
    assert not looks_like_card_number(value)


def test_card_number_in_text_field_is_rejected_and_redacted() -> None:
    raw = _row(city="4111 1111 1111 1111")
    result = validate_record(raw, now=NOW, hash_device=_hash)
    assert ("city", "sensitive_data_detected") in _codes(result)
    stored = redact_raw_row(raw, result.sensitive_fields)
    assert stored["city"] == "[REDACTED]"
    assert "4111" not in str(stored)


def test_device_id_may_be_luhn_valid_imei() -> None:
    # IMEIs use the Luhn check digit; device ids are hashed, never stored raw.
    assert _check(device_id="490154203237518").ok


def test_redacted_row_drops_unknown_columns_and_masks_device() -> None:
    raw = {**_row(), "is_fraud": "1", "notes": "x"}
    stored = redact_raw_row(raw, frozenset())
    assert "is_fraud" not in stored and "notes" not in stored
    assert stored["device_id"] == "[REDACTED]"
    assert stored["amount"] == "125.50"


@pytest.mark.parametrize(
    "name",
    [
        "card_number",
        "Card Number",
        "PAN",
        "cvv",
        "iban",
        "account_number",
        "customer_name",
        "first_name",
        "date_of_birth",
        "email",
        "Phone Number",
        "home_address",
        "gender",
        "ethnicity",
        "nationality",
        "password",
    ],
)
def test_forbidden_field_names(name: str) -> None:
    assert is_forbidden_field(name)


@pytest.mark.parametrize(
    "name", ["external_id", "customer_ref", "city", "merchant_category", "is_fraud", "device_id"]
)
def test_allowed_field_names(name: str) -> None:
    assert not is_forbidden_field(name)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("sentinel_generator", True), ("core-banking", True), ("X", False), ("Bad Name", False)],
)
def test_source_system_format(value: str, ok: bool) -> None:
    assert is_valid_source_system(value) is ok


# ---------------------------------------------------------------- content hash


def _rec_hash(**overrides: Any) -> str:
    result = _check(**overrides)
    assert result.record is not None
    return result.record.content_hash


def test_content_hash_ignores_external_id_and_formatting() -> None:
    base = _rec_hash()
    assert _rec_hash(external_id="TX-OTHER") == base
    assert _rec_hash(amount="125.5", currency="zar") == base
    assert _rec_hash(occurred_at="2026-03-01T11:15:00+02:00") == base


@pytest.mark.parametrize(
    "overrides",
    [{"amount": "125.51"}, {"status": "declined"}, {"customer_ref": "C-2"}, {"device_id": "x"}],
)
def test_content_hash_changes_with_content(overrides: dict[str, Any]) -> None:
    assert _rec_hash(**overrides) != _rec_hash()


def test_content_hash_is_stable() -> None:
    value = content_hash(
        customer_ref="C-1",
        occurred_at=datetime(2026, 3, 1, 9, 15, tzinfo=UTC),
        amount=Decimal("1.00"),
        currency="ZAR",
        txn_type="purchase",
        channel="online",
        merchant_category="groceries",
        status="completed",
        city=None,
        country=None,
        device_id_hash=None,
    )
    assert len(value) == 64 and value == content_hash(
        customer_ref="C-1",
        occurred_at=datetime(2026, 3, 1, 11, 15, tzinfo=UTC) - timedelta(hours=2),
        amount=Decimal("1.0"),
        currency="ZAR",
        txn_type="purchase",
        channel="online",
        merchant_category="groceries",
        status="completed",
        city=None,
        country=None,
        device_id_hash=None,
    )
