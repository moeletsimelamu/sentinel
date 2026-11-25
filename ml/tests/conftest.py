from __future__ import annotations

from pathlib import Path

import pytest

from sentinel_ml.datasets.build import BuiltDataset, build_dataset
from sentinel_ml.generate.generator import GeneratedDataset, generate
from sentinel_ml.generate.profiles import load_profile

SEED = 7


@pytest.fixture(scope="session")
def tiny() -> GeneratedDataset:
    return generate(load_profile("tiny"), SEED)


@pytest.fixture(scope="session")
def built(tiny: GeneratedDataset, tmp_path_factory: pytest.TempPathFactory) -> BuiltDataset:
    return build_dataset(tiny, tmp_path_factory.mktemp("data"))


def folder(built: BuiltDataset) -> Path:
    return built.path
