# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :func:`meho_backplane.features.build_features_block`.

The builder is a pure function over a :class:`Settings` snapshot. Tests
construct :class:`Settings` instances directly (no env var dance, no
``get_settings`` cache mutation) and assert the returned dict.

Coverage matrix (G0.14-T7 #1148):

* Each gated feature returns ``configured: false`` with the right
  ``missing_env`` list when env vars are unset.
* Each gated feature returns ``configured: true`` with an empty
  ``missing_env`` list when env vars are fully wired.
* ``audit_replay`` is the only entry that emits ``capture_mode``;
  the pre-T6 (#1147) value is the fixed string ``"enforced"``.
* ``approval_queue`` is transitive on ``agent_runtime`` — its
  ``configured`` mirrors ``agent_runtime.configured`` and the
  ``depends_on`` field surfaces the chain.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.features import build_features_block
from meho_backplane.settings import Settings


def _settings_with(**overrides: object) -> Settings:
    """Build a :class:`Settings` with the minimum required fields.

    All gate-relevant fields default to empty (unset) so a test that
    only sets one gate's fields gets ``configured: false`` on the rest.
    The non-gate fields are pinned to valid sentinels so construction
    cannot fail on an unrelated validator.
    """
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "postgresql+asyncpg://meho:secret@db.test:5432/meho",
        # The four gated features' env vars — empty by default.
        "keycloak_admin_url": "",
        "keycloak_admin_client_id": "",
        "keycloak_admin_client_secret": "",
        "ui_keycloak_client_id": "",
        "ui_keycloak_client_secret": "",
        "mcp_require_session_id": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# agent_runtime
# ---------------------------------------------------------------------------


def test_agent_runtime_unconfigured_lists_all_three_missing_env_vars() -> None:
    """Unset → ``configured=False`` and all three KEYCLOAK_ADMIN_* listed."""
    block = build_features_block(_settings_with())
    agent_runtime = block["agent_runtime"]
    assert agent_runtime["configured"] is False
    assert agent_runtime["missing_env"] == [
        "KEYCLOAK_ADMIN_URL",
        "KEYCLOAK_ADMIN_CLIENT_ID",
        "KEYCLOAK_ADMIN_CLIENT_SECRET",
    ]
    assert agent_runtime["docs"] == "docs/cross-repo/keycloak-agent-client.md"


def test_agent_runtime_configured_when_all_three_present() -> None:
    """All three set → ``configured=True`` and empty ``missing_env``."""
    block = build_features_block(
        _settings_with(
            keycloak_admin_url="https://keycloak.test/admin/realms/meho",
            keycloak_admin_client_id="meho-admin",
            keycloak_admin_client_secret="s3cret",
        )
    )
    agent_runtime = block["agent_runtime"]
    assert agent_runtime["configured"] is True
    assert agent_runtime["missing_env"] == []
    # Doc reference stays in the block on the happy path too: the
    # operator looking at the configured surface still wants the
    # provenance trail to the doc that explains the setup.
    assert agent_runtime["docs"] == "docs/cross-repo/keycloak-agent-client.md"


def test_agent_runtime_partial_lists_only_unset_env_vars() -> None:
    """Mid-state lists exactly the unset env vars, preserving order."""
    block = build_features_block(
        _settings_with(
            keycloak_admin_url="https://keycloak.test/admin/realms/meho",
            keycloak_admin_client_id="",  # unset
            keycloak_admin_client_secret="s3cret",
        )
    )
    agent_runtime = block["agent_runtime"]
    assert agent_runtime["configured"] is False
    assert agent_runtime["missing_env"] == ["KEYCLOAK_ADMIN_CLIENT_ID"]


# ---------------------------------------------------------------------------
# ui_surface
# ---------------------------------------------------------------------------


def test_ui_surface_unconfigured_lists_both_missing_env_vars() -> None:
    """Unset → ``configured=False`` and both UI_KEYCLOAK_* listed."""
    block = build_features_block(_settings_with())
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is False
    assert ui_surface["missing_env"] == [
        "UI_KEYCLOAK_CLIENT_ID",
        "UI_KEYCLOAK_CLIENT_SECRET",
    ]
    assert ui_surface["docs"] == "docs/cross-repo/keycloak-web-client.md"


def test_ui_surface_configured_when_both_present() -> None:
    """Both set → ``configured=True`` and empty ``missing_env``."""
    block = build_features_block(
        _settings_with(
            ui_keycloak_client_id="meho-web",
            ui_keycloak_client_secret="s3cret",
        )
    )
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is True
    assert ui_surface["missing_env"] == []


def test_ui_surface_partial_lists_only_unset_env_var() -> None:
    """One set, one unset → exactly one env var in ``missing_env``."""
    block = build_features_block(
        _settings_with(
            ui_keycloak_client_id="meho-web",
            ui_keycloak_client_secret="",
        )
    )
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is False
    assert ui_surface["missing_env"] == ["UI_KEYCLOAK_CLIENT_SECRET"]


# ---------------------------------------------------------------------------
# audit_replay
# ---------------------------------------------------------------------------


def test_audit_replay_capture_mode_is_enforced_pre_t6() -> None:
    """Pre-T6 (#1147): ``capture_mode`` is the fixed string ``"enforced"``.

    The value is independent of :attr:`Settings.mcp_require_session_id`
    today because capture and enforcement are the same knob — both
    on, both off. T6 will flip ``capture_mode`` to ``"always"`` in a
    one-line edit when capture is decoupled from enforcement.
    """
    block_off = build_features_block(_settings_with(mcp_require_session_id=False))
    block_on = build_features_block(_settings_with(mcp_require_session_id=True))

    for block in (block_off, block_on):
        audit_replay = block["audit_replay"]
        assert audit_replay["configured"] is True
        assert audit_replay["capture_mode"] == "enforced"
        assert audit_replay["missing_env"] == []
        # No ``docs`` field — the capture is feature-coupled to MCP
        # itself, not to an admin-configurable knob. Operators don't
        # have a separate setup doc to read.
        assert "docs" not in audit_replay


# ---------------------------------------------------------------------------
# approval_queue
# ---------------------------------------------------------------------------


def test_approval_queue_unconfigured_when_agent_runtime_unconfigured() -> None:
    """``approval_queue.configured`` mirrors ``agent_runtime.configured``."""
    block = build_features_block(_settings_with())
    approval_queue = block["approval_queue"]
    assert approval_queue["configured"] is False
    assert approval_queue["depends_on"] == "agent_runtime"
    # No ``missing_env`` field — the transitive dependency means
    # the operator's remediation is "configure agent_runtime", not
    # "set a list of env vars on the queue itself".
    assert "missing_env" not in approval_queue


def test_approval_queue_configured_when_agent_runtime_configured() -> None:
    """Once ``agent_runtime`` is wired, the queue activates."""
    block = build_features_block(
        _settings_with(
            keycloak_admin_url="https://keycloak.test/admin/realms/meho",
            keycloak_admin_client_id="meho-admin",
            keycloak_admin_client_secret="s3cret",
        )
    )
    approval_queue = block["approval_queue"]
    assert approval_queue["configured"] is True
    assert approval_queue["depends_on"] == "agent_runtime"


# ---------------------------------------------------------------------------
# Block-level shape
# ---------------------------------------------------------------------------


def test_features_block_carries_all_four_entries() -> None:
    """The block enumerates the four gated features by exact key name.

    Operator tooling (and downstream alerting) keys off these names.
    Renaming any of them is a wire-compat break that this assertion
    catches at test time rather than at integration time.
    """
    block = build_features_block(_settings_with())
    assert set(block.keys()) == {
        "agent_runtime",
        "ui_surface",
        "audit_replay",
        "approval_queue",
    }


@pytest.mark.parametrize(
    "feature",
    ["agent_runtime", "ui_surface", "audit_replay", "approval_queue"],
)
def test_every_feature_has_configured_bool(feature: str) -> None:
    """Each entry carries a ``configured: bool`` — the load-bearing field."""
    block = build_features_block(_settings_with())
    assert isinstance(block[feature]["configured"], bool)
