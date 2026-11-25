"""Builds and executes ``01_eda_outliers.ipynb`` against a generated dataset.

    python notebooks/make_eda_notebook.py ../data/demo-1.0.0-42

Kept as a script so the notebook's source is reviewable as plain Python; the executed
notebook (with outputs) is committed for reading on GitHub.
"""

from __future__ import annotations

import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

HERE = Path(__file__).resolve().parent

CELLS: list[tuple[str, str]] = [
    (
        "md",
        """# Synthetic data: exploration and outlier analysis

Sentinel generates its own data (see `docs/DATA_PIPELINE.md`). Because we designed both the
fraud patterns and the detectors, the main job of this notebook is to check that the data is
**not too easy**: that fraud overlaps legitimate behaviour, that benign anomalies create
false-positive pressure, and that no single feature gives the answer away.

> Synthetic data only. Nothing here estimates real-world detection performance.""",
    ),
    (
        "code",
        """from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sentinel_ml.datasets import eda

DATASET = Path(DATASET_PATH)
data = eda.load(DATASET)
t, lab, splits = data.transactions, data.labels, data.splits
df = t.merge(lab, on="external_id").merge(splits, on="external_id")
print(f"{DATASET.name}: {len(t):,} transactions, {t.customer_ref.nunique():,} customers")
print(f"injected fraud {lab.is_fraud_injected.mean():.2%}, labelled {lab.is_fraud_labelled.mean():.2%}")""",
    ),
    ("md", "## Volume and mix"),
    (
        "code",
        """summary = pd.DataFrame({
    "transactions": df.groupby("split").size(),
    "fraud_injected": df.groupby("split").is_fraud_injected.sum(),
})
summary["fraud_pct"] = (100 * summary.fraud_injected / summary.transactions).round(2)
display(summary)
display(df[df.is_fraud_injected].groupby("typology").agg(
    transactions=("external_id", "size"), episodes=("scenario_id", "nunique")))
display(df.benign_scenario.value_counts().rename("benign transactions"))""",
    ),
    (
        "code",
        """fig, axes = plt.subplots(1, 2, figsize=(12, 3.5))
daily = df.set_index("occurred_at").resample("D").size()
daily.plot(ax=axes[0], color="#2f4b7c", lw=1)
axes[0].set_title("Transactions per day (weekend and payday cycles)")
per_cust = df.groupby("customer_ref").size()
axes[1].hist(per_cust, bins=50, color="#2f4b7c")
axes[1].set_title("Transactions per customer (sparse histories exist)")
axes[1].set_xlabel("transactions")
plt.tight_layout()""",
    ),
    (
        "md",
        """## Amounts by class

Fraud amounts should overlap legitimate ones — injected episodes draw amounts relative to
each customer's own scale, and `low_and_slow` uses ordinary amounts on purpose.""",
    ),
    (
        "code",
        """from sentinel.core.vocabulary import FX_TO_ZAR
df["amount_zar"] = df.amount * df.currency.map(FX_TO_ZAR)
fig, ax = plt.subplots(figsize=(12, 3.5))
bins = np.logspace(0, 6, 60)
ax.hist(df.loc[~df.is_fraud_injected, "amount_zar"], bins=bins, density=True, alpha=0.6,
        label="legitimate", color="#2f4b7c")
ax.hist(df.loc[df.is_fraud_injected, "amount_zar"], bins=bins, density=True, alpha=0.6,
        label="injected fraud", color="#d45087")
ax.set_xscale("log"); ax.set_xlabel("amount (ZAR, log scale)"); ax.legend()
ax.set_title("Amount distribution by class (density)")
display(df.groupby("typology").amount_zar.describe(percentiles=[0.5, 0.9]).round(0))""",
    ),
    (
        "md",
        """## Outliers: signal or artefact?

Tukey fences (3x IQR on log amount) within each merchant category. The question for each
outlier group is whether it is *signal* (fraud we want to find), *benign* (an anomaly we
generated on purpose, which should not be flagged), or an *artefact* of the generator.""",
    ),
    (
        "code",
        """df["amount_outlier"] = eda.amount_outliers(df).to_numpy()
df["kind"] = np.select(
    [df.is_fraud_injected, df.benign_scenario.notna()],
    ["fraud: " + df.typology.fillna(""), "benign: " + df.benign_scenario.fillna("")],
    default="ordinary")
out = df[df.amount_outlier]
print(f"{len(out):,} amount outliers ({len(out)/len(df):.2%} of transactions)")
display(out.kind.value_counts().rename("outliers").to_frame()
        .assign(share=lambda d: (d.outliers / d.outliers.sum()).round(3)))""",
    ),
    (
        "md",
        """Reading the table: most amount outliers are **not** fraud — benign travel spending
dominates, so a population-level amount rule would mostly flag legitimate activity. The
fraud outliers sit in the *low* tail: card-testing probes of a few rand. High-value fraud
(account takeover, night drain, mule pass-through) stays inside the category fences because
its amounts are drawn relative to each customer's own scale — it is unusual *for that
customer*, not for the population. That is why the features are customer-relative
(`amount_vs_cust_median_90d`) rather than global thresholds.

Outliers are therefore **kept** (they are realistic and informative); heavy-tailed features
are log-transformed, and models that need scaling do it inside the training pipeline
(Phase 6).""",
    ),
    (
        "md",
        """## Single-feature leakage check

Each model input on the training split, scored on its own. A feature with separability
≥ 0.95 would suggest the generator left a fingerprint.""",
    ),
    (
        "code",
        """X, y = eda.training_matrix(data)
sep = eda.single_feature_auc(X, y)
display(sep.head(12).round(3))
print("suspicious features:", sep.loc[sep.suspicious, "feature"].tolist() or "none")""",
    ),
    (
        "code",
        """fig, ax = plt.subplots(figsize=(12, 4))
top = sep.head(15)[::-1]
ax.barh(top.feature, top.separability, color="#2f4b7c")
ax.axvline(0.5, color="grey", lw=1, ls=":")
ax.axvline(eda.SUSPICIOUS_AUC, color="#d45087", lw=1, ls="--", label="suspicious")
ax.set_xlim(0.45, 1.0); ax.set_xlabel("single-feature separability (ROC AUC)"); ax.legend()
ax.set_title("No single feature separates the classes")
plt.tight_layout()""",
    ),
    (
        "md",
        """## Per-typology difficulty preview

How often does each typology show the behavioural signal its rule looks for? `low_and_slow`
is expected to look ordinary on every feature — the intended false negatives.""",
    ),
    (
        "code",
        """f = data.features.merge(lab, on="external_id")
checks = {
    "new device": f.is_new_device_90d == True,  # noqa: E712
    "new city": f.is_new_city_90d == True,  # noqa: E712
    "amount >= 3x median": f.amount_vs_cust_median_90d >= 3,
    "speed >= 900 km/h": f.implied_speed_kmh >= 900,
    "failed in 24h >= 3": f.failed_count_24h >= 3,
    "rare hour (>= 0.97)": f.hour_rarity_cust >= 0.97,
}
rates = pd.DataFrame({name: m.groupby(f.typology.fillna("legitimate")).mean()
                      for name, m in checks.items()})
display((100 * rates).round(1))""",
    ),
    (
        "md",
        """## Conclusions

- Fraud is ~0.7% of transactions, spread over seven typologies, with a few episodes left
  unlabelled on purpose.
- Fraud and legitimate amounts overlap heavily; most amount outliers are benign travel, and
  the fraud outliers are tiny card-testing probes.
- No single model input is near-perfectly separating (best ≈ 0.77 AUC on the demo
  dataset), so models must combine signals.
- Episode signals are partial by construction: only the first transaction of a takeover is
  on a "new" device, and only the first jump of an impossible-travel episode has an
  implausible speed.
- `low_and_slow` shows almost none of the behavioural signals — expect it to be missed.
- About 16% of legitimate transactions are ≥ 3x the customer's median, so relative-amount
  rules carry real false-positive load.

These properties are re-checked automatically by the tests in `ml/tests/`.""",
    ),
]


def build(dataset: Path, target: Path) -> None:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "name": "python3",
        "display_name": "Python 3",
        "language": "python",
    }
    nb.cells.append(
        nbformat.v4.new_code_cell(
            f"# Dataset folder (relative to ml/notebooks)\nDATASET_PATH = {dataset.as_posix()!r}"
        )
    )
    for kind, source in CELLS:
        cell = nbformat.v4.new_markdown_cell if kind == "md" else nbformat.v4.new_code_cell
        nb.cells.append(cell(source))
    NotebookClient(
        nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(HERE)}}
    ).execute()
    for cell in nb.cells:  # strip volatile execution metadata so re-runs diff cleanly
        cell.metadata.pop("execution", None)
    nbformat.write(nb, target)


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../../data/demo-1.0.0-42")
    build(folder, HERE / "01_eda_outliers.ipynb")
