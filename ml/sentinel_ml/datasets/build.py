"""Write a generated dataset to ``<out>/<dataset_version>/``.

Files:
  transactions.parquet   ingestible columns, no labels
  labels.parquet         ground truth — evaluation only, never ingested
  customers.parquet      generator metadata (segment, …) — never ingested
  episodes.parquet       one row per injected fraud episode
  splits.parquet         external_id → train / validation / test / holdout / excluded
  features.parquet       behavioural features (same code as the scoring worker)
  feature_reference.json population statistics fitted on the training split
  ingest/transactions_part_NNN.csv   CSVs for POST /api/v1/imports (labels excluded)
  dataset_card.md / dataset_card.json
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sentinel.features import FEATURE_SCHEMA_HASH, FEATURE_SET_VERSION

from sentinel_ml import GENERATOR_VERSION
from sentinel_ml.datasets.features import build_feature_table, fit_reference
from sentinel_ml.datasets.split import SPLITS, assign_splits
from sentinel_ml.generate.generator import GeneratedDataset

CSV_ROWS_PER_PART = 100_000  # below the API's 250k-row import limit
DISCLAIMER = (
    "Synthetic data generated for an educational simulation. It contains no real people, "
    "accounts, cards or transactions. Results obtained on it demonstrate methodology only "
    "and do not estimate real-world fraud detection performance."
)

# Deliberately invalid rows appended to the first ingestion CSV so validation reporting
# can be demonstrated. Each exercises a different rule.
MALFORMED_ROWS = [
    "BAD-0001,C-00001,2026-01-10T10:00:00Z,-50.00,ZAR,purchase,online,groceries,completed,,,,,",
    "BAD-0002,C-00001,2026-01-10T10:05:00Z,50.00,XYZ,purchase,online,groceries,completed,,,,,",
    "BAD-0003,C-00002,,75.00,ZAR,purchase,card_present,fuel,completed,Durban,ZA,,,",
    "BAD-0004,C-00002,2026-01-11 09:00:00,75.00,ZAR,purchase,card_present,fuel,completed,,,,,",
    "BAD-0005,C-00003,2026-01-12T08:00:00Z,20.00,ZAR,teleport,online,groceries,completed,,,,,",
    "BAD-0006,C-00003,2026-01-12T08:10:00Z,20.00,ZAR,purchase,online,groceries,completed,,,95.0,28.0,",
    "BAD-0007,C-00004,2099-01-01T00:00:00Z,20.00,ZAR,purchase,online,groceries,completed,,,,,",
    "BAD-0008,C-00004,2026-01-13T08:00:00Z,20.00,ZAR,purchase,online,groceries,completed,"
    "4111 1111 1111 1111,ZA,,,",
    "BAD-0009,C-00005,2026-01-14T08:00:00Z,20.00,ZAR",
    "T00000001,C-00005,2026-01-14T09:00:00Z,20.00,ZAR,purchase,online,groceries,completed,,,,,",
]


@dataclass(frozen=True)
class BuiltDataset:
    path: Path
    card: dict[str, Any]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _csv_frame(transactions: pd.DataFrame) -> pd.DataFrame:
    out = transactions.copy()
    out["occurred_at"] = out["occurred_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["amount"] = out["amount"].map(lambda a: f"{a:.2f}")
    return out


def write_ingest_csvs(ds: GeneratedDataset, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    frame = _csv_frame(ds.transactions)
    paths = []
    for part, start in enumerate(range(0, len(frame), CSV_ROWS_PER_PART), start=1):
        path = folder / f"transactions_part_{part:03d}.csv"
        text = frame.iloc[start : start + CSV_ROWS_PER_PART].to_csv(
            index=False, lineterminator="\n"
        )
        if part == 1 and ds.profile.malformed_rows:
            text += "\n".join(MALFORMED_ROWS) + "\n"
        path.write_text(text, encoding="utf-8", newline="")
        paths.append(path)
    return paths


def _rate(n: int, d: int) -> float:
    return round(100 * n / d, 3) if d else 0.0


def build_card(
    ds: GeneratedDataset,
    splits: pd.Series,
    boundaries: dict[str, str],
    files: dict[str, str],
) -> dict[str, Any]:
    t, lab = ds.transactions, ds.labels
    fraud = lab[lab["is_fraud_injected"]]
    per_split = {}
    for s in SPLITS:
        mask = splits == s
        per_split[s] = {
            "transactions": int(mask.sum()),
            "fraud_injected": int(lab.loc[mask, "is_fraud_injected"].sum()),
            "fraud_labelled": int(lab.loc[mask, "is_fraud_labelled"].sum()),
        }
    return {
        "dataset_version": ds.version,
        "generator_version": GENERATOR_VERSION,
        "profile": ds.profile.name,
        "profile_config_hash": ds.profile.config_hash(),
        "seed": ds.seed,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "period": {
            "start": t["occurred_at"].min().isoformat(),
            "end": t["occurred_at"].max().isoformat(),
            "days": ds.profile.days,
        },
        "counts": {
            "customers": int(ds.customers.shape[0]),
            "transactions": int(t.shape[0]),
            "episodes": int(ds.episodes.shape[0]),
            "unlabelled_episodes": int((~ds.episodes["labelled"]).sum()),
            "fraud_injected": int(lab["is_fraud_injected"].sum()),
            "fraud_labelled": int(lab["is_fraud_labelled"].sum()),
            "fraud_injected_pct": _rate(int(lab["is_fraud_injected"].sum()), len(lab)),
        },
        "typologies": {
            typ: {
                "episodes": int((ds.episodes["typology"] == typ).sum()),
                "transactions": int((fraud["typology"] == typ).sum()),
            }
            for typ in sorted(ds.profile.typology_weights)
        },
        "benign_scenarios": {
            k: int(v) for k, v in lab["benign_scenario"].value_counts().sort_index().items()
        },
        "segments": {
            k: int(v) for k, v in ds.customers["segment"].value_counts().sort_index().items()
        },
        "channels": {k: int(v) for k, v in t["channel"].value_counts().sort_index().items()},
        "statuses": {k: int(v) for k, v in t["status"].value_counts().sort_index().items()},
        "split_boundaries": boundaries,
        "splits": per_split,
        "files_sha256": files,
        "disclaimer": DISCLAIMER,
    }


def render_card(card: dict[str, Any]) -> str:
    c = card["counts"]
    lines = [
        f"# Dataset card — `{card['dataset_version']}`",
        "",
        f"> {card['disclaimer']}",
        "",
        "| Property | Value |",
        "|---|---|",
        f"| Generator version | {card['generator_version']} |",
        f"| Profile | `{card['profile']}` (config hash `{card['profile_config_hash']}`) |",
        f"| Seed | {card['seed']} |",
        f"| Feature set | `{card['feature_set_version']}` "
        f"(schema `{card['feature_schema_hash']}`) |",
        f"| Period | {card['period']['start']} → {card['period']['end']} "
        f"({card['period']['days']} days) |",
        f"| Customers | {c['customers']:,} |",
        f"| Transactions | {c['transactions']:,} |",
        f"| Injected fraud | {c['fraud_injected']:,} transactions ({c['fraud_injected_pct']}%) "
        f"in {c['episodes']} episodes |",
        f"| Labelled fraud | {c['fraud_labelled']:,} — {c['unlabelled_episodes']} episodes left "
        "unlabelled to mimic undiscovered fraud |",
        "",
        "## Fraud typologies",
        "",
        "| Typology | Episodes | Transactions |",
        "|---|--:|--:|",
        *(
            f"| `{k}` | {v['episodes']} | {v['transactions']} |"
            for k, v in card["typologies"].items()
        ),
        "",
        "## Benign anomalies (not fraud)",
        "",
        "| Scenario | Transactions |",
        "|---|--:|",
        *(f"| `{k}` | {v} |" for k, v in card["benign_scenarios"].items()),
        "",
        "## Splits",
        "",
        "Time-based; fraud episodes stay in the split of their first transaction (any later "
        "part is `excluded`); 10% of customers form a customer-disjoint `holdout`.",
        "",
        "| Split | Transactions | Injected fraud | Labelled fraud |",
        "|---|--:|--:|--:|",
        *(
            f"| {k} | {v['transactions']:,} | {v['fraud_injected']} | {v['fraud_labelled']} |"
            for k, v in card["splits"].items()
        ),
        "",
        "Boundaries: " + ", ".join(f"{k} `{v}`" for k, v in card["split_boundaries"].items()),
        "",
        "## Files",
        "",
        "| File | SHA-256 |",
        "|---|---|",
        *(f"| `{k}` | `{v}` |" for k, v in card["files_sha256"].items()),
        "",
    ]
    return "\n".join(lines)


def build_dataset(ds: GeneratedDataset, out: Path, *, with_features: bool = True) -> BuiltDataset:
    folder = out / ds.version
    folder.mkdir(parents=True, exist_ok=True)
    splits, b, _ = assign_splits(ds.transactions, ds.labels, ds.profile, ds.seed)

    outputs: dict[str, pd.DataFrame] = {
        "transactions.parquet": ds.transactions,
        "labels.parquet": ds.labels,
        "customers.parquet": ds.customers,
        "episodes.parquet": ds.episodes,
        "splits.parquet": pd.DataFrame(
            {"external_id": ds.transactions["external_id"], "split": splits.to_numpy()}
        ),
    }
    if with_features:
        features = build_feature_table(ds.transactions)
        outputs["features.parquet"] = features
        train = features[(splits == "train").to_numpy()]
        reference = fit_reference(train)
        (folder / "feature_reference.json").write_text(
            json.dumps(reference.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for name, frame in outputs.items():
        frame.to_parquet(folder / name, index=False)
    csvs = write_ingest_csvs(ds, folder / "ingest")

    names = [*outputs, *(["feature_reference.json"] if with_features else [])]
    names += [str(p.relative_to(folder).as_posix()) for p in csvs]
    files = {n: _sha256(folder / n) for n in names}
    card = build_card(ds, splits, b.as_dict(), files)
    (folder / "dataset_card.json").write_text(
        json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (folder / "dataset_card.md").write_text(render_card(card), encoding="utf-8")
    return BuiltDataset(folder, card)
