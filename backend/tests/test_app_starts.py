# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Smoke tests for the chassis FastAPI app.

These tests do not exercise any business logic — they assert that the
project skeleton imports, the FastAPI app instantiates, and the root
route returns the expected identity payload. Health / version / ready
behaviour is covered in :mod:`tests.test_health` once Task #19 lands.
"""

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from meho_backplane import __version__
from meho_backplane.main import app

# Resolve the backend project root from the test file location:
#   backend/tests/test_app_starts.py → parents[1] == backend/
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_root_returns_identity_payload() -> None:
    """``GET /`` returns 200 with the locked name + version JSON."""
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"name": "meho-backplane", "version": "0.1.0-dev"}


def test_version_constant_matches_pyproject() -> None:
    """The package ``__version__`` stays in lock-step with pyproject.

    Acts as a tripwire: bumping the version in ``pyproject.toml``
    without bumping :mod:`meho_backplane.__init__` (or vice versa)
    breaks this test, making the drift visible in CI. The check parses
    ``pyproject.toml`` directly so the invariant promised in the
    docstring is actually enforced (rather than asserted against a
    duplicated literal).
    """
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
