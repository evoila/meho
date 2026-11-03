# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Example real-spec reconcile lane — the #2980 harness demonstration.

The template every per-vendor lane (#2981-#2993) follows: resolve the
pinned spec from the shelf (uniform skip when unconfigured), extract
its served op_ids through the real ingest parser, assert every
hand-coded ``METHOD:/path``. Runs for real in CI (keycloak-26.3 is in the
armed checkout). Standard: docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

# A real lane introspects this set from the connector's live constants
# (never a hardcoded mirror that can drift); see the #2944 lane.
_DEMO_DECLARED_OP_IDS = {"GET:/admin/realms/{realm}", "GET:/admin/realms/{realm}/clients"}


def test_example_lane_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    spec_path = require_shelf_spec("keycloak-26.3", "keycloak-admin-openapi.json")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(
        _DEMO_DECLARED_OP_IDS, served, spec_label="keycloak-26.3/keycloak-admin-openapi.json"
    )
