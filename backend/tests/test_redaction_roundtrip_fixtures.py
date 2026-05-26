# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Round-trip redaction fixture suite -- G11.4-T4 (#1073) CI gate.

This file is the **CI gate** for Initiative #805's round-trip DoD bullet
("redaction policy round-trips ... enforced in CI"). For every fixture
directory under ``backend/tests/redaction_fixtures/``, the harness:

1. Loads ``policy.yaml`` via :func:`parse_policy`.
2. Loads ``raw.json`` (the captured raw payload) and ``expected.json``
   (the expected redacted view).
3. Optionally loads ``labels.json`` (``connector_id`` / ``tenant`` /
   ``op``) and ``manifest.json`` (the expected manifest projection).
4. Calls :func:`meho_backplane.redaction.redact` and asserts equality
   **in both senses** -- the engine output must equal ``expected``
   exactly. No leak (raw secret survives) and no over-redaction
   (engine touched a value the policy did not target) are tolerated.

The double-sided contract is load-bearing per #805 DoD: under-redaction
is the safety failure (the parent goal #800 hinges on it); over-
redaction is the usability failure (operators stop trusting the system
when their summaries blank out). The fixture pair is the only way to
catch both without a human eyeballing every dispatch.

**Shadow mode.** Fixtures with ``mode: shadow`` in their policy YAML
expect ``expected.json == raw.json`` -- shadow / detection-only mode
emits the manifest but does not mutate the payload. The same equality
assertion catches a bug where shadow accidentally redacts (would
mean the mode flag is broken).

**Adding fixtures.** See ``backend/tests/redaction_fixtures/README.md``.

The harness is parametrised by fixture directory name so pytest's
output points at the failing fixture by name; a single broken fixture
does not skip the rest.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from meho_backplane.redaction import (
    RedactionManifestEntry,
    parse_policy,
    redact,
)

# Resolve the fixtures directory relative to this file so the suite
# is robust to ``pytest`` being invoked from any cwd (``backend/``,
# repo root, the worktree's own root). ``__file__`` is stable across
# editable installs because the wheel layout puts tests outside the
# import root.
FIXTURES_ROOT = pathlib.Path(__file__).parent / "redaction_fixtures"


def _discover_fixtures() -> list[str]:
    """Return the names of every fixture sub-directory.

    A fixture is a directory under :data:`FIXTURES_ROOT` containing at
    least ``policy.yaml``, ``raw.json``, and ``expected.json``. Other
    files (``README.md``, hidden dotfiles) are skipped. The list is
    sorted so the parametrised test order is deterministic.
    """
    if not FIXTURES_ROOT.exists():
        return []
    fixtures: list[str] = []
    for entry in sorted(FIXTURES_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "policy.yaml").exists():
            continue
        if not (entry / "raw.json").exists():
            continue
        if not (entry / "expected.json").exists():
            continue
        fixtures.append(entry.name)
    return fixtures


# Captured at import time -- the fixture set does not change between
# tests in one process, and parametrize() needs the list eagerly.
_FIXTURE_NAMES = _discover_fixtures()


def test_fixture_directory_is_non_empty() -> None:
    """The CI gate is only as good as the fixtures behind it.

    An empty fixture directory would pass every parametrised test
    vacuously -- the round-trip gate would then "pass" without
    actually checking anything. This guard fails first when the
    fixture set is removed or the harness's resolution breaks.
    """
    assert _FIXTURE_NAMES, (
        f"no round-trip fixtures discovered under {FIXTURES_ROOT}. "
        "Each fixture is a sub-directory with policy.yaml + raw.json + expected.json; "
        "see redaction_fixtures/README.md."
    )


@pytest.mark.parametrize("fixture_name", _FIXTURE_NAMES)
def test_roundtrip_fixture_redacted_payload_matches_expected(fixture_name: str) -> None:
    """The engine's ``redacted`` output must equal ``expected.json``.

    This is the load-bearing assertion of the CI gate. Both senses are
    covered by one ``==``:

    * ``raw -> redacted`` produced a string the policy says should
      stay (over-redaction) -> the equality fails on that key/leaf.
    * ``raw -> redacted`` left a string the policy says should be
      redacted (under-redaction / leak) -> the equality fails on the
      same key/leaf.

    The failure message includes a structured diff via pytest's
    built-in dict/list reprlib so a CI failure points the operator at
    the exact path that drifted.
    """
    fixture = FIXTURES_ROOT / fixture_name
    policy = parse_policy((fixture / "policy.yaml").read_text(encoding="utf-8"))
    raw = json.loads((fixture / "raw.json").read_text(encoding="utf-8"))
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    labels = _load_labels(fixture)

    result = redact(
        raw,
        policy,
        connector_id=labels.get("connector_id"),
        tenant=labels.get("tenant"),
        op=labels.get("op"),
    )

    assert result.redacted == expected, (
        f"round-trip mismatch for fixture {fixture_name!r}: "
        "the engine output differs from expected.json. "
        "Either the policy regressed (leak / over-redaction) or expected.json is stale."
    )


@pytest.mark.parametrize("fixture_name", _FIXTURE_NAMES)
def test_roundtrip_fixture_manifest_projection_matches_expected(fixture_name: str) -> None:
    """When ``manifest.json`` ships, the projected manifest must match.

    The projection covers the load-bearing fields -- ``rule``,
    ``pattern``, ``action``, ``count``, ``path``. ``span`` is excluded
    because the engine docstring marks it diagnostic-only (it indexes
    into the per-rule-input string, not the original). ``reason`` is
    excluded because it lives in the policy YAML, not the fixture
    data -- copying it into the fixture would couple unrelated files.

    Fixtures without a ``manifest.json`` skip this test cleanly --
    the per-fixture payload check above is sufficient on its own;
    the manifest projection is the extra signal an operator gets
    when authoring a new fixture and wants explicit per-rule
    evidence captured in-tree.
    """
    fixture = FIXTURES_ROOT / fixture_name
    manifest_file = fixture / "manifest.json"
    if not manifest_file.exists():
        pytest.skip(f"fixture {fixture_name!r} has no manifest.json")

    policy = parse_policy((fixture / "policy.yaml").read_text(encoding="utf-8"))
    raw = json.loads((fixture / "raw.json").read_text(encoding="utf-8"))
    labels = _load_labels(fixture)
    expected_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    result = redact(
        raw,
        policy,
        connector_id=labels.get("connector_id"),
        tenant=labels.get("tenant"),
        op=labels.get("op"),
    )

    actual = [_project_manifest_entry(entry) for entry in result.manifest]
    assert actual == expected_manifest, (
        f"manifest projection mismatch for fixture {fixture_name!r}: "
        "the engine's rule firings differ from manifest.json. "
        "Either the policy regressed (rules changed, scope drifted, "
        "pattern catalogue shifted) or manifest.json is stale."
    )


def _load_labels(fixture: pathlib.Path) -> dict[str, Any]:
    """Read the optional labels file; absent file -> empty dict.

    The engine treats unset labels as ``None`` (the scope-less
    wildcard); empty dict + ``.get()`` produces the same shape
    without a per-fixture branch.
    """
    labels_file = fixture / "labels.json"
    if not labels_file.exists():
        return {}
    parsed = json.loads(labels_file.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(
            f"labels.json in fixture {fixture.name!r} must be a JSON object, "
            f"got {type(parsed).__name__}",
        )
    return parsed


def _project_manifest_entry(entry: RedactionManifestEntry) -> dict[str, Any]:
    """Project a manifest entry onto the fixture-comparison fields.

    Mirrors the docstring above: rule / pattern / action / count /
    path only. The output is a plain dict so the equality check in
    pytest produces a readable diff (``RedactionManifestEntry``'s
    ``__eq__`` would compare ``span`` too).
    """
    return {
        "rule": entry.rule,
        "pattern": entry.pattern,
        "action": entry.action,
        "count": entry.count,
        "path": entry.path,
    }
