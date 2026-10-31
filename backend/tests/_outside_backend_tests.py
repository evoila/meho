# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The unit test files that read files outside ``backend/``.

Some backend tests read files from other parts of the repository: the Helm
chart under ``deploy/``, the Go CLI under ``cli/``, the workflows under
``.github/``, the docs, the examples. A pull request that changes no file
under ``backend/`` can still break them.

CI skips the four unit shards on such a pull request (#5296). It runs only
the files in ``OUTSIDE_BACKEND_TESTS``, in the job
``python-outside-backend`` of ``.github/workflows/ci.yml``. The merge queue
always runs every test.

``test_outside_backend_tests.py`` keeps the list correct: it fails when
``scan()`` finds a file that is not on the list, or when the list holds a
file that ``scan()`` does not find.

What ``scan()`` counts as "reads outside ``backend/``":

* ``Path(__file__)`` followed by ``.parents[N]`` or a chain of ``.parent``
  that climbs to the repository root (one level above ``backend/``);
* a call to a local helper named ``_repo_root``;
* an import of a helper module in ``tests/`` that does one of the two
  above (for example ``tests/_pgvector_image.py``, which reads the chart's
  ``values.yaml``).

Only the files the unit shards run are scanned: everything under ``tests/``
except ``tests/integration/`` and ``tests/migrations/``. The migration tests
have their own change detector. The integration tests need containers, so
the merge queue runs them.

CI usage::

    uv run python tests/_outside_backend_tests.py   # one path per line

This is CI tooling, not a test (the leading underscore keeps pytest from
collecting it).
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent

# Paths are relative to backend/, sorted.
OUTSIDE_BACKEND_TESTS: tuple[str, ...] = (
    "tests/test_api_v1_operations_preview_response.py",
    "tests/test_chart_approval_handoff.py",
    "tests/test_chart_bring_your_own_render.py",
    "tests/test_chart_broadcast_hardening.py",
    "tests/test_chart_core_bootstrap.py",
    "tests/test_chart_core_keycloak.py",
    "tests/test_chart_core_openbao.py",
    "tests/test_chart_core_postgres.py",
    "tests/test_chart_core_secrets.py",
    "tests/test_chart_image_digest_pin.py",
    "tests/test_chart_image_registry.py",
    "tests/test_chart_mcp_resource_uri.py",
    "tests/test_chart_migrate_init_container.py",
    "tests/test_chart_migrate_job_no_sa.py",
    "tests/test_chart_network_policy_extra_egress.py",
    "tests/test_chart_observability_scrape.py",
    "tests/test_chart_result_cache.py",
    "tests/test_chart_sandbox_mode.py",
    "tests/test_chart_vault_addr_optional.py",
    "tests/test_ci_backend_python_skip.py",
    "tests/test_ci_check_chart_namespace_scope.py",
    "tests/test_ci_check_plugin_version.py",
    "tests/test_ci_check_release_body_paths.py",
    "tests/test_ci_coverage_shards.py",
    "tests/test_ci_fork_runner_isolation.py",
    "tests/test_ci_measure_test_map.py",
    "tests/test_ci_merge_queue_cleanup.py",
    "tests/test_ci_merge_queue_watchdog.py",
    "tests/test_ci_run_eval_gate.py",
    "tests/test_connector_overlay_build.py",
    "tests/test_connector_overlay_review.py",
    "tests/test_connectors_linux_day0_recipe.py",
    "tests/test_connectors_no_phantom_readback_tool.py",
    "tests/test_connectors_vmware_rest_doc_composite_counts.py",
    "tests/test_consumer_doc_tool_names.py",
    "tests/test_consumer_routing_render.py",
    "tests/test_core_keycloak_realm.py",
    "tests/test_core_keycloak_stage.py",
    "tests/test_core_vault_init.py",
    "tests/test_db_engine.py",
    "tests/test_examples_r1_tiered_triage.py",
    "tests/test_examples_r2_approval_gate.py",
    "tests/test_examples_r4_local_claude.py",
    "tests/test_features_doc_consistency.py",
    "tests/test_inventory_shapes.py",
    "tests/test_maturity_surface_drift.py",
    "tests/test_mcp_surface_conformance.py",
    "tests/test_operations_connector_auth_failed.py",
    "tests/test_operations_meta_tools.py",
    "tests/test_proxy_headers.py",
    "tests/test_reference_docs_drift.py",
    "tests/test_soak_harness_driver.py",
    "tests/test_soak_harness_host_detach_from_vds.py",
    "tests/test_vault_tenant_admin_policy_doc.py",
    "tests/test_vcf_api_contract_manifest.py",
)

# `Path(__file__)`, an optional `.resolve()`, then `.parents[N]` or `.parent`s.
_FILE_PARENTS = re.compile(
    r"__file__\)(?:\.resolve\(\))?(?P<chain>(?:\.parent\b)+|\.parents\[(?P<index>\d+)\])"
)
_REPO_ROOT_CALL = re.compile(r"\b_repo_root\(")


def _scanned_files(tests_dir: Path) -> list[Path]:
    """Every ``.py`` file the unit shards can load: no integration, no migrations."""
    return sorted(
        path
        for path in tests_dir.rglob("*.py")
        if path.relative_to(tests_dir).parts[0] not in ("integration", "migrations")
    )


def _climbs_to_repo_root(path: Path, backend_dir: Path) -> bool:
    """True when ``path`` reaches a file one level above ``backend/``."""
    # backend/tests/x.py: parents[0] is tests/, parents[1] is backend/,
    # parents[2] is the repository root. One more level per subfolder.
    root_index = len(path.relative_to(backend_dir).parts)
    source = path.read_text(encoding="utf-8")
    for match in _FILE_PARENTS.finditer(source):
        if match.group("index") is not None:
            index = int(match.group("index"))
        else:
            index = match.group("chain").count(".parent") - 1
        if index >= root_index:
            return True
    return bool(_REPO_ROOT_CALL.search(source))


def scan(tests_dir: Path = TESTS_DIR) -> set[str]:
    """Return the test files that read outside ``backend/``, relative to it."""
    backend_dir = tests_dir.parent
    files = _scanned_files(tests_dir)
    helpers = {
        path.stem
        for path in files
        if not path.name.startswith("test_") and _climbs_to_repo_root(path, backend_dir)
    }
    found: set[str] = set()
    for path in files:
        if not path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        imports_helper = any(
            re.search(rf"^\s*(?:from|import)\s.*\b{re.escape(name)}\b", source, re.MULTILINE)
            for name in helpers
        )
        if imports_helper or _climbs_to_repo_root(path, backend_dir):
            found.add(str(path.relative_to(backend_dir)))
    return found


if __name__ == "__main__":
    print("\n".join(OUTSIDE_BACKEND_TESTS))
