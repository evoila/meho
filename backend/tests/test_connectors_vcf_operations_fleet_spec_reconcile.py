# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile lane for the VCF Operations 9.1 fleet certificate and password calls.

Checks that every vendor call in
:data:`~meho_backplane.connectors.vcf_operations.fleet_secrets_handlers.VENDOR_CALLS`
is in the vendor's VCF Operations 9.1.0 and 9.1.1 API files on the spec shelf
(``vcf-operations-9.1/``). Skips when the shelf is not configured
(``tests/_spec_shelf.py``).

The vendor files fail MEHO's ingest check (some schemas carry an empty
``required`` list), so this lane does not run them through the ingest parser.
It reads the path keys and methods straight from the JSON and puts the
server base (``/suite-api``) in front, which gives the same ``METHOD:/path``
strings the parser would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meho_backplane.connectors.vcf_operations.fleet_secrets_handlers import VENDOR_CALLS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "vcf-operations-9.1"
_METHODS = ("get", "put", "post", "patch", "delete")


def _served(spec_path: Path) -> set[str]:
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    base = document["servers"][0]["url"].rstrip("/")
    return {
        f"{method.upper()}:{base}{path}"
        for path, item in document["paths"].items()
        for method in _METHODS
        if method in item
    }


def test_vendor_calls_are_pinned() -> None:
    """Guard: the call list itself (runs without a shelf)."""
    assert len(VENDOR_CALLS) == 6
    assert all(call.split(":", 1)[1].startswith("/suite-api/api/") for call in VENDOR_CALLS)


@pytest.mark.parametrize(
    "spec_file",
    ["vcf-operations-openapi-9.1.0.json", "vcf-operations-openapi-9.1.1.json"],
)
def test_fleet_calls_are_in_the_vendor_file(spec_file: str) -> None:
    spec_path = require_shelf_spec(_SPEC_DIR, spec_file)
    assert_op_ids_served(VENDOR_CALLS, _served(spec_path), spec_label=f"{_SPEC_DIR}/{spec_file}")
