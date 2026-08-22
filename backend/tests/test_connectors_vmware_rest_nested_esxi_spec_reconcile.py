# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pinned-spec reconcile lanes for the nested-hypervisor VM recipe (#3087).

The governed "create a nested-hypervisor-ready VM" recipe
(``docs/codebase/connectors-vmware-rest-nested-esxi-recipe.md``) names four
raw ingested ops plus one composite. This module grounds every spec-level
claim the recipe makes against the pinned ``vcenter-9.0`` shelf specs, per
the spec-reconcile standard (``docs/decisions/spec-reconcile-guards-standard.md``):

* The recipe's REST ops are **served** by the pinned ``vcenter.yaml``
  (path lane) with **flat** ``*Spec`` request bodies, never the legacy
  ``/rest``-style single-``spec`` wrapper (#2973 body lane).
* The recipe's **gap claims stay true**: the vSphere Automation REST
  surface cannot express nested hardware virtualization (VHV) — no
  ``hardware_virtualization`` / ``nestedHV`` field exists anywhere in the
  pinned ``vcenter.yaml`` — and ``Disk.VmdkCreateSpec`` carries no
  thin-provisioning knob. When a future re-pin adds either field these
  lanes go red, which is the signal to upgrade the recipe (retire the
  VI-JSON VHV step / add the provisioning param), not harness noise.
* The VI-JSON escape hatch the recipe uses for VHV is served: the pinned
  ``vi-json.yaml`` POSTs ``/VirtualMachine/{moId}/ReconfigVM_Task`` and its
  ``VirtualMachineConfigSpec`` declares ``nestedHVEnabled``.

Division of labour (mirrors the write-body lane, #2973): these lanes ground
the **spec-side contract** and skip uniformly when the shelf is
unprovisioned (#2980 harness contract). The **connector/dispatcher-side**
proof — that the raw dispatch path puts exactly these flat bodies on the
wire — is ``tests/test_connectors_vmware_rest_nested_esxi_recipe.py``,
which runs everywhere.
"""

from __future__ import annotations

import functools
from typing import Any

import yaml

from meho_backplane.connectors.vmware_rest.composites import _write
from tests._spec_shelf import (
    assert_op_ids_served,
    require_shelf_spec,
)
from tests.acceptance._vcenter_spec import resolve_vi_json_yaml

# ---------------------------------------------------------------------------
# The recipe's op manifest (single source of truth for these lanes)
# ---------------------------------------------------------------------------

#: Raw ingested REST ops the nested-ESXi recipe names. Frozen so a doc
#: rewrite that renames an op forces this lane (and the wire-pin module)
#: to be updated deliberately.
RECIPE_REST_OP_IDS: frozenset[str] = frozenset(
    {
        "POST:/vcenter/vm",
        "POST:/vcenter/vm/{vm}/hardware/disk",
        "PATCH:/vcenter/vm/{vm}/hardware/cpu",
        "POST:/vcenter/vm/{vm}/hardware/cdrom",
    }
)

#: The VI-JSON op the recipe uses for VHV (``nestedHVEnabled``) — the one
#: mutation the REST surface cannot express (see the gap lanes below).
RECIPE_VI_JSON_OP_ID = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"


# ---------------------------------------------------------------------------
# Always-on manifest tie-ins (no shelf required)
# ---------------------------------------------------------------------------


def test_recipe_ops_match_the_connectors_live_constants() -> None:
    """The recipe's op strings are byte-for-byte the connector's own constants.

    Ties the frozen recipe manifest to the live ``_write`` constants where
    the connector already declares the same endpoint, so the recipe cannot
    silently drift from the strings the composites gate on and the ingest
    parser emits.
    """
    assert _write._OP_CREATE_VM == "POST:/vcenter/vm"
    assert _write._OP_UPDATE_VM_CPU == "PATCH:/vcenter/vm/{vm}/hardware/cpu"
    assert _write._OP_RECONFIG_VM_TASK == RECIPE_VI_JSON_OP_ID
    assert {_write._OP_CREATE_VM, _write._OP_UPDATE_VM_CPU} < RECIPE_REST_OP_IDS


def test_cdrom_composite_covers_update_remove_disconnect_only() -> None:
    """``vm.device.cdrom`` has no ADD leg — the recipe's cdrom-add is a raw op.

    The recipe claims the existing composite covers update / remove /
    disconnect of an *existing* CD-ROM device and that attaching a new
    ISO-backed device rides the raw ingested
    ``POST:/vcenter/vm/{vm}/hardware/cdrom``. Pin the composite's sub-op
    manifest so a future ADD leg (which would obsolete the recipe's raw
    step) is a deliberate, visible change here.
    """
    assert set(_write._SUB_OPS_VM_DEVICE_CDROM) == {
        "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
        "PATCH:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
        "DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
        "POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect",
    }
    assert "POST:/vcenter/vm/{vm}/hardware/cdrom" not in set(_write._SUB_OPS_VM_DEVICE_CDROM)


# ---------------------------------------------------------------------------
# Shelf-backed helpers (parse once per session; skip uniformly without shelf)
# ---------------------------------------------------------------------------


@functools.cache
def _vcenter_descriptor_index() -> dict[str, Any]:
    """Parse the pinned ``vcenter.yaml`` once and index protos by op_id.

    Uses the real ingest parser so every schema fact asserted below is
    read off the exact ``endpoint_descriptor`` shape an operator's ingest
    of the same spec produces. Cached module-wide: the ~7.5 MB spec parse
    is paid once for all lanes in this module rather than per test.
    """
    from meho_backplane.operations.ingest import parse_openapi

    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_path.read_text(encoding="utf-8"),
    )
    return {row.op_id: row for row in rows}


@functools.cache
def _vcenter_spec_text() -> str:
    """Raw pinned ``vcenter.yaml`` text for whole-surface textual pins."""
    return require_shelf_spec("vcenter-9.0", "vcenter.yaml").read_text(encoding="utf-8")


def _body_schema(op_id: str) -> dict[str, Any]:
    """The parsed requestBody schema (``properties.body``) for *op_id*."""
    proto = _vcenter_descriptor_index().get(op_id)
    assert proto is not None, f"{op_id}: not served by the pinned vcenter.yaml"
    body = (proto.parameter_schema or {}).get("properties", {}).get("body")
    assert isinstance(body, dict), f"{op_id}: pinned spec serves no JSON request body"
    return body


def _component_schema(spec_text: str, schema_name: str) -> dict[str, Any]:
    """Extract one ``components.schemas`` entry from a pinned spec's raw text.

    The ingest parser inlines only the top-level requestBody schema; nested
    ``allOf: [$ref: ...]`` sub-specs (``VmdkCreateSpec``,
    ``Cdrom.BackingSpec``, ``VirtualMachineConfigSpec``) stay refs in the
    descriptor. Rather than YAML-load an entire multi-MB spec, slice the
    schema's block by its stable 4-space ``components.schemas`` indentation
    (same line-scan approach as the vi-json path lane in
    ``test_connectors_vmware_rest_composites_l2_ingest_reconcile.py``) and
    YAML-load only that fragment.
    """
    lines = spec_text.splitlines()
    key = f"    {schema_name}:"
    try:
        start = lines.index(key)
    except ValueError as exc:
        raise AssertionError(f"{schema_name}: not found in the pinned spec") from exc
    block = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and (len(line) - len(line.lstrip(" "))) <= 4:
            break  # next components.schemas entry (or a higher-level key)
        block.append(line)
    fragment = "\n".join(row[4:] if len(row) >= 4 else row for row in block)
    loaded = yaml.safe_load(fragment)
    assert isinstance(loaded, dict) and schema_name in loaded
    schema = loaded[schema_name]
    assert isinstance(schema, dict)
    return schema


# ---------------------------------------------------------------------------
# vcenter.yaml lanes — the recipe's REST surface
# ---------------------------------------------------------------------------


def test_recipe_rest_ops_are_served_by_the_pinned_spec() -> None:
    """Path lane: every REST op the recipe names exists in vcenter-9.0."""
    served = set(_vcenter_descriptor_index())
    assert_op_ids_served(
        RECIPE_REST_OP_IDS, served, spec_label="vcenter-9.0/vcenter.yaml (nested-ESXi recipe)"
    )


def test_disk_add_body_is_the_flat_disk_create_spec() -> None:
    """``POST .../hardware/disk`` takes ``Disk.CreateSpec`` fields at the top level.

    The recipe's data-disk step sends
    ``{"type": "SCSI", "new_vmdk": {"capacity": <bytes>}}`` — the flat
    ``/api`` shape, never a ``{"spec": {...}}`` envelope (#2973 class).
    """
    props = set(_body_schema("POST:/vcenter/vm/{vm}/hardware/disk").get("properties", {}))
    assert props != {"spec"}, "disk-add regressed to a /rest-style single-spec wrapper"
    assert {"type", "backing", "new_vmdk", "scsi", "ide", "sata", "nvme"} == props


def test_vmdk_create_spec_has_no_provisioning_knob() -> None:
    """Gap pin: thin/thick provisioning is not expressible on the REST disk add.

    ``Disk.VmdkCreateSpec`` is exactly ``{name, capacity, storage_policy}``
    — provisioning format follows the target datastore's default (or the
    referenced storage policy). The recipe documents this as a named gap;
    if a re-pinned spec ever grows a provisioning field here, this lane
    fails and the recipe's disk step gains the explicit knob.
    """
    schema = _component_schema(_vcenter_spec_text(), "Vcenter.Vm.Hardware.Disk.VmdkCreateSpec")
    assert set(schema.get("properties", {})) == {"name", "capacity", "storage_policy"}
    capacity = schema["properties"]["capacity"]
    assert capacity.get("type") == "integer"
    assert capacity.get("format") == "int64"  # bytes, not MiB/GiB


def test_cdrom_add_body_is_the_flat_cdrom_create_spec() -> None:
    """``POST .../hardware/cdrom`` takes ``Cdrom.CreateSpec`` fields at the top level."""
    props = set(_body_schema("POST:/vcenter/vm/{vm}/hardware/cdrom").get("properties", {}))
    assert props != {"spec"}, "cdrom-add regressed to a /rest-style single-spec wrapper"
    assert {"type", "ide", "sata", "backing", "start_connected", "allow_guest_control"} == props


def test_cdrom_backing_spec_serves_iso_file_backing() -> None:
    """``Cdrom.BackingSpec`` declares the ISO backing the recipe mounts.

    The recipe's installer step sends
    ``{"backing": {"type": "ISO_FILE", "iso_file": "[datastore] path.iso"}}``;
    both the ``iso_file`` property and the ``ISO_FILE`` backing type must be
    served by the pinned spec (the type enum is prose-documented, so the
    membership check reads the description).
    """
    schema = _component_schema(_vcenter_spec_text(), "Vcenter.Vm.Hardware.Cdrom.BackingSpec")
    props = schema.get("properties", {})
    assert "iso_file" in props
    assert schema.get("required") == ["type"]
    assert "ISO_FILE" in str(props.get("type", {}).get("description", ""))


def test_cpu_update_spec_cannot_express_hardware_virtualization() -> None:
    """Gap pin: VHV is absent from ``PATCH .../hardware/cpu`` — and the whole REST spec.

    Issue #3087's premise named ``hardware_virtualization: true`` on the CPU
    PATCH as the VHV step; the pinned 9.0 ``vcenter.yaml`` serves no such
    field — ``Cpu.UpdateSpec`` is exactly
    ``{count, cores_per_socket, hot_add_enabled, hot_remove_enabled}`` and
    no ``hardware_virtualization`` / ``nestedHV`` spelling appears anywhere
    on the REST surface. The recipe therefore routes VHV through the
    VI-JSON ``ReconfigVM_Task`` (lanes below). If this lane ever fails, a
    re-pinned spec has grown REST-native VHV: upgrade the recipe to the
    REST field and retire the VI-JSON step.
    """
    props = set(_body_schema("PATCH:/vcenter/vm/{vm}/hardware/cpu").get("properties", {}))
    assert props == {"count", "cores_per_socket", "hot_add_enabled", "hot_remove_enabled"}
    assert "hardware_virtualization" not in _vcenter_spec_text()
    assert "nestedHV" not in _vcenter_spec_text()


def test_vm_create_spec_guest_os_is_a_snake_case_free_string_with_esxi_values() -> None:
    """``POST:/vcenter/vm`` keys the guest identifier ``guest_os`` — prose-only enum.

    Three recipe-load-bearing facts about the pinned ``VM.CreateSpec``:

    * The field is **snake_case ``guest_os``** and it is the spec's only
      required property. The ``vm.create`` composite currently emits the
      vSphere-6.7-8.x documented mixed-case ``guest_OS`` — the recorded
      field-casing gap (``docs/codebase/connectors-vmware-rest.md``), which
      the recipe carries as a named risk pending a live-9.x verify.
    * The schema declares **no machine-readable enum** — the guest-OS
      identifier passes through dispatch validation as a free string, so
      the ESXi ``VMKERNEL_*`` identifiers reach the wire verbatim.
    * The ESXi identifiers are **documented values**: the prose enum names
      ``VMKERNEL_8`` (and the 9.x sibling), grounding "an ESXi guest is a
      legitimate ``guest_os``" in the pinned spec rather than vendor lore.
    """
    body = _body_schema("POST:/vcenter/vm")
    props = body.get("properties", {})
    assert "guest_os" in props
    assert "guest_OS" not in props
    assert body.get("required") == ["guest_os"]
    guest_os = props["guest_os"]
    assert guest_os.get("type") == "string"
    assert "enum" not in guest_os
    description = str(guest_os.get("description", ""))
    assert "VMKERNEL_8" in description
    assert "VMKERNEL_9" in description
    # The CreateSpec can also carry the recipe's device payloads inline
    # (raw single-call alternative documented in the recipe).
    assert {"disks", "cdroms", "cpu", "memory", "nics", "boot", "boot_devices"} <= set(props)


# ---------------------------------------------------------------------------
# vi-json.yaml lanes — the VHV escape hatch
# ---------------------------------------------------------------------------


def _vi_json_spec_text() -> str:
    """Pinned ``vi-json.yaml`` text, or skip with the uniform shelf reason."""
    import pytest

    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(
            "vcenter-9.0/vi-json.yaml spec not configured. Set MEHO_CONSUMER_DOCS_ROOT "
            "to a spec-shelf docs directory containing vcenter-9.0/vi-json.yaml."
        )
    return spec_path.read_text(encoding="utf-8")


def test_vi_json_serves_reconfig_vm_task_post() -> None:
    """The pinned vi-json.yaml serves ``POST /VirtualMachine/{moId}/ReconfigVM_Task``.

    Same line-scan as the disk-grow lane in the L2 reconcile module: the
    path item is keyed at 2-space indent with its operations at 4-space
    indent — cheap and precise on the ~10 MB spec.
    """
    lines = _vi_json_spec_text().splitlines()
    _, _, path = RECIPE_VI_JSON_OP_ID.partition(":")
    try:
        start = lines.index(f"  {path}:")
    except ValueError:
        raise AssertionError(f"{path}: not a path item in the pinned vi-json.yaml") from None
    has_post = False
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("   "):
            break
        if line.strip() == "post:":
            has_post = True
            break
    assert has_post, f"{path}: served without a POST operation"


def test_vi_json_config_spec_declares_nested_hv_enabled() -> None:
    """``VirtualMachineConfigSpec.nestedHVEnabled`` — the recipe's VHV field — is served.

    The recipe's VHV step POSTs ``{"spec": {"nestedHVEnabled": true}}`` to
    ``ReconfigVM_Task``; this grounds both halves in the pinned spec: the
    request type keys its one required parameter ``spec`` (a genuine vim
    field, not the #2973 envelope), and the referenced
    ``VirtualMachineConfigSpec`` declares the boolean.
    """
    spec_text = _vi_json_spec_text()
    request_type = _component_schema(spec_text, "ReconfigVMRequestType")
    assert set(request_type.get("properties", {})) == {"spec"}
    assert request_type.get("required") == ["spec"]

    config_spec = _component_schema(spec_text, "VirtualMachineConfigSpec")
    nested = config_spec.get("properties", {}).get("nestedHVEnabled")
    assert isinstance(nested, dict), "VirtualMachineConfigSpec lost nestedHVEnabled"
    assert nested.get("type") == "boolean"
