"""Record-level validation for incoming transactions.

Pure: no database, no web framework. The same function validates CSV rows and REST
records, so both paths accept and reject exactly the same data. Every failure is
reported per field with a stable machine-readable ``code``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sentinel.core.vocabulary import CURRENCIES, Channel, MerchantCategory, TxnStatus, TxnType

REQUIRED_FIELDS: tuple[str, ...] = (
    "external_id",
    "customer_ref",
    "occurred_at",
    "amount",
    "currency",
    "txn_type",
    "channel",
    "merchant_category",
    "status",
)
OPTIONAL_FIELDS: tuple[str, ...] = ("city", "country", "lat", "lon", "device_id")
KNOWN_FIELDS: frozenset[str] = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

MAX_AMOUNT = Decimal("10000000.00")
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_RAW_VALUE_LENGTH = 256

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.:]{0,63}")
_SOURCE_SYSTEM = re.compile(r"[a-z0-9][a-z0-9_\-]{1,31}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_CARD_LIKE = re.compile(r"[\d \-]{13,23}")

# Field names that indicate card, bank-credential or personal data. Sentinel must never
# store these, so a file containing such a column is rejected outright. Protected
# attributes are listed too: they must never become model inputs.
_FORBIDDEN_EXACT = frozenset(
    {
        "pan",
        "cvv",
        "cvc",
        "cvv2",
        "iban",
        "pin",
        "ssn",
        "name",
        "email",
        "phone",
        "dob",
        "address",
        "password",
        "passport",
        "gender",
        "sex",
        "race",
        "ethnicity",
        "nationality",
        "religion",
        "marital_status",
    }
)
_FORBIDDEN_PARTS = (
    "card_number",
    "cardnumber",
    "account_number",
    "accountnumber",
    "routing_number",
    "sort_code",
    "security_code",
    "expiry",
    "date_of_birth",
    "birth_date",
    "first_name",
    "last_name",
    "full_name",
    "middle_name",
    "given_name",
    "family_name",
    "maiden_name",
    "customer_name",
    "client_name",
    "holder_name",
    "cardholder",
    "account_holder",
    "surname",
    "id_number",
    "national_id",
    "phone_number",
    "email_address",
    "street_address",
    "home_address",
    "password",
    "passport",
    "ethnicity",
    "nationality",
    "religion",
    "gender",
)


@dataclass(frozen=True)
class FieldError:
    field: str | None
    code: str
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ValidRecord:
    external_id: str
    customer_ref: str
    occurred_at: datetime
    amount: Decimal
    currency: str
    txn_type: str
    channel: str
    merchant_category: str
    status: str
    city: str | None
    country: str | None
    lat: float | None
    lon: float | None
    device_id_hash: str | None
    content_hash: str


@dataclass(frozen=True)
class ValidationResult:
    record: ValidRecord | None
    errors: tuple[FieldError, ...]
    # True when a value looked like a card number; the raw value must not be stored.
    sensitive_fields: frozenset[str] = frozenset()

    @property
    def ok(self) -> bool:
        return self.record is not None


def normalise_field_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def is_forbidden_field(name: str) -> bool:
    norm = normalise_field_name(name)
    return norm in _FORBIDDEN_EXACT or any(part in norm for part in _FORBIDDEN_PARTS)


def is_valid_source_system(value: str) -> bool:
    return _SOURCE_SYSTEM.fullmatch(value) is not None


def looks_like_card_number(value: str) -> bool:
    """13-19 digits (spaces/dashes allowed) that pass the Luhn check with a card prefix."""
    if not _CARD_LIKE.fullmatch(value):
        return False
    digits = re.sub(r"[ \-]", "", value)
    if not (13 <= len(digits) <= 19 and digits.isdigit()) or digits[0] not in "23456":
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def content_hash(
    *,
    customer_ref: str,
    occurred_at: datetime,
    amount: Decimal,
    currency: str,
    txn_type: str,
    channel: str,
    merchant_category: str,
    status: str,
    city: str | None,
    country: str | None,
    device_id_hash: str | None,
) -> str:
    """Hash of everything except the record's own id — equal hashes under different ids
    suggest the same real-world event was submitted twice."""
    parts = [
        customer_ref,
        occurred_at.astimezone(UTC).isoformat(),
        f"{amount:.2f}",
        currency,
        txn_type,
        channel,
        merchant_category,
        status,
        city or "",
        country or "",
        device_id_hash or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class _Collector:
    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw
        self.errors: list[FieldError] = []
        self.sensitive: set[str] = set()

    def fail(self, field: str | None, code: str, message: str) -> None:
        self.errors.append(FieldError(field, code, message))

    def text(self, field: str, *, required: bool, max_length: int = 64) -> str | None:
        value = self.raw.get(field)
        if value is None:
            if required:
                self.fail(field, "required", "This field is required")
            return None
        if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
            self.fail(field, "invalid_type", "Expected a text or number value")
            return None
        text = str(value).strip()
        if not text:
            if required:
                self.fail(field, "required", "This field is required")
            return None
        if len(text) > max_length:
            self.fail(field, "too_long", f"Must be at most {max_length} characters")
            return None
        if any(ord(c) < 32 for c in text):
            self.fail(field, "invalid_characters", "Control characters are not allowed")
            return None
        if field != "device_id" and looks_like_card_number(text):
            self.sensitive.add(field)
            self.fail(
                field,
                "sensitive_data_detected",
                "Value looks like a payment card number; card data must not be submitted",
            )
            return None
        return text

    def identifier(self, field: str) -> str | None:
        text = self.text(field, required=True)
        if text is not None and not _IDENTIFIER.fullmatch(text):
            self.fail(
                field,
                "invalid_format",
                "Use 1-64 letters, digits, '_', '-', '.' or ':' starting with a letter or digit",
            )
            return None
        return text

    def choice(self, field: str, allowed: frozenset[str]) -> str | None:
        text = self.text(field, required=True)
        if text is None:
            return None
        norm = text.lower()
        if norm not in allowed:
            self.fail(field, "invalid_choice", f"Must be one of: {', '.join(sorted(allowed))}")
            return None
        return norm

    def number(self, field: str, low: float, high: float) -> float | None:
        text = self.text(field, required=False, max_length=32)
        if text is None:
            return None
        try:
            value = float(text)
        except ValueError:
            self.fail(field, "invalid_number", "Must be a number")
            return None
        if not math.isfinite(value) or not low <= value <= high:
            self.fail(field, "out_of_range", f"Must be between {low:g} and {high:g}")
            return None
        return value


def _parse_amount(c: _Collector) -> Decimal | None:
    raw = c.raw.get("amount")
    # JSON numbers arrive as floats; repr() gives the shortest round-trip form (10.1, not
    # 10.0999999...). Strings are preferred and documented for money.
    text = repr(raw) if isinstance(raw, float) else c.text("amount", required=True, max_length=32)
    if text is None:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        c.fail("amount", "invalid_number", "Must be a decimal number, e.g. 125.50")
        return None
    if not amount.is_finite():
        c.fail("amount", "invalid_number", "Must be a decimal number, e.g. 125.50")
        return None
    if amount <= 0:
        c.fail("amount", "not_positive", "Must be greater than zero")
        return None
    if amount > MAX_AMOUNT:
        c.fail("amount", "too_large", f"Must not exceed {MAX_AMOUNT}")
        return None
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        c.fail("amount", "too_many_decimals", "At most 2 decimal places")
        return None
    return amount.quantize(Decimal("0.01"))


def _parse_timestamp(c: _Collector, now: datetime) -> datetime | None:
    text = c.text("occurred_at", required=True, max_length=40)
    if text is None:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        c.fail("occurred_at", "invalid_datetime", "Use ISO 8601, e.g. 2026-03-01T14:05:00Z")
        return None
    if ts.tzinfo is None:
        c.fail("occurred_at", "missing_timezone", "Include a UTC offset, e.g. Z or +02:00")
        return None
    ts = ts.astimezone(UTC)
    if ts < EARLIEST:
        c.fail("occurred_at", "too_old", "Must be on or after 2000-01-01")
        return None
    if ts > now + FUTURE_TOLERANCE:
        c.fail("occurred_at", "in_future", "Must not be in the future")
        return None
    return ts


def _given(raw: Mapping[str, Any], field: str) -> bool:
    value = raw.get(field)
    return value is not None and str(value).strip() != ""


_TXN_TYPES = frozenset(t.value for t in TxnType)
_CHANNELS = frozenset(c.value for c in Channel)
_CATEGORIES = frozenset(m.value for m in MerchantCategory)
_STATUSES = frozenset(s.value for s in TxnStatus)
_CURRENCIES_LOWER = frozenset(c.lower() for c in CURRENCIES)


def validate_record(
    raw: Mapping[str, Any], *, now: datetime, hash_device: Callable[[str], str]
) -> ValidationResult:
    """Validate one incoming record. ``raw`` keys must already be normalised field names."""
    c = _Collector(raw)
    external_id = c.identifier("external_id")
    customer_ref = c.identifier("customer_ref")
    occurred_at = _parse_timestamp(c, now)
    amount = _parse_amount(c)
    currency = c.choice("currency", _CURRENCIES_LOWER)
    txn_type = c.choice("txn_type", _TXN_TYPES)
    channel = c.choice("channel", _CHANNELS)
    category = c.choice("merchant_category", _CATEGORIES)
    status = c.choice("status", _STATUSES)
    city = c.text("city", required=False)
    country = c.text("country", required=False, max_length=8)
    if country is not None:
        country = country.upper()
        if not _COUNTRY.fullmatch(country):
            c.fail("country", "invalid_format", "Use an ISO 3166-1 alpha-2 code, e.g. ZA")
            country = None
    lat = c.number("lat", -90, 90)
    lon = c.number("lon", -180, 180)
    lat_given, lon_given = _given(raw, "lat"), _given(raw, "lon")
    if lat_given != lon_given:
        c.fail("lon" if lat_given else "lat", "incomplete_coordinates", "Give both lat and lon")
    device_id = c.text("device_id", required=False, max_length=128)

    if (
        c.errors
        or external_id is None
        or customer_ref is None
        or occurred_at is None
        or amount is None
        or currency is None
        or txn_type is None
        or channel is None
        or category is None
        or status is None
    ):
        return ValidationResult(None, tuple(c.errors), frozenset(c.sensitive))

    device_hash = hash_device(device_id) if device_id else None
    currency = currency.upper()
    record = ValidRecord(
        external_id=external_id,
        customer_ref=customer_ref,
        occurred_at=occurred_at,
        amount=amount,
        currency=currency,
        txn_type=txn_type,
        channel=channel,
        merchant_category=category,
        status=status,
        city=city,
        country=country,
        lat=lat,
        lon=lon,
        device_id_hash=device_hash,
        content_hash=content_hash(
            customer_ref=customer_ref,
            occurred_at=occurred_at,
            amount=amount,
            currency=currency,
            txn_type=txn_type,
            channel=channel,
            merchant_category=category,
            status=status,
            city=city,
            country=country,
            device_id_hash=device_hash,
        ),
    )
    return ValidationResult(record, ())


def redact_raw_row(raw: Mapping[str, Any], sensitive_fields: frozenset[str]) -> dict[str, Any]:
    """Copy of a rejected row that is safe to store and show in the rejection report."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in KNOWN_FIELDS:
            continue  # unknown columns are never stored
        if key in sensitive_fields or key == "device_id":
            out[key] = "[REDACTED]" if value not in (None, "") else value
        elif value is None or isinstance(value, bool | int | float):
            out[key] = value
        else:
            out[key] = str(value)[:MAX_RAW_VALUE_LENGTH]
    return out
