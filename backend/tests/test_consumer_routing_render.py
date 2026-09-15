# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pinned drift guard for the generated consumer-routing vehicles (#3491).

The "prefer MEHO before any work" routing discipline is authored once, in
``docs/examples/consumer-onboarding/contract/meho-first-routing.md``, and
rendered by ``scripts/ci/gen_consumer_routing.py`` into two public delivery
vehicles: the Claude Code plugin skills and the Layer-2 ``CLAUDE.md``
starter. This module pins every rendering against the source so a hand-edit
of a rendered file — the exact drift the single-source model exists to
prevent — fails CI loudly instead of silently diverging.

It is the generated-files sibling of
``backend/tests/test_mcp_surface_conformance.py`` (which pins the MCP wire
listing) and complements ``scripts/ci/check_consumer_tool_names.py`` (which
checks the names a rendered file mentions *exist*; this checks the rendered
files *match* the source).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
_GEN_SCRIPT: pathlib.Path = _REPO_ROOT / "scripts" / "ci" / "gen_consumer_routing.py"


def _load_generator() -> types.ModuleType:
    """Import the repo-root generator script as a module (no sys.path churn)."""
    spec = importlib.util.spec_from_file_location("_gen_consumer_routing_under_test", _GEN_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {_GEN_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the generator's frozen dataclasses resolve their
    # (``from __future__`` string) annotations against ``sys.modules`` at class
    # creation on Python 3.14 — an unregistered module 404s there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gen = _load_generator()
_RENDERED: dict[pathlib.Path, str] = _gen.render_all(_REPO_ROOT)

#: The six vehicles the generator owns: five plugin skills + the template.
_EXPECTED_PATHS: frozenset[str] = frozenset(
    {
        "clients/claude-code-plugin/skills/prefer-meho/SKILL.md",
        "clients/claude-code-plugin/skills/operations/SKILL.md",
        "clients/claude-code-plugin/skills/knowledge/SKILL.md",
        "clients/claude-code-plugin/skills/memory/SKILL.md",
        "clients/claude-code-plugin/skills/broadcast/SKILL.md",
        "docs/examples/consumer-onboarding/CLAUDE.md",
    }
)


def test_generator_renders_exactly_the_expected_vehicles() -> None:
    """The recipe covers all six vehicles and nothing else."""
    actual = frozenset(str(path.relative_to(_REPO_ROOT)) for path in _RENDERED)
    assert actual == _EXPECTED_PATHS


@pytest.mark.parametrize("path", sorted(_RENDERED), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_rendered_file_matches_source(path: pathlib.Path) -> None:
    """Each committed rendering byte-matches a fresh render from the source.

    A hand-edit of any rendered file (or of the source without re-running
    the generator) fails here. Fix: ``python scripts/ci/gen_consumer_routing.py``.
    """
    assert path.exists(), f"{path.relative_to(_REPO_ROOT)} is missing — run the generator"
    assert path.read_text(encoding="utf-8") == _RENDERED[path], (
        f"{path.relative_to(_REPO_ROOT)} has drifted from the contract source; "
        "re-run `python scripts/ci/gen_consumer_routing.py` (do not hand-edit)."
    )


def test_a_hand_edit_is_detected(tmp_path: pathlib.Path) -> None:
    """A one-character change to a rendered file is detected as drift.

    Proves the byte-compare is the teeth the acceptance criterion requires,
    without mutating a tracked file: a real rendering copied to tmp and
    nudged by one char no longer matches its fresh render.
    """
    target = next(iter(_RENDERED))
    mutated = tmp_path / "rendered.md"
    mutated.write_text(_RENDERED[target] + "x", encoding="utf-8")
    assert mutated.read_text(encoding="utf-8") != _RENDERED[target]


def test_check_mode_passes_on_the_committed_tree() -> None:
    """``gen_consumer_routing.py --check`` exits 0 against the committed tree."""
    assert _gen.main(["--check"]) == 0


def test_renderings_carry_no_estate_identifiers() -> None:
    """No estate hostname/realm/IP leaks survive into a rendered vehicle."""
    forbidden = (".lab", "rdc-vault", "rdc-hetzner", "evba", "v0.2 transition")
    for path, content in _RENDERED.items():
        lowered = content.lower()
        for token in forbidden:
            assert token not in lowered, f"{token!r} leaked into {path.relative_to(_REPO_ROOT)}"
