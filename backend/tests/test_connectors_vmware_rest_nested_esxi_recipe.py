# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatcher-path wire pins for the nested-hypervisor VM recipe (#3087).

The governed "create a nested-hypervisor-ready VM" recipe
(``docs/codebase/connectors-vmware-rest-nested-esxi-recipe.md``) rides two
dispatch shapes: the ``vmware.composite.vm.create`` composite for the base
VM, then **raw ingested ops** for the per-device add-ons (data disks,
CD-ROM + ISO) and the VI-JSON VHV reconfigure. This module pins what each
of those calls puts on the wire, end to end through the production
seams — :func:`~meho_backplane.operations._branches.dispatch_ingested`
(param-loc split → path substitution → ``mount_op_path`` → verb-correct
transport) against the real :class:`VmwareRestConnector` with respx
intercepting at the httpx transport layer:

* the request **body is the flat ``*Spec``** — never the legacy
  ``/rest``-style ``{"spec": {...}}`` envelope (#2973 class; the recipe's
  ops were swept for the same trap #3071 found on the deploy path);
* the ``{vm}`` / ``{moId}`` params ride the **path**, everything else the
  body — the ``x-meho-param-loc`` contract of the real ingest parser
  (descriptors here are produced by :func:`parse_openapi` from an inline
  spec subset mirroring the pinned shapes; the pinned-spec side of the
  mirror is held by
  ``tests/test_connectors_vmware_rest_nested_esxi_spec_reconcile.py``);
* the recipe's example params **validate** against the parsed schemas the
  dispatcher enforces (``validate_params``);
* the raw VI-JSON dispatch mounts ``ReconfigVM_Task`` on the **``/api``
  prefix** — the observed 9.x accommodation (#2466), pinned deliberately:
  raw ingested dispatch has no release-versioned ``/sdk/vim25`` mounting
  (that lives only in the typed ``_post_vmomi_json`` helper), which is a
  documented caveat of the recipe's VHV step (404s on vCenter 8.0.x);
* ``vmware.composite.vm.create`` passes an ESXi ``VMKERNEL_*`` guest-OS
  identifier through to the create body **verbatim**.

Governance is deliberately out of frame: policy / approval / audit
wrapping is op-agnostic and proven in
``tests/test_connectors_vmware_rest_composites_write_e2e.py`` and the
acceptance dispatch lanes; these pins isolate the wire contract.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._write import vm_create_composite
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._branches import dispatch_ingested
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Inline spec subsets (shapes mirror the pinned vcenter-9.0 shelf specs)
# ---------------------------------------------------------------------------

# Request-body property sets below are byte-for-byte the pinned spec's
# top-level ``*Spec`` fields; the sibling reconcile module pins the pinned
# spec to the same literals, so a drift on either side fails one of the two
# modules. Only the sub-fields the recipe exercises carry nested schemas.

_JSON_BODY = "application/json"


def _op(
    *,
    operation_id: str,
    body_schema: dict[str, Any],
    path_params: tuple[str, ...] = ("vm",),
) -> dict[str, Any]:
    """One OpenAPI operation object with a JSON body + string response."""
    return {
        "operationId": operation_id,
        "parameters": [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in path_params
        ],
        "requestBody": {
            "required": True,
            "content": {_JSON_BODY: {"schema": body_schema}},
        },
        "responses": {
            "200": {
                "description": "ok",
                "content": {_JSON_BODY: {"schema": {"type": "string"}}},
            }
        },
    }


_DISK_CREATE_SPEC: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "backing": {"type": "object"},
        "new_vmdk": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "capacity": {"type": "integer", "format": "int64"},
                "storage_policy": {"type": "object"},
            },
        },
        "scsi": {"type": "object"},
        "ide": {"type": "object"},
        "sata": {"type": "object"},
        "nvme": {"type": "object"},
    },
}

_CPU_UPDATE_SPEC: dict[str, Any] = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "cores_per_socket": {"type": "integer"},
        "hot_add_enabled": {"type": "boolean"},
        "hot_remove_enabled": {"type": "boolean"},
    },
}

_CDROM_CREATE_SPEC: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "ide": {"type": "object"},
        "sata": {"type": "object"},
        "backing": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string"},
                "iso_file": {"type": "string"},
                "host_device": {"type": "string"},
                "device_access_type": {"type": "string"},
            },
        },
        "start_connected": {"type": "boolean"},
        "allow_guest_control": {"type": "boolean"},
    },
}

_INLINE_VCENTER_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "vcenter nested-ESXi recipe subset", "version": "9.0"},
    "paths": {
        "/vcenter/vm/{vm}/hardware/disk": {
            "post": _op(operation_id="VM_Hardware_Disk_Create", body_schema=_DISK_CREATE_SPEC)
        },
        "/vcenter/vm/{vm}/hardware/cpu": {
            "patch": _op(operation_id="VM_Hardware_Cpu_Update", body_schema=_CPU_UPDATE_SPEC)
        },
        "/vcenter/vm/{vm}/hardware/cdrom": {
            "post": _op(operation_id="VM_Hardware_Cdrom_Create", body_schema=_CDROM_CREATE_SPEC)
        },
    },
}

# vi-json shape: ``ReconfigVMRequestType`` keys its one required parameter
# ``spec`` — a genuine vim field (the request type), not the #2973 envelope.
_INLINE_VI_JSON_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "vi-json nested-ESXi recipe subset", "version": "9.0"},
    "paths": {
        "/VirtualMachine/{moId}/ReconfigVM_Task": {
            "post": _op(
                operation_id="VirtualMachine_ReconfigVM_Task",
                path_params=("moId",),
                body_schema={
                    "type": "object",
                    "required": ["spec"],
                    "properties": {
                        "spec": {
                            "type": "object",
                            "properties": {"nestedHVEnabled": {"type": "boolean"}},
                        }
                    },
                },
            )
        }
    },
}


@functools.cache
def _descriptors() -> dict[str, EndpointDescriptor]:
    """Parse both inline specs through the real ingest parser, index by op_id.

    ``parse_openapi`` with inline ``content=`` (the ``file://`` URI is the
    audit label only) emits the same ``x-meho-param-loc``-tagged
    ``parameter_schema`` an operator's real ingest produces, so the
    dispatch pins below exercise the genuine descriptor contract — path
    params tagged ``path``, the requestBody folded under the single
    ``body`` param.
    """
    rows = parse_openapi(
        "file:///inline/vcenter-nested-esxi.json",
        spec_source="spec:vcenter.yaml",
        content=json.dumps(_INLINE_VCENTER_SPEC),
    ) + parse_openapi(
        "file:///inline/vi-json-nested-esxi.json",
        spec_source="spec:vi-json.yaml",
        content=json.dumps(_INLINE_VI_JSON_SPEC),
    )
    now = datetime.now(UTC)
    descriptors: dict[str, EndpointDescriptor] = {}
    for row in rows:
        descriptors[row.op_id] = EndpointDescriptor(
            id=uuid4(),
            tenant_id=None,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id=row.op_id,
            source_kind="ingested",
            method=row.method,
            path=row.path,
            handler_ref=None,
            summary=row.summary,
            description=row.description,
            tags=list(row.tags),
            parameter_schema=row.parameter_schema,
            response_schema=row.response_schema,
            llm_instructions=None,
            safety_level="dangerous",
            requires_approval=True,
            is_enabled=True,
            embedding=None,
            custom_description=None,
            custom_notes=None,
            created_at=now,
            updated_at=now,
        )
    return descriptors


# The recipe's raw-op example calls (single source for the wire pins and the
# schema-validation sweep). Bodies are the flat ``*Spec`` shapes the recipe
# documents — 128 GiB thin-follows-datastore data disk, nested-host sizing,
# SATA CD-ROM with a datastore-ISO backing, VHV via ``nestedHVEnabled``.
_RECIPE_CALLS: dict[str, dict[str, Any]] = {
    "POST:/vcenter/vm/{vm}/hardware/disk": {
        "vm": "vm-42",
        "body": {"type": "SCSI", "new_vmdk": {"capacity": 137438953472}},
    },
    "PATCH:/vcenter/vm/{vm}/hardware/cpu": {
        "vm": "vm-42",
        "body": {"count": 8, "cores_per_socket": 2},
    },
    "POST:/vcenter/vm/{vm}/hardware/cdrom": {
        "vm": "vm-42",
        "body": {
            "type": "SATA",
            "start_connected": True,
            "allow_guest_control": False,
            "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/esxi-9.iso"},
        },
    },
    "POST:/VirtualMachine/{moId}/ReconfigVM_Task": {
        "moId": "vm-42",
        "body": {"spec": {"nestedHVEnabled": True}},
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin the chassis env vars ``Settings`` reads at construction time."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    """Structural :class:`VsphereTargetLike` stand-in (same shape as the auth tests)."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="vcenter-nested",
    host="vcenter-nested.test.invalid",
    port=443,
    secret_ref="vsphere/vcenter-nested",
)

_BASE_URL = "https://vcenter-nested.test.invalid"


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    """Canned session credentials — keeps the unit lane Vault-free."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_operator() -> Operator:
    return Operator(
        sub="op-nested-esxi-recipe",
        name="Nested ESXi Recipe Pins",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_raw(
    op_id: str,
    *,
    connector: VmwareRestConnector,
    params: dict[str, Any] | None = None,
) -> Any:
    """Dispatch one recipe op through the production ingested branch."""
    return await dispatch_ingested(
        connector=connector,
        descriptor=_descriptors()[op_id],
        operator=_make_operator(),
        target=_TARGET,
        params=params if params is not None else dict(_RECIPE_CALLS[op_id]),
    )


# ---------------------------------------------------------------------------
# Raw ingested wire pins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_add_posts_flat_create_spec_on_the_api_mount() -> None:
    """Data-disk add: POST ``/api/vcenter/vm/{vm}/hardware/disk`` with a flat body.

    ``vm`` rides the path; the body is the ``Disk.CreateSpec`` at the top
    level (``type`` + ``new_vmdk.capacity`` in bytes) — no ``{"spec": ...}``
    envelope. The 200 body is the new disk id (bare JSON string).
    """
    connector = VmwareRestConnector(session_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok-nested")
        route = mock.post("/api/vcenter/vm/vm-42/hardware/disk").respond(200, json="3001")

        payload = await _dispatch_raw("POST:/vcenter/vm/{vm}/hardware/disk", connector=connector)

    assert payload == "3001"
    request = route.calls.last.request
    assert request.url.path == "/api/vcenter/vm/vm-42/hardware/disk"
    assert json.loads(request.content) == {
        "type": "SCSI",
        "new_vmdk": {"capacity": 137438953472},
    }


@pytest.mark.asyncio
async def test_cpu_patch_sends_flat_update_spec_and_tolerates_204() -> None:
    """CPU sizing: PATCH ``/api/.../hardware/cpu`` flat body; 204 returns ``{}``.

    The recipe's CPU step is sizing only — VHV is **not** expressible here
    (the reconcile module pins that gap); it rides the VI-JSON reconfigure
    below. vCenter acknowledges the PATCH with ``204 No Content``, which
    the transport maps to ``{}`` (#3082) instead of failing the parse after
    the write took effect.
    """
    connector = VmwareRestConnector(session_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok-nested")
        route = mock.patch("/api/vcenter/vm/vm-42/hardware/cpu").respond(204)

        payload = await _dispatch_raw("PATCH:/vcenter/vm/{vm}/hardware/cpu", connector=connector)

    assert payload == {}
    request = route.calls.last.request
    assert request.method == "PATCH"
    assert json.loads(request.content) == {"count": 8, "cores_per_socket": 2}


@pytest.mark.asyncio
async def test_cdrom_add_posts_flat_create_spec_with_iso_backing() -> None:
    """Installer media: POST ``/api/.../hardware/cdrom`` with an ISO_FILE backing.

    The ADD leg the ``vm.device.cdrom`` composite deliberately does not
    cover (its manifest is update / remove / disconnect — pinned in the
    reconcile module): a new SATA CD-ROM backed by a datastore ISO path,
    connected at power-on.
    """
    connector = VmwareRestConnector(session_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok-nested")
        route = mock.post("/api/vcenter/vm/vm-42/hardware/cdrom").respond(200, json="16002")

        payload = await _dispatch_raw("POST:/vcenter/vm/{vm}/hardware/cdrom", connector=connector)

    assert payload == "16002"
    assert json.loads(route.calls.last.request.content) == {
        "type": "SATA",
        "start_connected": True,
        "allow_guest_control": False,
        "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/esxi-9.iso"},
    }


@pytest.mark.asyncio
async def test_vhv_reconfigure_dispatches_vi_json_op_on_the_api_mount() -> None:
    """VHV: raw ``ReconfigVM_Task`` dispatch rides the ``/api``-mounted vmomi form.

    Two recipe facts pinned at once:

    * the body is ``{"spec": {"nestedHVEnabled": true}}`` — ``spec`` here is
      the vim request type's genuine required parameter (reconcile module),
      not a #2973 envelope — and the 200 body is the Task MoRef to poll;
    * raw ingested dispatch mounts the vmomi path at ``/api/VirtualMachine/...``
      via ``mount_op_path``. That is the observed 9.x accommodation (#2466);
      the release-versioned ``/sdk/vim25/{release}`` base is applied only by
      the typed ``_post_vmomi_json`` composite helper, so the recipe's raw
      VHV step documents "9.x fleet only — 404s on vCenter 8.0.x" as a
      named caveat. If this pin ever fails because ``mount_op_path`` learned
      vmomi-aware mounting, upgrade the recipe caveat away.
    """
    connector = VmwareRestConnector(session_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok-nested")
        route = mock.post("/api/VirtualMachine/vm-42/ReconfigVM_Task").respond(
            200, json={"type": "Task", "value": "task-1701"}
        )

        payload = await _dispatch_raw(
            "POST:/VirtualMachine/{moId}/ReconfigVM_Task", connector=connector
        )

    assert payload == {"type": "Task", "value": "task-1701"}
    request = route.calls.last.request
    assert request.url.path == "/api/VirtualMachine/vm-42/ReconfigVM_Task"
    assert json.loads(request.content) == {"spec": {"nestedHVEnabled": True}}


def test_recipe_params_validate_against_the_ingested_schemas() -> None:
    """Every documented example call passes the dispatcher's schema validation.

    ``validate_params`` is the exact gate ``dispatch`` runs before the
    ingested branch executes; a recipe example that fails it would park as
    ``invalid_params`` for every operator who copies it.
    """
    for op_id, params in _RECIPE_CALLS.items():
        errors = validate_params(_descriptors()[op_id].parameter_schema, params)
        assert errors == [], f"{op_id}: recipe example params fail dispatch validation: {errors}"


# ---------------------------------------------------------------------------
# vm.create composite — ESXi guest-OS pass-through
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Minimal recording double for the composite's direct-session seam.

    The full recording connector lives in
    ``tests/test_connectors_vmware_rest_composites_write.py``; this subset
    carries only the methods ``vm_create_composite`` touches. The
    ``_about_version`` read serves ``None`` (unresolved), which keeps the
    composite on the REST create arm this module pins (#3099).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def _about_version(self, target: Any, operator: Operator) -> str | None:
        del target, operator
        return None

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        # Modern-mount flavor: strip the legacy ``filter.`` prefix (#2298).
        if not query:
            return None
        return {key.removeprefix("filter."): value for key, value in query.items()}

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": "GET", "path": path, "query": params, "body": None})
        return self._responses[path]

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> Any:
        self.calls.append({"method": verb, "path": path, "query": None, "body": json})
        return self._responses[path]


async def _auto_execute_gate(**_kwargs: Any) -> None:
    """Stand-in for ``enforce_subop_policy`` — always clears the gate.

    The governance seam itself is op-agnostic and proven in the composite
    write/e2e suites; this pin isolates the create body.
    """
    return None


@pytest.mark.asyncio
async def test_vm_create_composite_passes_vmkernel_guest_os_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vmware.composite.vm.create`` sends an ESXi guest identifier untouched.

    ``guest_os`` has no machine enum anywhere on the path — the composite's
    parameter schema takes any non-empty string and the pinned spec's enum
    is prose-only (reconcile module) — so ``VMKERNEL_8`` must reach the
    create body verbatim. The full body is pinned byte-for-byte; note the
    ``guest_OS`` / ``size_MiB`` field casing is the vSphere-6.7-8.x
    documented wire form, while the pinned 9.0 spec keys them ``guest_os``
    / ``size_mib`` — the recorded field-casing gap
    (``docs/codebase/connectors-vmware-rest.md``), carried by the recipe as
    a named risk pending a live-9.x verify. If that gap is ever closed,
    update this pin alongside the composite.
    """
    monkeypatch.setattr(_write, "enforce_subop_policy", _auto_execute_gate)
    session = _RecordingSession(
        {
            "/api/vcenter/folder": [{"folder": "folder-7", "name": "nested-lab"}],
            "/api/vcenter/vm": {"value": "vm-42"},
        }
    )

    out = await vm_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "folder_name": "nested-lab",
            "name": "esx-nested-01",
            "guest_os": "VMKERNEL_8",
            "cpu_count": 8,
            "memory_mib": 16384,
        },
        connector=session,  # type: ignore[arg-type]
    )

    assert isinstance(out, dict)
    assert out["status"] == "created"
    assert out["vm_id"] == "vm-42"
    create_calls = [c for c in session.calls if c["path"] == "/api/vcenter/vm"]
    assert len(create_calls) == 1
    assert create_calls[0]["body"] == {
        "name": "esx-nested-01",
        "guest_OS": "VMKERNEL_8",  # verbatim pass-through; casing gap noted above
        "placement": {"folder": "folder-7"},
        "cpu": {"count": 8},
        "memory": {"size_MiB": 16384},
    }
