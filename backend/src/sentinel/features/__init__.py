"""Pure feature engineering, shared by the scoring worker and (from Phase 5) ML training.

No database, web framework or domain-module imports (enforced by import-linter).
"""

from sentinel.features.behaviour import (
    FEATURE_SET_VERSION,
    FeatureRow,
    TxnRecord,
    build_feature_rows,
)

__all__ = ["FEATURE_SET_VERSION", "FeatureRow", "TxnRecord", "build_feature_rows"]
