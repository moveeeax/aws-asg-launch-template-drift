import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

#: Every fixture timestamp is relative to this instant so ages never drift with the clock.
NOW = datetime(2026, 11, 1, 11, 0, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def prod() -> dict:
    return load("prod.json")


@pytest.fixture
def aged() -> dict:
    return load("aged.json")


@pytest.fixture
def majority() -> dict:
    return load("majority.json")
