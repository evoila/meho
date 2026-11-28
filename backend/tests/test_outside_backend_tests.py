# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard for the unit tests that read files outside ``backend/`` (#5296).

On a pull request that changes no file under ``backend/``, CI skips the unit
shards and runs only the files that ``scan()`` finds. A test that reads a
file outside ``backend/`` but is not found would then not run, and a pull
request that breaks it would show green.

There is no hand-kept list any more: CI asks ``scan()``. A new test file
needs no edit here. :data:`FOUND_ON_2026_11_28` holds what the hand-kept
list held on that day. The guard checks that the scan still finds each of
these files, so a change to the scan cannot quietly drop one from the job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests import _outside_backend_tests as outside

# The hand-kept list on 2026-11-28, when the scan replaced it. Only remove
# lines: a file that was deleted is skipped, a file that no longer reads
# outside backend/ is taken off with the line the guard prints.
FOUND_ON_2026_11_28: tuple[str, ...] = (
    "tests/retrieval_eval/test_ci_run_eval_gate.py",
    "tests/test_agent_toolset_working_surface.py",
    "tests/test_api_v1_operations_preview_response.py",
    "tests/test_api_v1_settings.py",
    "tests/test_approval_table_judge.py",
    "tests/test_auth_old_groups_read_as_new_roles.py",
    "tests/test_broadcast_person_guidance.py",
    "tests/test_chart_approval_handoff.py",
    "tests/test_chart_bring_your_own_render.py",
    "tests/test_chart_broadcast_hardening.py",
    "tests/test_chart_core_bootstrap.py",
    "tests/test_chart_core_keycloak.py",
    "tests/test_chart_core_keycloak_own_postgres.py",
    "tests/test_chart_core_keycloak_role_check.py",
    "tests/test_chart_core_openbao.py",
    "tests/test_chart_core_postgres.py",
    "tests/test_chart_core_secrets.py",
    "tests/test_chart_core_wiring.py",
    "tests/test_chart_dispatch_limits.py",
    "tests/test_chart_image_digest_pin.py",
    "tests/test_chart_image_registry.py",
    "tests/test_chart_k3s.py",
    "tests/test_chart_knowledge_settings_values.py",
    "tests/test_chart_mcp_resource_uri.py",
    "tests/test_chart_migrate_init_container.py",
    "tests/test_chart_migrate_job_no_sa.py",
    "tests/test_chart_network_policy_extra_egress.py",
    "tests/test_chart_observability_scrape.py",
    "tests/test_chart_openai_endpoint.py",
    "tests/test_chart_progress_deadline.py",
    "tests/test_chart_result_cache.py",
    "tests/test_chart_runtime_database_login.py",
    "tests/test_chart_safe_updates.py",
    "tests/test_chart_sandbox_mode.py",
    "tests/test_chart_session_store_values.py",
    "tests/test_chart_update_strategy_job.py",
    "tests/test_chart_vault_addr_optional.py",
    "tests/test_check_install_secrets.py",
    "tests/test_check_tested_live_claims.py",
    "tests/test_ci_backend_python_skip.py",
    "tests/test_ci_check_chart_namespace_scope.py",
    "tests/test_ci_check_connector_kit_release.py",
    "tests/test_ci_check_contract_compat.py",
    "tests/test_ci_check_plugin_version.py",
    "tests/test_ci_check_private_names.py",
    "tests/test_ci_check_release_body_paths.py",
    "tests/test_ci_coverage_shards.py",
    "tests/test_ci_dockerhub_login_soft_fail.py",
    "tests/test_ci_docs_deploy.py",
    "tests/test_ci_fork_runner_isolation.py",
    "tests/test_ci_helm_golden.py",
    "tests/test_ci_install_node_step.py",
    "tests/test_ci_install_php_cli_step.py",
    "tests/test_ci_main_push.py",
    "tests/test_ci_measure_test_map.py",
    "tests/test_ci_memory_sampler.py",
    "tests/test_ci_merge_queue_cleanup.py",
    "tests/test_ci_redaction_compare_summary_only.py",
    "tests/test_ci_repo_secrets.py",
    "tests/test_ci_runner_allow_list.py",
    "tests/test_ci_second_runner_site.py",
    "tests/test_ci_security_weekly_issue.py",
    "tests/test_ci_spec_shelf_lists.py",
    "tests/test_ci_test_images.py",
    "tests/test_ci_upgrade_release_lookup.py",
    "tests/test_connector_kit.py",
    "tests/test_connector_overlay_build.py",
    "tests/test_connector_overlay_review.py",
    "tests/test_connectors_keycloak_authflow_touch.py",
    "tests/test_connectors_linux_day0_recipe.py",
    "tests/test_connectors_no_phantom_readback_tool.py",
    "tests/test_consumer_doc_tool_names.py",
    "tests/test_consumer_routing_render.py",
    "tests/test_core_company_login.py",
    "tests/test_core_keycloak_realm.py",
    "tests/test_core_keycloak_stage.py",
    "tests/test_core_vault_init.py",
    "tests/test_db_engine.py",
    "tests/test_examples_r1_tiered_triage.py",
    "tests/test_examples_r2_approval_gate.py",
    "tests/test_examples_r4_local_claude.py",
    "tests/test_features_doc_consistency.py",
    "tests/test_hard_to_undo_upgrade_query.py",
    "tests/test_inventory_shapes.py",
    "tests/test_live_test_cells.py",
    "tests/test_live_tests_record.py",
    "tests/test_maturity_surface_drift.py",
    "tests/test_mcp_surface_conformance.py",
    "tests/test_operations_connector_auth_failed.py",
    "tests/test_operations_meta_tools.py",
    "tests/test_people_behind_docs.py",
    "tests/test_policy_routing.py",
    "tests/test_policy_routing_old_kinds.py",
    "tests/test_policy_rule_sources_doc.py",
    "tests/test_policy_small_team_parity.py",
    "tests/test_policy_switch_texts.py",
    "tests/test_proxy_headers.py",
    "tests/test_redaction_compare_judge.py",
    "tests/test_reference_docs_drift.py",
    "tests/test_release_security_sheet.py",
    "tests/test_shared_lists_only_shrink.py",
    "tests/test_skip_limit_check.py",
    "tests/test_soak_harness_driver.py",
    "tests/test_soak_harness_host_detach_from_vds.py",
    "tests/test_ssh_host_key_pins_script.py",
    "tests/test_targets_file_schema.py",
    "tests/test_ui_principal_blocks.py",
    "tests/test_upgrade_rows.py",
    "tests/test_vault_tenant_admin_policy_doc.py",
    "tests/test_vcf_api_contract_manifest.py",
    "tests/test_work_ref_rule.py",
)


def test_scan_still_finds_every_file_it_found_on_2026_11_28() -> None:
    found = outside.scan()
    lost = [
        name
        for name in FOUND_ON_2026_11_28
        if (outside.BACKEND_DIR / name).is_file() and name not in found
    ]
    assert not lost, "\n".join(
        f"{name} no longer reads files outside backend/, so the job "
        "python-outside-backend stops running it. If that is right, remove the "
        f"line '    \"{name}\",' from FOUND_ON_2026_11_28 in "
        "backend/tests/test_outside_backend_tests.py."
        for name in lost
    )


def test_pin_is_sorted_and_has_no_duplicates() -> None:
    files = list(FOUND_ON_2026_11_28)
    assert files == sorted(set(files))


def test_script_prints_the_scan() -> None:
    printed = subprocess.run(
        [sys.executable, str(outside.TESTS_DIR / "_outside_backend_tests.py")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert printed == sorted(outside.scan())
    assert printed


# --- The scanner itself, on a fake backend/ tree -----------------------------


def _fake_backend(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``tmp_path/backend/tests``.

    The sources spell the two patterns the scan looks for a little
    differently (``FILE``, and a space before the bracket of the helper
    call). This writer puts the real spelling back, so the scan of the real
    test tree does not find this test file itself.
    """
    tests_dir = tmp_path / "backend" / "tests"
    for name, source in files.items():
        path = tests_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        real = source.replace("FILE", "__file__").replace("_repo_root (", "_repo_root" + "(")
        path.write_text(real, encoding="utf-8")
    return tests_dir


@pytest.mark.parametrize(
    ("name", "source", "reads_outside"),
    [
        # backend/tests/x.py: parents[2] is the repository root.
        ("test_a.py", 'R = Path(FILE).resolve().parents[2] / "deploy"\n', True),
        ("test_a.py", 'R = Path(FILE).resolve().parents[1] / "src"\n', False),
        ("test_a.py", 'R = Path(FILE).parent.parent.parent / "cli"\n', True),
        ("test_a.py", 'R = Path(FILE).parent.parent / "alembic"\n', False),
        ("test_a.py", "ROOT = _repo_root (Path(FILE))\n", True),
        # One folder deeper, the root is one level further up.
        ("acceptance/test_a.py", 'R = Path(FILE).resolve().parents[2] / "src"\n', False),
        ("acceptance/test_a.py", 'R = Path(FILE).resolve().parents[3] / "docs"\n', True),
        # Integration and migration tests are not part of the unit shards.
        ("integration/test_a.py", 'R = Path(FILE).resolve().parents[3] / "cli"\n', False),
        ("migrations/test_a.py", 'R = Path(FILE).resolve().parents[3] / "ci"\n', False),
    ],
)
def test_scan_finds_reads_outside_backend(
    tmp_path: Path, name: str, source: str, reads_outside: bool
) -> None:
    tests_dir = _fake_backend(tmp_path, {name: source})
    expected = {f"tests/{name}"} if reads_outside else set()
    assert outside.scan(tests_dir) == expected


def test_scan_follows_helper_modules(tmp_path: Path) -> None:
    tests_dir = _fake_backend(
        tmp_path,
        {
            "_helper.py": 'VALUES = Path(FILE).resolve().parents[2] / "deploy"\n',
            "test_uses_helper.py": "from tests._helper import VALUES\n",
            "test_plain.py": "import json\n",
        },
    )
    assert outside.scan(tests_dir) == {"tests/test_uses_helper.py"}
