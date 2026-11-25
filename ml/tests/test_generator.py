"""Generator: determinism, realism bounds, privacy, and ingestion compatibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas.testing as pdt
import pytest
from sentinel.modules.ingestion.csv_reader import parse_csv
from sentinel.modules.ingestion.validation import validate_record

from sentinel_ml.datasets.build import BuiltDataset, build_dataset
from sentinel_ml.generate.generator import LABEL_COLUMNS, GeneratedDataset, generate
from sentinel_ml.generate.profiles import load_profile
from tests.conftest import SEED

PERSONAL = {"name", "first_name", "last_name", "dob", "date_of_birth", "gender", "ethnicity",
            "nationality", "email", "phone", "address", "id_number"}  # fmt: skip


def test_same_seed_same_data(tiny: GeneratedDataset) -> None:
    again = generate(load_profile("tiny"), SEED)
    for name in ("transactions", "labels", "customers", "episodes"):
        pdt.assert_frame_equal(getattr(tiny, name), getattr(again, name))


def test_same_seed_same_files(built: BuiltDataset, tmp_path: Path) -> None:
    rebuilt = build_dataset(generate(load_profile("tiny"), SEED), tmp_path)
    assert rebuilt.card["files_sha256"] == built.card["files_sha256"]
    assert (rebuilt.path / "dataset_card.md").read_bytes() == (
        built.path / "dataset_card.md"
    ).read_bytes()


def test_different_seed_different_data(tiny: GeneratedDataset) -> None:
    other = generate(load_profile("tiny"), SEED + 1)
    assert not tiny.transactions["amount"].equals(other.transactions["amount"])


def test_version_string(tiny: GeneratedDataset) -> None:
    assert tiny.version == f"tiny-1.0.0-{SEED}"


def test_fraud_share_and_typologies(tiny: GeneratedDataset) -> None:
    lab = tiny.labels
    assert 0.003 < lab["is_fraud_injected"].mean() < 0.04
    assert set(lab.loc[lab["is_fraud_injected"], "typology"]) == set(tiny.profile.typology_weights)
    assert set(lab["benign_scenario"].dropna()) >= {"travel", "payday_splurge"}


def test_demo_profile_meets_design_targets() -> None:
    profile = load_profile("demo")
    assert profile.n_customers == 2000 and profile.days == 90


def test_label_noise_flips_whole_episodes(tiny: GeneratedDataset) -> None:
    noisy = generate(load_profile("tiny", label_noise=0.5), SEED)
    lab = noisy.labels[noisy.labels["is_fraud_injected"]]
    per_episode = lab.groupby("scenario_id")["is_fraud_labelled"].nunique()
    assert (per_episode == 1).all()  # never partially labelled
    assert 0 < (~lab["is_fraud_labelled"]).sum() < len(lab)
    assert not noisy.labels.loc[~noisy.labels["is_fraud_injected"], "is_fraud_labelled"].any()


def test_transactions_contain_no_labels_or_personal_data(tiny: GeneratedDataset) -> None:
    cols = set(tiny.transactions.columns)
    assert not cols & (set(LABEL_COLUMNS) - {"external_id"})
    assert not cols & PERSONAL
    assert not set(tiny.customers.columns) & PERSONAL
    assert "segment" not in cols


def test_basic_integrity(tiny: GeneratedDataset) -> None:
    t = tiny.transactions
    assert t["external_id"].is_unique
    assert t["occurred_at"].is_monotonic_increasing
    start = datetime.combine(tiny.profile.start, datetime.min.time(), tzinfo=UTC)
    assert t["occurred_at"].min() >= start
    assert t["occurred_at"].max() < start + timedelta(days=tiny.profile.days)
    assert (t["amount"] > 0).all()
    assert t["lat"].isna().equals(t["lon"].isna())
    card_present = t["channel"] == "card_present"
    assert t.loc[card_present, "device_id"].isna().all()


def test_every_generated_row_passes_ingestion_validation(built: BuiltDataset) -> None:
    content = (built.path / "ingest" / "transactions_part_001.csv").read_bytes()
    parsed = parse_csv(content, max_rows=1_000_000)
    now = datetime.now(UTC)
    rejected: dict[str, set[str]] = {}
    for row in parsed.rows:
        result = validate_record(row.values, now=now, hash_device=lambda d: d)
        ext = row.values.get("external_id", "")
        if row.shape_error or not result.ok:
            rejected[ext] = {e.code for e in result.errors} or {"malformed_row"}
    # Only the deliberately malformed demo rows are rejected.
    assert set(rejected) == {f"BAD-{i:04d}" for i in range(1, 10)}
    assert rejected["BAD-0001"] == {"not_positive"}
    assert rejected["BAD-0008"] == {"sensitive_data_detected"}
    assert parsed.ignored_columns == []


@pytest.mark.parametrize("name", ["tiny", "demo", "full"])
def test_profiles_load(name: str) -> None:
    p = load_profile(name)
    assert abs(sum(p.typology_weights.values()) - 1) < 1e-9


def test_unknown_profile() -> None:
    with pytest.raises(ValueError, match="Unknown profile"):
        load_profile("_common")
