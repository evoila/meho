# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/helm_golden.py`` (no ``helm`` binary needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "helm_golden.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("helm_golden", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


golden = _load()


def test_mask_random_hides_the_generated_values_only() -> None:
    text = (
        "data:\n"
        '  requirepass: "made-up-one"\n'
        '  token: "made-up-two"\n'
        '  other: "kept"\n'
        "annotations:\n"
        "  checksum/metrics-auth: " + "ab" * 32 + "\n"
        "  checksum/config: 1234   \n"
    )
    masked = golden.mask_random(text)
    assert 'requirepass: "<random>"' in masked
    assert 'token: "<random>"' in masked
    assert "checksum/metrics-auth: <random>" in masked
    assert 'other: "kept"' in masked
    assert "checksum/config: 1234" in masked
    assert golden.mask_random(masked) == masked


def test_every_scenario_has_a_committed_golden_file() -> None:
    for name in golden.SCENARIOS:
        assert (_REPO_ROOT / golden.GOLDEN_DIR / f"{name}.yaml").is_file(), name
