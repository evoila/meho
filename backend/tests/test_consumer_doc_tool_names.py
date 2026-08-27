# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``scripts/ci/check_consumer_tool_names.py``.

The guard is the CI gate that keeps consumer-facing docs/skills honest
against the registered MCP tool surface (field-test finding F4, #3143):
a template or plugin skill that names a nonexistent tool
(``broadcast_announce`` instead of ``meho_broadcast_announce``,
``search_connectors`` which never existed) erodes the first-reflex
behaviour the plugin exists to build. Weak coverage here lets that drift
back in, so the matrix below pins:

* the **positive** case — the real repo tree passes (locks in that the
  #3150 sweep left the consumer surface clean, and that no future edit
  reintroduces a bad name);
* one **negative** case per detection rule — a synthetic doc with a bad
  name is flagged;
* the **guard-the-guard** invariants — the derived registered set is
  plausible and the denylist has not gone stale;
* the **CLI contract** — the script exits non-zero on a violation and
  zero on the clean tree, which is what the workflow relies on.

Synthetic bad docs are written into ``tmp_path`` and never land under a
scanned path, so they cannot trip (or be masked by) the real tree scan.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

#: Repo root resolved relative to this test file
#: (``backend/tests/<this>`` -> ``parents[2]``). The script is imported as
#: a module so failures render as pytest tracebacks; the subprocess tests
#: exercise the real CLI entrypoint separately.
_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH: pathlib.Path = _REPO_ROOT / "scripts" / "ci" / "check_consumer_tool_names.py"


def _load_script_module() -> object:
    """Import the CI guard script as a module without polluting ``sys.path``."""
    spec = importlib.util.spec_from_file_location(
        "_check_consumer_tool_names_under_test",
        _SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_module = _load_script_module()

_TOOLS_DIR: pathlib.Path = _REPO_ROOT / _module.DEFAULT_TOOLS_DIR
_HUMAN_ONLY: pathlib.Path = _REPO_ROOT / _module.DEFAULT_HUMAN_ONLY


def _real_legal() -> frozenset[str]:
    """The legal name set derived from the real in-repo source."""
    return frozenset(_module.legal_names(_TOOLS_DIR, _HUMAN_ONLY))


def _scan_doc(tmp_path: pathlib.Path, body: str) -> list[object]:
    """Write *body* to a synthetic doc and scan it against the real legal set."""
    doc = tmp_path / "doc.md"
    doc.write_text(body)
    return _module.check_paths((doc,), _real_legal())


# ---------------------------------------------------------------------------
# Guard-the-guard invariants
# ---------------------------------------------------------------------------


def test_registered_set_is_plausible_and_pins_known_names() -> None:
    """Extraction yields a plausibly-sized set with known literal + const tools."""
    registered = _module.registered_tool_names(_TOOLS_DIR)
    assert len(registered) >= 70, registered
    # A literal-registered working tool, a meho_-prefixed working tool, and
    # two const-registered tools (topology.py / audit.py register by CONST).
    for name in ("call_operation", "meho_broadcast_announce", "query_topology", "query_audit"):
        assert name in registered, f"{name} missing from derived registered set"


def test_denylist_is_not_stale() -> None:
    """No FORBIDDEN_NONEXISTENT token is actually a registered tool.

    If the backend ever registers e.g. ``search_connectors``, its denylist
    entry becomes wrong; this invariant makes that fail loudly.
    """
    registered = _module.registered_tool_names(_TOOLS_DIR)
    assert set(_module.FORBIDDEN_NONEXISTENT).isdisjoint(registered)


def test_human_only_verbs_are_legal_references() -> None:
    """The three human-only verbs (#3155) are legal to name in a doc."""
    legal = _real_legal()
    for verb in ("meho_approvals_approve", "meho_approvals_reject", "meho_agents_grant_elevate"):
        assert verb in legal


# ---------------------------------------------------------------------------
# Positive case — the real tree is clean
# ---------------------------------------------------------------------------


def test_real_consumer_tree_passes() -> None:
    """The real consumer-facing tree has no tool-name violations.

    Locks in the #3150 sweep result and fails any future edit that
    reintroduces a bad name into a scanned path.
    """
    roots = tuple(_REPO_ROOT / rel for rel in _module.DEFAULT_SCAN_ROOTS)
    violations = _module.check_paths(roots, _real_legal())
    assert violations == [], [v.as_line() for v in violations]


# ---------------------------------------------------------------------------
# Negative cases — one per detection rule
# ---------------------------------------------------------------------------


def test_bare_broadcast_name_is_flagged(tmp_path: pathlib.Path) -> None:
    """A bare ``broadcast_announce`` is flagged; the ``meho_`` form is not."""
    bad = _scan_doc(tmp_path, "Call `broadcast_announce` with phase=start.\n")
    assert len(bad) == 1
    assert "meho_broadcast_announce" in bad[0].message

    good = _scan_doc(tmp_path, "Call `meho_broadcast_announce` with phase=start.\n")
    assert good == []


@pytest.mark.parametrize(
    ("token", "hint"),
    [
        ("search_connectors", "meho_connector_list"),
        ("list_connectors", "meho_connector_list"),
        ("result_aggregate", "result_query"),
        ("result_export", "result_query"),
        ("result_describe", "result_query"),
        ("operation_id", "op_id"),
    ],
)
def test_forbidden_nonexistent_name_is_flagged(
    tmp_path: pathlib.Path, token: str, hint: str
) -> None:
    """Each never-registered name is flagged with its replacement hint."""
    violations = _scan_doc(tmp_path, f"Use the `{token}` tool to do the thing.\n")
    assert len(violations) == 1
    assert token in violations[0].message
    assert hint in violations[0].message


def test_unregistered_meho_prefixed_typo_is_flagged(tmp_path: pathlib.Path) -> None:
    """The wide net catches a ``meho_`` typo not on any hand-maintained list."""
    violations = _scan_doc(tmp_path, "Call `meho_broadcst_announce` first.\n")
    assert len(violations) == 1
    assert "meho_broadcst_announce" in violations[0].message


def test_unregistered_scoped_matcher_is_flagged(tmp_path: pathlib.Path) -> None:
    """A plugin-scoped matcher naming a nonexistent tool is flagged."""
    violations = _scan_doc(
        tmp_path, "The matcher `mcp__plugin_meho_meho__broadcast_announce` fires.\n"
    )
    # Bare-broadcast rule does NOT fire (prefixed by `meho_meho__`), but the
    # scoped-matcher wide-net rule does: the embedded tool is unregistered.
    assert len(violations) == 1
    assert "broadcast_announce" in violations[0].message


def test_correct_scoped_matcher_passes(tmp_path: pathlib.Path) -> None:
    """The correct doubled-prefix scoped matcher is accepted."""
    assert (
        _scan_doc(tmp_path, "The matcher `mcp__plugin_meho_meho__meho_broadcast_announce` fires.\n")
        == []
    )


def test_bare_meho_prefix_mention_is_not_flagged(tmp_path: pathlib.Path) -> None:
    """A prose mention of the ``meho_`` prefix itself is not a tool reference."""
    assert _scan_doc(tmp_path, "The `meho_` prefix is load-bearing; keep it.\n") == []


# ---------------------------------------------------------------------------
# CLI contract — the exact shape the workflow invokes
# ---------------------------------------------------------------------------


def test_cli_passes_on_clean_repo_tree() -> None:
    """``python scripts/ci/check_consumer_tool_names.py`` exits 0 on the repo."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cli_fails_on_a_bad_doc(tmp_path: pathlib.Path) -> None:
    """The CLI exits 1 and annotates when a scanned doc names a bad tool."""
    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text("Please call `search_connectors` to list connectors.\n")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), str(bad_doc)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout
    assert "::error" in result.stdout
    assert "search_connectors" in result.stdout
