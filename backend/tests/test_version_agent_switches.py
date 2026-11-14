# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``agent_switches`` list on ``GET /version`` (#6178).

These tests check:

* every ``Settings`` field is in exactly one of two groups: agent-facing
  or not agent-facing, so a new setting cannot be left out by mistake;
* the fields with the ``AGENT_FACING`` mark are exactly the agent-facing
  group, and each of them is a ``bool``;
* ``GET /version`` lists each switch by env name, sorted, with the value
  ``get_settings`` read from that env name;
* the answer holds only these names and ``true`` / ``false``, no secret
  and no host name;
* ``meho_status`` and ``tools/list`` do not carry the list.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from meho_backplane.api.v1.health import HealthResponse
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import AGENT_FACING, Settings, get_settings
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: Settings that change what an AI agent sees. ``GET /version`` lists them.
AGENT_FACING_SETTINGS: frozenset[str] = frozenset(
    {
        "agent_operation_tools_from_mcp",
        "agent_reads_custom_description",
        "checks_agent_events",
        "jsonflux_answer_ready",
        "k8s_node_list_resources",
        "kb_similar_check",
        "mcp_approval_exit_code",
        "mcp_reveal_fetch",
        "mcp_short_tool_texts",
        "mcp_topology_off_default_list",
        "operation_contract_guidance",
        "preview_typed_writes",
        "search_clear_lead",
        "search_plain_sentences",
        "team_knowledge_recall",
    }
)

#: Settings that do not change what an AI agent sees.
NOT_AGENT_FACING_SETTINGS: frozenset[str] = frozenset(
    {
        "agent_approval_wait_timeout_seconds",
        "agent_budget_degrade_threshold",
        "agent_default_model",
        "agent_grant_required",
        "agent_invoke_max_depth",
        "agent_max_output_tokens",
        "agent_model_backend",
        "agent_run_lease_ttl_seconds",
        "agent_run_reaper_enabled",
        "agent_run_reaper_max_per_tick",
        "agent_run_reaper_tick_interval_seconds",
        "agent_runs_disabled_global",
        "agent_runs_disabled_tenants",
        "agent_sync_timeout_seconds",
        "anthropic_api_key",
        "approval_allow_delegated_decision",
        "approval_allow_self_approval",
        "approval_default_ttl_seconds",
        "approval_expiry_enabled",
        "approval_expiry_tick_interval_seconds",
        "approval_handoff_encryption_key",
        "approval_one_time_code_acr_values",
        "approval_outcome_unknown_after_minutes",
        "approval_presence_touch_valid_seconds",
        "approval_reveal_fetch_window_seconds",
        "approval_sign_in_level",
        "audit_client_identity",
        "audit_wrapper_rows_retired",
        "backplane_url",
        "bedrock_default_model",
        "bedrock_region",
        "broadcast_announce_rate_per_minute",
        "broadcast_announcement_prune_enabled",
        "broadcast_announcement_prune_interval_seconds",
        "broadcast_announcement_retention_days",
        "broadcast_redis_password",
        "broadcast_redis_url",
        "broadcast_retention_hours",
        "check_runner_client_id",
        "check_runner_client_secret",
        "checks_alert_advisory_window_minutes",
        "checks_evidence_prune_enabled",
        "checks_evidence_prune_interval_seconds",
        "checks_evidence_retention_days",
        "checks_investigation_poll_timeout_seconds",
        "checks_investigator_agent",
        "checks_notify_suppression_window_minutes",
        "composite_max_depth",
        "corpus_allowed_hosts",
        "corpus_answer_timeout_seconds",
        "corpus_audience",
        "corpus_require_filters",
        "corpus_service_token",
        "corpus_timeout_seconds",
        "corpus_url",
        "credential_backend",
        "database_pool_size",
        "database_pool_timeout",
        "database_url",
        "dispatch_activity_advisory_window_minutes",
        "dispatch_concurrency_slot_ttl_seconds",
        "dispatch_max_concurrent_ops",
        "dispatch_max_concurrent_ops_overrides",
        "dispatch_rate_limit_per_minute",
        "dispatch_rate_limit_per_minute_overrides",
        "docs_debug_log_query_text",
        "enable_rbac_test_route",
        "event_drain_enabled",
        "event_drain_tick_interval_seconds",
        "events_ingest_rate_per_minute",
        "flight_recorder_enabled",
        "flight_recorder_reaper_enabled",
        "flight_recorder_reaper_max_per_tick",
        "flight_recorder_reaper_tick_interval_seconds",
        "flight_recorder_retention_days_default",
        "fresh_sign_in_max_age_seconds",
        "gateway_deadman_enabled",
        "gateway_deadman_tick_interval_seconds",
        "gateway_runner_stale_after_multiplier",
        "gateway_unreported_mint_enabled",
        "gateway_unreported_mint_tick_interval_seconds",
        "grant_expiry_enabled",
        "grant_expiry_tick_interval_seconds",
        "gsm_impersonate_sa",
        "gsm_param_allowed_projects",
        "gsm_project",
        "gsm_wif_audience",
        "gsm_wif_pool_id",
        "gsm_wif_provider_id",
        "gsm_wif_service_account",
        "gsm_wif_subject_token_type",
        "http_max_request_body_bytes",
        "jsonflux_sample_byte_budget",
        "jwt_approver_claim_name",
        "jwt_capabilities_claim_name",
        "jwt_platform_admin_claim_name",
        "jwt_platform_admin_role_name",
        "jwt_principal_kind_claim_name",
        "jwt_role_groups",
        "jwt_runner_id_claim_name",
        "jwt_scopes_claim_name",
        "jwt_self_approver_group",
        "jwt_service_account_username_claim",
        "jwt_service_account_username_prefix",
        "jwt_tenant_claim_name",
        "jwt_tenant_role_claim_name",
        "jwt_tenants_claim_name",
        "kb_ingest_root",
        "kb_shared_tenant_ids",
        "kb_similar_min_score",
        "keycloak_admin_client_id",
        "keycloak_admin_client_secret",
        "keycloak_admin_url",
        "keycloak_audience",
        "keycloak_cli_approve_client_id",
        "keycloak_cli_client_id",
        "keycloak_discovery_url",
        "keycloak_issuer_url",
        "keycloak_jwks_cache_ttl_seconds",
        "keycloak_jwt_leeway_seconds",
        "knowledge_upkeep_enabled",
        "knowledge_upkeep_interval_seconds",
        "mail_from",
        "mail_recipient_allowlist",
        "mail_smtp_ca_bundle",
        "mail_smtp_host",
        "mail_smtp_password",
        "mail_smtp_port",
        "mail_smtp_starttls",
        "mail_smtp_username",
        "mcp_client_per_tool_kind",
        "mcp_require_session_id",
        "mcp_resource_uri",
        "mcp_session_start_limit_per_minute",
        "mcp_session_start_limit_per_minute_overrides",
        "memory_expiry_enabled",
        "memory_expiry_tick_interval_seconds",
        "memory_user_default_ttl_days",
        "metrics_auth_token",
        "openai_api_key",
        "openai_base_url",
        "openai_default_model",
        "operation_run_lease_ttl_seconds",
        "operation_run_reaper_enabled",
        "operation_run_reaper_max_per_tick",
        "operation_run_reaper_tick_interval_seconds",
        "operation_search_store_query_text",
        "params_hash_key",
        "per_action_sign_in_max_age_seconds",
        "policy_classification",
        "policy_engine_shadow",
        "raw_payload_prune_enabled",
        "raw_payload_prune_interval_seconds",
        "raw_payload_retention_days",
        "reflex_advisory_window_minutes",
        "request_usage_retention_days",
        "result_cache_redis_password",
        "result_cache_redis_url",
        "result_cache_writer_enabled",
        "result_complete_response_wire_max_bytes",
        "result_handle_max_record_bytes",
        "result_handle_max_spill_rows",
        "result_query_executor_workers",
        "result_query_max_output_bytes",
        "result_query_max_output_rows",
        "result_query_rate_per_minute",
        "result_query_response_wire_max_bytes",
        "result_query_tenant_concurrency",
        "result_query_timeout_seconds",
        "result_reduction_max_decoded_bytes",
        "result_reduction_max_depth",
        "result_reduction_max_nodes",
        "retrieval_embedding_model",
        "retrieval_model_cache_dir",
        "sandbox_delete_grants_from_api",
        "sandbox_mode",
        "sandbox_mode_confirm",
        "satellite_write_allowlist",
        "satellite_write_signing_key",
        "satellite_write_verify_key",
        "scheduler_agent_secret_env_pattern",
        "scheduler_agent_vault_path_pattern",
        "scheduler_enabled",
        "scheduler_tick_interval_seconds",
        "sensor_auth_failure_park_threshold",
        "sensor_runner_enabled",
        "sensor_runner_stall_after_ticks",
        "sensor_runner_tick_interval_seconds",
        "service_grant_delete_shaped_patterns",
        "shutdown_drain_seconds",
        "spaces_enabled",
        "test_amortize_typed_op_registrars",
        "test_fail_start_after_registration",
        "test_stub_descriptor_embedding",
        "topology_history_prune_enabled",
        "topology_history_prune_interval_seconds",
        "topology_history_retention_days",
        "topology_refresh_interval_seconds",
        "ui_keycloak_client_id",
        "ui_keycloak_client_secret",
        "ui_session_absolute_lifetime_seconds",
        "ui_session_encryption_key",
        "ui_session_read_revalidation_seconds",
        "ui_session_sliding_extension_seconds",
        "vault_addr",
        "vault_check_runner_role",
        "vault_kv_folder_max_keys",
        "vault_kv_tenant_scope_prefix",
        "vault_namespace",
        "vault_oidc_mount_path",
        "vault_oidc_role",
        "vault_oidc_role_by_tenant",
        "vault_scheduler_token",
        "vault_scheduler_token_file",
        "vault_timeout_seconds",
        "vcf_paif_base_url",
        "vcf_paif_model",
        "vcf_paif_oidc_client_id",
        "vcf_paif_oidc_client_secret",
        "vcf_paif_oidc_scope",
        "vcf_paif_oidc_token_url",
        "web_push_enabled",
        "web_push_vapid_private_key",
    }
)

_DECIDE = (
    "decide if this setting changes what the AI sees; if yes, mark it agent-facing "
    "(json_schema_extra=AGENT_FACING) and add it to AGENT_FACING_SETTINGS, "
    "if no, add it to NOT_AGENT_FACING_SETTINGS"
)


def _marked() -> set[str]:
    return {
        name
        for name, field in Settings.model_fields.items()
        if field.json_schema_extra == AGENT_FACING
    }


def _set_all(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    for name in AGENT_FACING_SETTINGS:
        monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()


def test_every_setting_is_in_one_group() -> None:
    fields = set(Settings.model_fields)
    unsorted = fields - AGENT_FACING_SETTINGS - NOT_AGENT_FACING_SETTINGS
    assert not unsorted, f"{sorted(unsorted)}: {_DECIDE}"
    gone = (AGENT_FACING_SETTINGS | NOT_AGENT_FACING_SETTINGS) - fields
    assert not gone, f"{sorted(gone)} are no longer Settings fields; remove them here"
    assert not AGENT_FACING_SETTINGS & NOT_AGENT_FACING_SETTINGS


def test_marked_settings_are_the_agent_facing_group() -> None:
    assert _marked() == set(AGENT_FACING_SETTINGS), _DECIDE


def test_only_bool_settings_are_marked() -> None:
    wrong = {
        name: Settings.model_fields[name].annotation
        for name in _marked()
        if Settings.model_fields[name].annotation is not bool
    }
    assert not wrong, f"only bool settings may be agent-facing: {wrong}"


def test_version_lists_each_switch_off_by_default(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_with_operator
    for name in AGENT_FACING_SETTINGS:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()

    body = client.get("/version").json()

    expected = sorted(name.upper() for name in AGENT_FACING_SETTINGS)
    assert list(body["agent_switches"]) == expected
    assert set(body["agent_switches"].values()) == {False}
    assert {"git_sha", "build_date", "chart_version"} <= set(body)


@pytest.mark.parametrize("name", sorted(AGENT_FACING_SETTINGS))
def test_version_reads_each_switch_from_its_env_name(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    client, _ = client_with_operator
    _set_all(monkeypatch, "false")
    monkeypatch.setenv(name.upper(), "true")
    get_settings.cache_clear()

    switches = client.get("/version").json()["agent_switches"]

    assert switches[name.upper()] is True
    assert [key for key, on in switches.items() if on] == [name.upper()]


def test_version_answer_holds_only_switch_names_and_bools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_with_operator
    made_up_secret = "made-up-token-6178"
    monkeypatch.setenv("CORPUS_SERVICE_TOKEN", made_up_secret)
    monkeypatch.setenv("METRICS_AUTH_TOKEN", made_up_secret)
    _set_all(monkeypatch, "true")

    response = client.get("/version")

    switches = response.json()["agent_switches"]
    assert set(switches) == {name.upper() for name in AGENT_FACING_SETTINGS}
    assert all(value is True for value in switches.values())
    text = json.dumps(switches)
    for forbidden in (made_up_secret, "keycloak.test", "vault.test", "meho.test", "corpus.test"):
        assert forbidden not in response.text
        assert forbidden not in text


def test_agent_surfaces_do_not_carry_the_switch_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_with_operator
    _set_all(monkeypatch, "false")

    listed = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    status = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
    )

    for marker in ("agent_switches", "agent_facing"):
        assert marker not in listed.text
        assert marker not in status.text
    payload = json.loads(status.json()["result"]["content"][0]["text"])
    assert set(payload) <= set(HealthResponse.model_fields)
