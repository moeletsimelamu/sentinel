"""``sentinel-ml`` command line.

sentinel-ml generate --profile demo --seed 42 --out ../data
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sentinel_ml.datasets.build import build_dataset
from sentinel_ml.generate.generator import generate
from sentinel_ml.generate.profiles import load_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-ml")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="Generate a synthetic dataset")
    gen.add_argument("--profile", default="demo", help="tiny | demo | full")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--out", type=Path, default=Path("../data"))
    gen.add_argument("--no-features", action="store_true", help="Skip feature extraction")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    profile = load_profile(args.profile)
    ds = generate(profile, args.seed)
    built = build_dataset(ds, args.out, with_features=not args.no_features)
    c = built.card["counts"]
    sys.stdout.write(
        f"Dataset {ds.version}: {c['transactions']:,} transactions, {c['customers']:,} customers, "
        f"{c['fraud_injected_pct']}% injected fraud -> {built.path} "
        f"({time.perf_counter() - started:.1f}s)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
