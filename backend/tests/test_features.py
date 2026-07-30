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
* ``approval_queue.effective_posture`` (#2087) resolves from
  ``Settings.approval_allow_self_approval``: ``four_eyes_enforced``
  by default, ``single_operator_break_glass`` under the emergency
  ``APPROVAL_ALLOW_SELF_APPROVAL=true`` escape.
* The feature-maturity registry (#2674) covers the #2664 propagation
  plan, honours the tier-conditional field contract, merges into the
  mapped ``/ready`` entries, and never leaks mutable state. Tier
  values are asserted **structurally only** — reclassification must
  remain a one-line registry edit.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from meho_backplane.features import FEATURE_MATURITY, build_features_block
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
        "ui_session_encryption_key": "",
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


def test_ui_surface_unconfigured_lists_all_three_missing_env_vars() -> None:
    """Unset → ``configured=False`` and all three UI_* env vars listed.

    G0.15-T5 (#1214) added ``UI_SESSION_ENCRYPTION_KEY`` to the
    enumerated set. Operators following the ``/ready`` self-doc were
    previously told to set the two client vars and redeploy; the BFF
    then 500'd on first session-cookie write because the Fernet key
    was unset. The block now lists the full set the operator must
    wire — matching :doc:`docs/cross-repo/keycloak-web-client.md`
    Check 3.
    """
    block = build_features_block(_settings_with())
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is False
    assert ui_surface["missing_env"] == [
        "UI_KEYCLOAK_CLIENT_ID",
        "UI_KEYCLOAK_CLIENT_SECRET",
        "UI_SESSION_ENCRYPTION_KEY",
    ]
    assert ui_surface["docs"] == "docs/cross-repo/keycloak-web-client.md"


def test_ui_surface_configured_when_all_three_present() -> None:
    """All three set → ``configured=True`` and empty ``missing_env``."""
    block = build_features_block(
        _settings_with(
            ui_keycloak_client_id="meho-web",
            ui_keycloak_client_secret="s3cret",
            ui_session_encryption_key="non-empty-placeholder",
        )
    )
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is True
    assert ui_surface["missing_env"] == []


def test_ui_surface_partial_lists_only_unset_env_vars() -> None:
    """Two set, one unset → exactly one env var in ``missing_env``."""
    block = build_features_block(
        _settings_with(
            ui_keycloak_client_id="meho-web",
            ui_keycloak_client_secret="s3cret",
            ui_session_encryption_key="",
        )
    )
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is False
    assert ui_surface["missing_env"] == ["UI_SESSION_ENCRYPTION_KEY"]


def test_ui_surface_only_session_key_present_lists_two() -> None:
    """The session-key set on its own leaves the two Keycloak vars listed.

    Guards against the regression where adding the third check
    reorders or shadows the first two. The order must remain
    declaration order: client-id, client-secret, session-key.
    """
    block = build_features_block(
        _settings_with(
            ui_keycloak_client_id="",
            ui_keycloak_client_secret="",
            ui_session_encryption_key="non-empty-placeholder",
        )
    )
    ui_surface = block["ui_surface"]
    assert ui_surface["configured"] is False
    assert ui_surface["missing_env"] == [
        "UI_KEYCLOAK_CLIENT_ID",
        "UI_KEYCLOAK_CLIENT_SECRET",
    ]


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


def test_approval_queue_effective_posture_defaults_to_four_eyes() -> None:
    """The default posture is the fail-closed four-eyes gate (#2087).

    ``Settings.approval_allow_self_approval`` defaults to ``False``,
    so a deploy that never touched ``APPROVAL_ALLOW_SELF_APPROVAL``
    reports ``four_eyes_enforced``. The posture field is independent
    of ``configured`` — it reflects the guard in
    ``operations.approval_queue.approve_request``, which is active
    whether or not the agent runtime is wired.
    """
    block = build_features_block(_settings_with())
    assert block["approval_queue"]["effective_posture"] == "four_eyes_enforced"


def test_approval_queue_effective_posture_reports_break_glass() -> None:
    """``APPROVAL_ALLOW_SELF_APPROVAL=true`` surfaces on ``/ready`` (#2087).

    With the emergency break-glass switch enabled, the block reports
    ``single_operator_break_glass`` so an operator or auditor can see
    the degraded posture from one GET instead of grepping the deploy's
    values file.
    """
    block = build_features_block(_settings_with(approval_allow_self_approval=True))
    assert block["approval_queue"]["effective_posture"] == "single_operator_break_glass"


# ---------------------------------------------------------------------------
# mcp (G0.14-T13 #1202)
# ---------------------------------------------------------------------------


def test_mcp_block_reports_server_pinned_protocol_version() -> None:
    """The ``mcp`` block surfaces the build-time pinned MCP revision.

    Acceptance criterion (G0.14-T13 #1202): ``/ready`` carries a
    ``features.mcp`` sub-block whose ``protocol_version`` field matches
    :data:`~meho_backplane.mcp.schemas.PROTOCOL_VERSION`. The shape
    mirrors ``audit_replay`` (no env var is "missing" — the constant
    is build-time, not deploy-time) and is independent of any
    :class:`Settings` field, so the assertion works against the default
    fixture without further env-var setup.
    """
    from meho_backplane.mcp.schemas import PROTOCOL_VERSION

    block = build_features_block(_settings_with())
    mcp = block["mcp"]
    assert mcp["configured"] is True
    assert mcp["protocol_version"] == PROTOCOL_VERSION
    assert mcp["missing_env"] == []
    # No ``docs`` field — the pinned version is a build-time constant,
    # not an admin-configurable knob. Same reasoning as ``audit_replay``.
    assert "docs" not in mcp


# ---------------------------------------------------------------------------
# Block-level shape
# ---------------------------------------------------------------------------


def test_features_block_carries_all_five_entries() -> None:
    """The block enumerates the five gated features by exact key name.

    Operator tooling (and downstream alerting) keys off these names.
    Renaming any of them is a wire-compat break that this assertion
    catches at test time rather than at integration time. The ``mcp``
    entry was added in G0.14-T13 (#1202).
    """
    block = build_features_block(_settings_with())
    assert set(block.keys()) == {
        "agent_runtime",
        "ui_surface",
        "audit_replay",
        "approval_queue",
        "mcp",
    }


@pytest.mark.parametrize(
    "feature",
    ["agent_runtime", "ui_surface", "audit_replay", "approval_queue", "mcp"],
)
def test_every_feature_has_configured_bool(feature: str) -> None:
    """Each entry carries a ``configured: bool`` — the load-bearing field."""
    block = build_features_block(_settings_with())
    assert isinstance(block[feature]["configured"], bool)


# ---------------------------------------------------------------------------
# Feature-maturity registry (#2674)
# ---------------------------------------------------------------------------

# The /ready-entry → registry-key mapping is a wire contract: #2675-#2678
# key their surface mappings on the registry names, and operator tooling
# reads the merged fields off these /ready entries. Pinned here (rather
# than imported from the module) so remapping an entry is a deliberate
# two-touch change. Tier values are deliberately NOT pinned anywhere in
# this file — reclassification must stay a one-line registry edit
# (#2674 acceptance criterion), so every tier expectation below derives
# from ``FEATURE_MATURITY`` itself.
READY_ENTRY_FEATURE_CONTRACT = {
    "agent_runtime": "agent_runtime",
    "ui_surface": "ui_console",
    "audit_replay": "audit",
    "approval_queue": "approvals",
}

_ISSUE_URL_RE = re.compile(r"^https://github\.com/evoila/meho/issues/\d+$")


def test_maturity_registry_covers_the_2664_classification() -> None:
    """Every feature in the #2664 propagation plan has a registry entry.

    The key set is the cross-surface contract: MCP tools (#2675), CLI
    commands (#2676), /ui areas (#2677), and the docs index / drift
    guard (#2678) all map onto these names. Renaming or dropping a key
    breaks every consumer at once — this pin catches it at test time.
    Adding a feature is an additive change here and in the registry.
    """
    assert set(FEATURE_MATURITY.keys()) == {
        # GA-track
        "audit",
        "memory_knowledge",
        "targets",
        "typed_connector_reads",
        "net_diagnostics",
        "approvals",
        "auth_tenancy",
        # Beta
        "sensors",
        "topology",
        "broadcast",
        "scheduler",
        "ui_console",
        "write_surfaces",
        "gsm_backend",
        "satellite_gateway",
        # Experimental
        "connector_ingest",
        "agent_runtime",
        "doc_collections",
        "two_world_ops",
    }


@pytest.mark.parametrize("feature_key", sorted(FEATURE_MATURITY.keys()))
def test_registry_entry_shape(feature_key: str) -> None:
    """Each entry honours the tier-conditional field contract.

    * ``maturity`` is one of the three tiers.
    * GA entries carry **neither** ``target_ga`` nor ``tracking`` —
      same absent-not-null convention as ``docs`` on the /ready gates.
    * Non-GA entries carry both: ``tracking`` is a concrete
      ``evoila/meho`` issue URL (the road-to-promotion pointer #2678
      renders), ``target_ga`` is a milestone string on beta entries
      and may be ``None`` only on experimental ones (outside the 1.0
      promise — no committed milestone to advertise).
    """
    entry = FEATURE_MATURITY[feature_key]
    maturity = entry["maturity"]
    assert maturity in {"ga", "beta", "experimental"}

    if maturity == "ga":
        assert "target_ga" not in entry
        assert "tracking" not in entry
        return

    assert _ISSUE_URL_RE.match(entry["tracking"]), (
        f"{feature_key}: tracking must be an evoila/meho issue URL, got {entry.get('tracking')!r}"
    )
    assert "target_ga" in entry
    if maturity == "beta":
        assert isinstance(entry["target_ga"], str) and entry["target_ga"]
    else:
        assert entry["target_ga"] is None or (
            isinstance(entry["target_ga"], str) and entry["target_ga"]
        )


@pytest.mark.parametrize(
    ("entry_name", "feature_key"),
    sorted(READY_ENTRY_FEATURE_CONTRACT.items()),
)
def test_ready_entry_merges_registry_maturity_fields(entry_name: str, feature_key: str) -> None:
    """Each mapped /ready entry renders its feature's maturity fields.

    Expectations derive from the registry, not from hardcoded tiers:
    retiering a feature must flip this surface with zero test edits.
    The merge is exactly the registry entry — no extra fields, no
    dropped fields.
    """
    block = build_features_block(_settings_with())
    entry = block[entry_name]
    for field, value in FEATURE_MATURITY[feature_key].items():
        assert entry[field] == value, (
            f"/ready entry {entry_name!r} renders {field}={entry.get(field)!r}; "
            f"registry key {feature_key!r} says {value!r}"
        )
    if FEATURE_MATURITY[feature_key]["maturity"] == "ga":
        assert "target_ga" not in entry
        assert "tracking" not in entry


def test_mcp_entry_carries_no_maturity_fields() -> None:
    """The ``mcp`` entry reports a protocol constant, not a feature.

    Individual MCP tools inherit maturity from their owning feature at
    registration time (#2675); labelling the transport block itself
    would double-count. If ``mcp`` ever becomes a classified feature,
    add it to the registry and the entry mapping together.
    """
    block = build_features_block(_settings_with())
    assert "maturity" not in block["mcp"]
    assert "target_ga" not in block["mcp"]
    assert "tracking" not in block["mcp"]


def test_builder_stays_pure_and_does_not_alias_the_registry() -> None:
    """Purity contract: repeatable output, no shared mutable state.

    Two calls over equivalent :class:`Settings` snapshots return equal
    blocks; mutating a returned entry leaks into neither the registry
    nor a subsequent call's result. Guards against the tempting
    ``block[name] = FEATURE_MATURITY[key]``-style aliasing bug — the
    merge must copy fields into the per-call dict.
    """
    registry_snapshot = copy.deepcopy(FEATURE_MATURITY)

    first = build_features_block(_settings_with())
    second = build_features_block(_settings_with())
    assert first == second

    first["ui_surface"]["maturity"] = "mutated"
    first["ui_surface"]["tracking"] = "https://example.invalid"

    assert registry_snapshot == FEATURE_MATURITY
    third = build_features_block(_settings_with())
    assert third == second
