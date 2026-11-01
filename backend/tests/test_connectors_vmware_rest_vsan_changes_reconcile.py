# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the vSAN changes against the pinned vendor files.

* An always-on test pins the child-op lists of the five changes.
* vCenter 9.0 (``vcenter-9.0/vi-json.yaml``): every vSAN method and every VIM
  method the changes call is a POST path; every data type they send exists,
  with every field they set; the request bodies use the vendor's argument
  names; the fixed values they send are vendor enum values.
* vCenter 8.0 U1 and U2 (``vcenter-8.0/vsan/<update>/vsanmgmtObjects.py`` of
  the vSAN Management SDK): the reconfigure spec sends its fields in the
  vendor field order, which the SOAP call of vCenter 8.0 checks. The vSAN
  methods themselves are checked against the SDK method lists by
  ``test_connectors_vmware_rest_8_0_spec_reconcile.py``, and the VIM methods
  (``QueryDisksForVsan`` on its 8.0 path, ``RetrieveDiskPartitionInfo``,
  ``UpdateDiskPartitions``) by the source scan of the same file.

The shelf tests skip when the spec shelf is not configured and run in CI.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_cluster,
    _vsan_disk_claim,
    _vsan_disk_wipe,
)
from tests._spec_shelf import require_shelf_spec

_VSAN_8_0_OBJECTS: dict[str, str] = {
    "8.0u1": "vsan/8.0u1/vsanmgmtObjects.py",
    "8.0u2": "vsan/8.0u2/vsanmgmtObjects.py",
}

#: Each data type the changes send -> the fields they set on it.
_SENT_TYPES: dict[str, set[str]] = {
    "VimVsanReconfigSpec": set(_vsan_cluster._RECONFIG_SPEC_ORDER),
    "VsanClusterConfigInfo": {"enabled", "vsanEsaEnabled"},
    "VsanDataEfficiencyConfig": {"dedupEnabled", "compressionEnabled"},
    "VsanPerfsvcConfig": {"enabled"},
    "VsanDataEncryptionConfig": {"encryptionEnabled", "kmsProviderId", "eraseDisksBeforeUse"},
    "KeyProviderId": {"id"},
    "VsanDataInTransitEncryptionConfig": {"enabled", "rekeyInterval"},
    "VsanAddStoragePoolDiskSpec": {"host", "disks"},
    "VsanStoragePoolDisk": {"diskName", "diskType"},
    "VimVsanHostDiskMappingCreationSpec": {
        "host",
        "cacheDisks",
        "capacityDisks",
        "creationType",
    },
    "VsanClusterReconfigRequestType": {"cluster", "vsanReconfigSpec"},
    "VsanEncryptedClusterRekeyRequestType": {
        "encryptedCluster",
        "deepRekey",
        "allowReducedRedundancy",
    },
    "VsanAddStoragePoolDiskRequestType": {"specs"},
    "InitializeDiskMappingsRequestType": {"spec"},
    "VsanHostWipeDiskRequestType": {"disks"},
    "VsanHostQueryWipeDiskRequestType": {"disks"},
    "QueryDisksForVsanRequestType": {"canonicalName"},
    "RetrieveDiskPartitionInfoRequestType": {"devicePath"},
    "UpdateDiskPartitionsRequestType": {"devicePath", "spec"},
    # Read from the answers.
    "VsanHostDiskResult": {"disk", "state", "vsanUuid", "error"},
    "VsanHostWipeDiskStatus": {"disk", "eligible", "wipeState", "percentageCompleted"},
}

#: Enum schema -> the values the changes send or read.
_ENUM_VALUES: dict[str, set[str]] = {
    "StoragePoolDiskType_enum": {"singleTier"},
    "VimVsanHostDiskMappingCreationType_enum": {"allFlash", "hybrid"},
    "VsanHostDiskResultState_enum": {"inUse", "eligible", "ineligible"},
    "VsanHostWipeDiskEligible_enum": {"Yes", "No"},
}


def test_the_child_op_lists_are_the_expected_sets() -> None:
    assert _vsan_cluster._VSAN_SUB_OPS_CLUSTER_CONFIGURE == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/vsan/VsanVcClusterConfigSystem/{moId}/VsanClusterReconfig",
    )
    assert _vsan_cluster._VSAN_SUB_OPS_ENCRYPTION_REKEY == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/vsan/VsanVcClusterConfigSystem/{moId}/VsanEncryptedClusterRekey_Task",
    )
    assert _vsan_disk_claim._VSAN_SUB_OPS_DISK_CLAIM == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/vsan/HostVsanSystem/{moId}/QueryDisksForVsan",
        "POST:/vsan/VimClusterVsanVcDiskManagementSystem/{moId}/VsanAddStoragePoolDisk",
        "POST:/vsan/VimClusterVsanVcDiskManagementSystem/{moId}/InitializeDiskMappings",
    )
    assert _vsan_disk_wipe._VSAN_SUB_OPS_DISK_WIPE == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/vsan/HostVsanSystem/{moId}/QueryDisksForVsan",
        "POST:/HostStorageSystem/{moId}/RetrieveDiskPartitionInfo",
        "POST:/vsan/VsanSystemEx/{moId}/VsanHostQueryWipeDisk",
        "POST:/HostStorageSystem/{moId}/UpdateDiskPartitions",
        "POST:/vsan/VsanSystemEx/{moId}/VsanHostWipeDisk",
    )


# ---------------------------------------------------------------------------
# vCenter 9.0 VI-JSON file
# ---------------------------------------------------------------------------


def _vi_json_lines() -> list[str]:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    return spec_path.read_text(encoding="utf-8").splitlines()


def _post_paths(lines: list[str]) -> set[str]:
    """``POST:<path>`` of every path item with a POST (2-space path keys)."""
    found: set[str] = set()
    in_paths = False
    current: str | None = None
    for line in lines:
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_paths = line.rstrip() == "paths:"
            current = None
        elif in_paths and line.startswith("  ") and not line.startswith("   "):
            key = line.strip()
            current = key[:-1] if key.startswith("/") and key.endswith(":") else None
        elif current is not None and line.strip() == "post:":
            found.add(f"POST:{current}")
    return found


def _schema_block(lines: list[str], schema: str) -> list[str]:
    """The lines of one component schema (4-space key) up to the next one."""
    start = lines.index(f"    {schema}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("    ") and not line.startswith("     "):
            break
        block.append(line)
    return block


def _properties(lines: list[str], schema: str) -> set[str]:
    """The property names of one schema (8-space keys under ``properties:``)."""
    names: set[str] = set()
    inside = False
    for line in _schema_block(lines, schema):
        if line == "      properties:":
            inside = True
            continue
        if inside and line.startswith("      ") and not line.startswith("       "):
            inside = False
        if inside and re.fullmatch(r" {8}[A-Za-z_][A-Za-z0-9_]*:", line):
            names.add(line.strip()[:-1])
    return names


def _enum(lines: list[str], schema: str) -> set[str]:
    values: set[str] = set()
    inside = False
    for line in _schema_block(lines, schema):
        stripped = line.strip()
        if stripped == "enum:":
            inside = True
        elif inside and stripped.startswith("- "):
            values.add(stripped[2:].strip().strip("'\""))
        elif inside:
            inside = False
    return values


def test_every_called_method_is_a_post_path_of_the_9_0_file() -> None:
    served = _post_paths(_vi_json_lines())
    assert served, "vi-json.yaml parsed to zero POST paths"
    called = (
        set(_vsan_cluster._VSAN_SUB_OPS_CLUSTER_CONFIGURE)
        | set(_vsan_cluster._VSAN_SUB_OPS_ENCRYPTION_REKEY)
        | set(_vsan_disk_claim._VSAN_SUB_OPS_DISK_CLAIM)
        | set(_vsan_disk_wipe._VSAN_SUB_OPS_DISK_WIPE)
    )
    missing = sorted(called - served)
    assert not missing, f"the pinned 9.0 vi-json.yaml has no POST path for {missing}"
    # QueryDisksForVsan has only the /vsan path from 9.0 on.
    assert "POST:/HostVsanSystem/{moId}/QueryDisksForVsan" not in served


@pytest.mark.parametrize("schema", sorted(_SENT_TYPES))
def test_every_sent_type_and_field_exists_in_the_9_0_file(schema: str) -> None:
    lines = _vi_json_lines()
    assert f"    {schema}:" in lines, f"vi-json.yaml has no schema {schema}"
    missing = sorted(_SENT_TYPES[schema] - _properties(lines, schema))
    assert not missing, f"{schema} has no field {missing} in the pinned 9.0 vi-json.yaml"


@pytest.mark.parametrize("schema", sorted(_ENUM_VALUES))
def test_fixed_values_are_vendor_enum_values(schema: str) -> None:
    values = _enum(_vi_json_lines(), schema)
    assert _ENUM_VALUES[schema] <= values, f"{schema}: {sorted(values)}"


# ---------------------------------------------------------------------------
# vCenter 8.0 vSAN Management SDK
# ---------------------------------------------------------------------------


def _data_type_fields(objects_file: Path, vmodl_name: str) -> list[str]:
    """The field names of one ``CreateDataType`` call, in the vendor order."""
    for node in ast.walk(ast.parse(objects_file.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CreateDataType"
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == vmodl_name
        ):
            fields = node.args[4]
            assert isinstance(fields, (ast.List, ast.Tuple))
            return [
                field.elts[0].value
                for field in fields.elts
                if isinstance(field, ast.Tuple) and isinstance(field.elts[0], ast.Constant)
            ]
    raise AssertionError(f"{objects_file.name} defines no {vmodl_name}")


@pytest.mark.parametrize("update", sorted(_VSAN_8_0_OBJECTS))
def test_reconfigure_spec_fields_follow_the_vendor_order(update: str) -> None:
    objects_file = require_shelf_spec("vcenter-8.0", _VSAN_8_0_OBJECTS[update])
    vendor = _data_type_fields(objects_file, "vim.vsan.ReconfigSpec")
    sent = list(_vsan_cluster._RECONFIG_SPEC_ORDER)
    assert all(name in vendor for name in sent), sorted(set(sent) - set(vendor))
    assert sent == sorted(sent, key=vendor.index)


@pytest.mark.parametrize("update", sorted(_VSAN_8_0_OBJECTS))
@pytest.mark.parametrize(
    ("vmodl_name", "sent"),
    [
        ("vim.vsan.DataEfficiencyConfig", ["dedupEnabled", "compressionEnabled"]),
        ("vim.cluster.VsanPerfsvcConfig", ["enabled"]),
        (
            "vim.vsan.DataEncryptionConfig",
            ["encryptionEnabled", "kmsProviderId", "eraseDisksBeforeUse"],
        ),
        ("vim.vsan.DataInTransitEncryptionConfig", ["enabled", "rekeyInterval"]),
        ("vim.vsan.host.AddStoragePoolDiskSpec", ["host", "disks"]),
        ("vim.vsan.host.StoragePoolDisk", ["diskName", "diskType"]),
        (
            "vim.vsan.host.DiskMappingCreationSpec",
            ["host", "cacheDisks", "capacityDisks", "creationType"],
        ),
    ],
)
def test_sent_vsan_types_follow_the_vendor_order(
    update: str, vmodl_name: str, sent: list[str]
) -> None:
    objects_file = require_shelf_spec("vcenter-8.0", _VSAN_8_0_OBJECTS[update])
    vendor = _data_type_fields(objects_file, vmodl_name)
    assert all(name in vendor for name in sent), sorted(set(sent) - set(vendor))
    assert sent == sorted(sent, key=vendor.index)
