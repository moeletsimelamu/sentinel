"""Generator profiles: ``_common.yaml`` merged with ``<name>.yaml``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"


@dataclass(frozen=True)
class Profile:
    name: str
    n_customers: int
    days: int
    start: date
    fraud_episodes_per_customer_month: float
    label_noise: float
    holdout_customer_share: float
    split: dict[str, float]
    benign: dict[str, float]
    typology_weights: dict[str, float]
    malformed_rows: bool

    def config_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_profile(name: str, **overrides: Any) -> Profile:
    path = PROFILE_DIR / f"{name}.yaml"
    if name.startswith("_") or not path.is_file():
        available = sorted(p.stem for p in PROFILE_DIR.glob("*.yaml") if not p.stem.startswith("_"))
        raise ValueError(f"Unknown profile {name!r}; choose one of {available}")
    common = yaml.safe_load((PROFILE_DIR / "_common.yaml").read_text(encoding="utf-8"))
    data = _merge(common, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    data = _merge(data, overrides)
    data["start"] = date.fromisoformat(str(data["start"]))
    weights = data["typology_weights"]
    if abs(sum(weights.values()) - 1) > 1e-6:
        raise ValueError("typology_weights must sum to 1")
    return Profile(name=name, **data)
