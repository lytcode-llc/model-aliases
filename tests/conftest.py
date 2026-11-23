"""Shared pytest scaffolding.

Loads the repo root on sys.path so tests can ``import model_aliases.<mod>``
directly as a top-level package (the same modules Hermes loads via its plugin
loader, minus the root ``__init__.py`` registration side effect).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def models_dev() -> dict:
    """Small synthetic models.dev-shaped catalog for tests."""
    return json.loads((FIXTURES / "models_dev.json").read_text())
