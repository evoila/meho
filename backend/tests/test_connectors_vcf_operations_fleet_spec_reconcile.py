# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile lane for the VCF Operations 9.1 fleet certificate and password calls.

Checks, against the vendor's VCF Operations 9.1.0 and 9.1.1 API files on the
spec shelf (``vcf-operations-9.1/``):

* every vendor call in
  :data:`~meho_backplane.connectors.vcf_operations.fleet_secrets_handlers.VENDOR_CALLS`
  is in both files;
* every filter and input value of either file (without the vendor's
  ``UNKNOWN`` placeholder) is in the op schemas, and the schemas hold no
  value that neither file knows;
* the values that only the 9.1.1 file knows are exactly
  :data:`~meho_backplane.connectors.vcf_operations.fleet_secrets_checks.ADDED_IN_9_1_1`.

Skips when the shelf is not configured (``tests/_spec_shelf.py``).

The vendor files fail MEHO's ingest check (some schemas carry an empty
``required`` list), so this lane does not run them through the ingest parser.
It reads the path keys and methods straight from the JSON and puts the
server base (``/suite-api``) in front, which gives the same ``METHOD:/path``
strings the parser would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.connectors.vcf_operations.fleet_secrets_checks import ADDED_IN_9_1_1
from meho_backplane.connectors.vcf_operations.fleet_secrets_handlers import VENDOR_CALLS
from meho_backplane.connectors.vcf_operations.fleet_secrets_ops import VROPS_FLEET_SECRETS_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "vcf-operations-9.1"
_SPEC_FILES = ["vcf-operations-openapi-9.1.0.json", "vcf-operations-openapi-9.1.1.json"]
_METHODS = ("get", "put", "post", "patch", "delete")
_UNKNOWN = "UNKNOWN"
_CERTIFICATE_QUERY = "/api/fleet-management/certificate-management/certificates/query"
_PASSWORD_QUERY = "/api/fleet-management/password-management/accounts/query"

#: Each value list of the op schemas, and where the vendor file keeps it:
#: ``("schema", <component schema>, <property>)`` for a request body field,
#: ``("query", <path>, <parameter>)`` for a query parameter.
_VALUE_LISTS: list[tuple[str, str, tuple[str, str, str]]] = [
    (
        "vrops.fleet.certificate.list",
        "appliance",
        ("schema", "VcfCertificateSearchRequest", "appliance"),
    ),
    (
        "vrops.fleet.certificate.list",
        "category",
        ("schema", "VcfCertificateSearchRequest", "category"),
    ),
    ("vrops.fleet.certificate.list", "status", ("schema", "VcfCertificateSearchRequest", "status")),
    ("vrops.fleet.certificate.list", "type", ("schema", "VcfCertificateSearchRequest", "type")),
    ("vrops.fleet.certificate.list", "sortBy", ("query", _CERTIFICATE_QUERY, "sortBy")),
    ("vrops.fleet.certificate.list", "sortOrder", ("query", _CERTIFICATE_QUERY, "sortOrder")),
    ("vrops.fleet.certificate.replace", "caType", ("schema", "CertificateReplaceData", "caType")),
    (
        "vrops.fleet.password.list",
        "appliance",
        ("schema", "VcfPasswordAccountSearchRequest", "appliance"),
    ),
    (
        "vrops.fleet.password.list",
        "status",
        ("schema", "VcfPasswordAccountSearchRequest", "status"),
    ),
    ("vrops.fleet.password.list", "sortBy", ("query", _PASSWORD_QUERY, "sortBy")),
    ("vrops.fleet.password.list", "sortOrder", ("query", _PASSWORD_QUERY, "sortOrder")),
]


def _served(spec_path: Path) -> set[str]:
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    base = document["servers"][0]["url"].rstrip("/")
    return {
        f"{method.upper()}:{base}{path}"
        for path, item in document["paths"].items()
        for method in _METHODS
        if method in item
    }


def _vendor_values(document: dict[str, Any], where: tuple[str, str, str]) -> set[str]:
    kind, holder, name = where
    if kind == "schema":
        prop = document["components"]["schemas"][holder]["properties"][name]
    else:
        prop = next(
            param["schema"]
            for param in document["paths"][holder]["post"]["parameters"]
            if param["name"] == name
        )
    return set(prop["enum"]) - {_UNKNOWN}


def _schema_values(op_id: str, name: str) -> set[str]:
    op = next(op for op in VROPS_FLEET_SECRETS_OPS if op.op_id == op_id)
    return set(op.parameter_schema["properties"][name]["enum"])


def _load(spec_file: str) -> dict[str, Any]:
    spec_path = require_shelf_spec(_SPEC_DIR, spec_file)
    document: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    return document


def test_vendor_calls_are_pinned() -> None:
    """Guard: the call list itself (runs without a shelf)."""
    assert len(VENDOR_CALLS) == 6
    assert all(call.split(":", 1)[1].startswith("/suite-api/api/") for call in VENDOR_CALLS)


@pytest.mark.parametrize("spec_file", _SPEC_FILES)
def test_fleet_calls_are_in_the_vendor_file(spec_file: str) -> None:
    spec_path = require_shelf_spec(_SPEC_DIR, spec_file)
    assert_op_ids_served(VENDOR_CALLS, _served(spec_path), spec_label=f"{_SPEC_DIR}/{spec_file}")


@pytest.mark.parametrize("spec_file", _SPEC_FILES)
def test_the_schemas_accept_every_value_of_the_vendor_file(spec_file: str) -> None:
    """A filter or input the vendor file supports never fails MEHO's parameter check."""
    document = _load(spec_file)
    for op_id, name, where in _VALUE_LISTS:
        missing = _vendor_values(document, where) - _schema_values(op_id, name)
        assert not missing, f"{spec_file}: {op_id} {name} lacks {sorted(missing)}"


def test_the_schemas_hold_no_value_that_neither_vendor_file_knows() -> None:
    documents = [_load(spec_file) for spec_file in _SPEC_FILES]
    for op_id, name, where in _VALUE_LISTS:
        known = set().union(*(_vendor_values(document, where) for document in documents))
        extra = _schema_values(op_id, name) - known
        assert not extra, f"{op_id} {name} has {sorted(extra)}, which no vendor file knows"


def test_the_9_1_1_table_is_the_difference_of_the_two_vendor_files() -> None:
    """The handlers refuse exactly the values the 9.1.0 file lacks on a 9.1.0 target."""
    old, new = (_load(spec_file) for spec_file in _SPEC_FILES)
    added: dict[str, set[str]] = {}
    for _op_id, name, where in _VALUE_LISTS:
        added.setdefault(name, set()).update(
            _vendor_values(new, where) - _vendor_values(old, where)
        )
    assert {name: values for name, values in added.items() if values} == {
        name: set(values) for name, values in ADDED_IN_9_1_1.items()
    }
