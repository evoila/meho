# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-operations **9.1** real-spec reconcile lane for the fleet role calls.

Checks that every ``METHOD:/path`` the fleet role handlers call
(:data:`~meho_backplane.connectors.vcf_operations.fleet_iam_handlers.FLEET_IAM_DECLARED_OP_IDS`)
is in the pinned VCF Operations 9.1.0 and 9.1.1 API files on the shelf
(``vcf-operations-9.1/``, ``vmware/vcf-api-specs``, Apache-2.0). The calls
exist only from 9.1 on, so the 9.0 lane
(:mod:`tests.test_connectors_vcf_operations_spec_reconcile`) cannot check them.

Parse-only set compare: no DB, no network. It skips when the shelf is not
set up (``tests/_spec_shelf.py``). The vendor 9.1 files fail MEHO's OpenAPI
metaschema check (empty ``required`` arrays, the same as the 9.0 file), so
this lane reads the ``paths`` map directly and puts the
``servers[0].url`` prefix (``/suite-api``) in front, which is what an ingest
would dispatch. Standard: ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import json

import pytest

from meho_backplane.connectors.vcf_operations.fleet_iam_handlers import (
    FLEET_IAM_DECLARED_OP_IDS,
)
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "vcf-operations-9.1"
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})


def _served_op_ids(filename: str) -> set[str]:
    spec_path = require_shelf_spec(_SPEC_DIR, filename)
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if not str(document.get("openapi", "")).startswith("3."):
        pytest.fail(f"{filename}: expected an OpenAPI 3 document; update this lane.")
    servers = document.get("servers") or [{}]
    base = str(servers[0].get("url") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in (document.get("paths") or {}).items()
        for method in item
        if method in _HTTP_METHODS
    }
    if not served:
        pytest.fail(f"{filename}: no paths found; update this lane.")
    return served


def test_declared_set_is_not_empty() -> None:
    assert len(FLEET_IAM_DECLARED_OP_IDS) == 7


@pytest.mark.parametrize(
    "filename",
    ["vcf-operations-openapi-9.1.0.json", "vcf-operations-openapi-9.1.1.json"],
)
def test_fleet_role_calls_are_served_by_the_pinned_9_1_spec(filename: str) -> None:
    assert_op_ids_served(
        set(FLEET_IAM_DECLARED_OP_IDS),
        _served_op_ids(filename),
        spec_label=f"{_SPEC_DIR}/{filename}",
    )
