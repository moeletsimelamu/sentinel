"""Pure feature engineering, shared by the scoring worker and ML training.

No database, web framework or domain-module imports (enforced by import-linter).
"""

from sentinel.features.behaviour import (
    FEATURE_SET_VERSION,
    FeatureRow,
    TxnRecord,
    build_feature_rows,
)
from sentinel.features.model_input import (
    FEATURE_SCHEMA_HASH,
    FORBIDDEN_INPUTS,
    MODEL_FEATURES,
    FeatureReference,
    to_model_vector,
)

__all__ = [
    "FEATURE_SCHEMA_HASH",
    "FEATURE_SET_VERSION",
    "FORBIDDEN_INPUTS",
    "MODEL_FEATURES",
    "FeatureReference",
    "FeatureRow",
    "TxnRecord",
    "build_feature_rows",
    "to_model_vector",
]
