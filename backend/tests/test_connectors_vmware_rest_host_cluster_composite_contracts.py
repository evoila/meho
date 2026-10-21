# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract texts of the vCenter host and cluster multi-step operations.

Checks what these operations tell the AI, without calling vCenter:

* every example under ``examples`` validates against its parameter schema
  (JSON Schema Draft 2020-12, the check MEHO runs before each call);
* the "parent or cluster" rule of ``resource_pool.create`` is a ``oneOf``,
  so a call with both or neither fails before the call reaches the handler;
* every op has ``llm_instructions`` with the keys the AI reads.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import schemas
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.operations._validate import validate_params

_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "cluster.drs_recommendations": schemas.CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    "cluster.drs_rule.create": schemas.CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
    "cluster.drs_rule.delete": schemas.CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA,
    "cluster.drs_vm_host_rule.create": schemas.CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
    "cluster.patch": schemas.CLUSTER_PATCH_PARAMETER_SCHEMA,
    "host.datastore_mount_nfs": schemas.HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
    "host.detach_from_vds": schemas.HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
    "host.disk_mark_flash": schemas.HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
    "host.evacuate": schemas.HOST_EVACUATE_PARAMETER_SCHEMA,
    "host.service_control": schemas.HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
    "resource_pool.create": schemas.RESOURCE_POOL_CREATE_PARAMETER_SCHEMA,
    "resource_pool.delete": schemas.RESOURCE_POOL_DELETE_PARAMETER_SCHEMA,
}

_READ_ONLY = {"cluster.drs_recommendations"}


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
    guidance = HOST_CLUSTER_LLM_INSTRUCTIONS[f"vmware.composite.{short_id}"]
    keys = {"when_to_call", "output_shape", "next_step"}
    if short_id not in _READ_ONLY:
        keys.add("preconditions")
    assert keys <= set(guidance), (short_id, keys - set(guidance))
    assert "when_to_use" not in guidance


def test_llm_instructions_cover_only_ops_in_this_table() -> None:
    expected = {f"vmware.composite.{s}" for s in _PARAMETER_SCHEMAS}
    assert set(HOST_CLUSTER_LLM_INSTRUCTIONS) == expected


@pytest.mark.parametrize(
    ("parent", "accepted"),
    [
        ({"parent": "resgroup-42"}, True),
        ({"cluster": "domain-c7"}, True),
        ({"parent": "resgroup-42", "cluster": "domain-c7"}, False),
        ({}, False),
    ],
    ids=["parent-only", "cluster-only", "both", "neither"],
)
def test_resource_pool_parent_is_pool_or_cluster_never_both(
    parent: dict[str, str], accepted: bool
) -> None:
    params = {"name": "<pool-name>", **parent}
    errors = validate_params(schemas.RESOURCE_POOL_CREATE_PARAMETER_SCHEMA, params)
    assert (errors == []) is accepted, errors
