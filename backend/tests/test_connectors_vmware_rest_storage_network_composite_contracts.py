# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract texts of the vCenter storage and network multi-step operations.

Checks what these operations tell the AI, without calling vCenter:

* every example under ``examples`` validates against its parameter schema
  (JSON Schema Draft 2020-12, the check MEHO runs before each call);
* the VLAN rules of ``network.portgroup.create`` (not both) and
  ``network.portgroup.vlan.set`` (exactly one) are in the schema, so a call
  that breaks them fails before the call reaches the handler;
* every op has ``llm_instructions`` with the keys the AI reads.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import schemas
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.operations._validate import validate_params

_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "datastore.usage": schemas.DATASTORE_USAGE_PARAMETER_SCHEMA,
    "datastore.refresh": schemas.DATASTORE_REFRESH_PARAMETER_SCHEMA,
    "datastore.file.list": schemas.DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
    "datastore.vsan.folder.list": schemas.DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
    "datastore.dir.create": schemas.DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
    "datastore.file.delete": schemas.DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
    "storage_policy.list": schemas.STORAGE_POLICY_LIST_PARAMETER_SCHEMA,
    "storage_policy.create": schemas.STORAGE_POLICY_CREATE_PARAMETER_SCHEMA,
    "storage_policy.delete": schemas.STORAGE_POLICY_DELETE_PARAMETER_SCHEMA,
    "network.portgroup.audit": schemas.NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    "network.portgroup.create": schemas.NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
    "network.portgroup.security.set": schemas.NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
    "network.portgroup.vlan.set": schemas.NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA,
    "network.portgroup.delete": schemas.NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    "host.standard_portgroup.delete": schemas.HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
}

_READ_ONLY = {
    "datastore.usage",
    "datastore.refresh",
    "datastore.file.list",
    "datastore.vsan.folder.list",
    "storage_policy.list",
    "network.portgroup.audit",
}

_TRUNK = {"vlan_trunk_ranges": [{"start": 0, "end": 4094}]}
_ACCESS = {"vlan_id": 100}
_VLAN_CASES = {
    "trunk-only": _TRUNK,
    "access-only": _ACCESS,
    "both": {**_TRUNK, **_ACCESS},
    "neither": {},
}


@pytest.mark.parametrize("short_id", sorted(_PARAMETER_SCHEMAS))
def test_every_example_validates(short_id: str) -> None:
    schema = _PARAMETER_SCHEMAS[short_id]
    Draft202012Validator.check_schema(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, f"{short_id} has no example"
    for example in examples:
        assert validate_params(schema, example) == [], (short_id, example)


@pytest.mark.parametrize("short_id", sorted(_PARAMETER_SCHEMAS))
def test_llm_instructions_have_the_keys_the_ai_reads(short_id: str) -> None:
    guidance = STORAGE_NETWORK_LLM_INSTRUCTIONS[f"vmware.composite.{short_id}"]
    keys = {"when_to_call", "output_shape", "next_step"}
    if short_id not in _READ_ONLY:
        keys.add("preconditions")
    assert keys <= set(guidance), (short_id, keys - set(guidance))
    assert "when_to_use" not in guidance


def test_llm_instructions_cover_only_ops_in_this_table() -> None:
    expected = {f"vmware.composite.{s}" for s in _PARAMETER_SCHEMAS}
    assert set(STORAGE_NETWORK_LLM_INSTRUCTIONS) == expected


@pytest.mark.parametrize(
    ("case", "accepted"),
    [("trunk-only", True), ("access-only", True), ("both", False), ("neither", False)],
)
def test_vlan_set_takes_exactly_one_vlan_mode(case: str, accepted: bool) -> None:
    params = {"portgroup": "dvportgroup-42", **_VLAN_CASES[case]}
    errors = validate_params(schemas.NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA, params)
    assert (errors == []) is accepted, errors


@pytest.mark.parametrize(
    ("case", "accepted"),
    # Neither keeps the switch's default VLAN, as the handler always did.
    [("trunk-only", True), ("access-only", True), ("both", False), ("neither", True)],
)
def test_portgroup_create_refuses_both_vlan_modes(case: str, accepted: bool) -> None:
    params = {"vds": "dvs-31", "name": "<portgroup-name>", **_VLAN_CASES[case]}
    errors = validate_params(schemas.NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA, params)
    assert (errors == []) is accepted, errors
