"""Model inference: verified loading, scoring and per-transaction explanations.

Pure with respect to the application: no database, web framework or domain modules
(enforced by import-linter). The scoring module passes in the registered artefact hash.
"""

from sentinel.inference.loader import (
    Contribution,
    LoadedModel,
    ModelLoadError,
    load_model,
)

__all__ = ["Contribution", "LoadedModel", "ModelLoadError", "load_model"]
