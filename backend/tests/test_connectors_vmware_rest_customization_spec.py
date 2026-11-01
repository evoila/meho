# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the guest customization spec reads and the governed delete.

* ``vmware.guest.customization_spec.list`` / ``.get`` run on the real
  :class:`VmwareRestConnector` over mocked vCenter HTTP (respx), on the
  vCenter 8.0 and 9.0 REST mount. Saved answers are made up by hand from the
  vendor API reference; no live vCenter. The Windows spec holds a password,
  a product key, a domain password and sysprep run-once commands: none of
  them may reach the answer.
* ``vmware.composite.guest.customization_spec.delete`` runs against a REST
  fake: the plan, the approval preview (blast radius, and a refusal for a
  spec that does not exist), the delete and its read-back, and the governed
  flow (preview first, a second person approves, an agent is refused).
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import _customization_spec_delete as delete
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    _WHEN_TO_USE_BY_GROUP,
    COMPOSITES_NOT_ON_8_0,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.customization_specs import (
    GUEST_WHEN_TO_USE,
    hide_secrets,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_customization_spec import (
    VMWARE_CUSTOMIZATION_SPEC_GET_OP,
    VMWARE_CUSTOMIZATION_SPEC_LIST_OP,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from meho_backplane.settings import get_settings

_BASE = "https://vc.example.test"
_SPECS = "/api/vcenter/guest/customization-specs"
_WIN = "<windows-spec>"
_LINUX = "<linux spec/1>"

# Made-up secret values. The tests check that none of them reaches an answer.
_ADMIN_PASSWORD = "<admin-password-value>"
_DOMAIN_PASSWORD = "<domain-password-value>"
_PRODUCT_KEY = "<product-key-value>"
_RUN_ONCE = "<run-once-command-with-a-secret>"

_SUMMARIES = [
    {
        "name": _WIN,
        "description": "Windows servers",
        "OS_type": "WINDOWS",
        "last_modified": "2026-09-01T10:00:00.000Z",
    },
    {
        "name": _LINUX,
        "description": "Linux web",
        "OS_type": "LINUX",
        "last_modified": "2026-09-02T10:00:00.000Z",
    },
]

_WIN_INFO: dict[str, Any] = {
    "last_modified": "2026-09-01T10:00:00.000Z",
    "spec": {
        "name": _WIN,
        "description": "Windows servers",
        "fingerprint": "<fingerprint>",
        "spec": {
            "configuration_spec": {
                "windows_config": {
                    "reboot": "REBOOT",
                    "sysprep": {
                        "gui_run_once_commands": [_RUN_ONCE],
                        "user_data": {
                            "computer_name": {"type": "VIRTUAL_MACHINE"},
                            "full_name": "<full-name>",
                            "organization": "<org>",
                            "product_key": _PRODUCT_KEY,
                        },
                        "domain": {
                            "type": "DOMAIN",
                            "domain": "<domain>",
                            "domain_username": "<join-user>",
                            "domain_password": _DOMAIN_PASSWORD,
                        },
                        "gui_unattended": {
                            "auto_logon": False,
                            "auto_logon_count": 1,
                            "password": _ADMIN_PASSWORD,
                            "time_zone": 85,
                        },
                    },
                }
            },
            "global_dns_settings": {"dns_servers": ["<dns-ip>"], "dns_suffix_list": []},
            "interfaces": [{"adapter": {"ipv4": {"type": "DHCP"}}}],
        },
    },
}

_LINUX_INFO: dict[str, Any] = {
    "last_modified": "2026-09-02T10:00:00.000Z",
    "spec": {
        "name": _LINUX,
        "description": "Linux web",
        "fingerprint": "<fingerprint>",
        "spec": {
            "configuration_spec": {
                "linux_config": {
                    "hostname": {"type": "VIRTUAL_MACHINE"},
                    "domain": "<domain>",
                    "script_text": "",
                }
            },
            "interfaces": [],
        },
    },
}


def _operator() -> Operator:
    return Operator(
        sub="op-customization-spec",
        name="Customization Spec Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c544"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<stub>"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None
    version: str | None = None


@pytest.fixture
async def connector() -> AsyncIterator[VmwareRestConnector]:
    conn = VmwareRestConnector(session_loader=_stub_loader)
    yield conn
    conn._session_tokens.clear()
    for client in conn._clients.values():
        await client.aclose()
    conn._clients.clear()


@pytest.fixture(params=["8.0.3", "9.0.0"])
def vc(request: pytest.FixtureRequest) -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(200, json="tok")
        mock.get("/api/about").respond(200, json={"version": request.param})
        mock.get(_SPECS, name="list").respond(200, json=_SUMMARIES)
        mock.get(f"{_SPECS}/%3Cwindows-spec%3E").respond(200, json=_WIN_INFO)
        mock.get(f"{_SPECS}/%3Clinux%20spec%2F1%3E").respond(200, json=_LINUX_INFO)
        mock.get(f"{_SPECS}/missing").respond(404, json={"error_type": "NOT_FOUND", "messages": []})
        yield mock


# ---------------------------------------------------------------------------
# Reads: registration and contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op", [VMWARE_CUSTOMIZATION_SPEC_LIST_OP, VMWARE_CUSTOMIZATION_SPEC_GET_OP]
)
def test_reads_are_safe_typed_ops_in_the_guest_group(op: Any) -> None:
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == "guest"
    for example in op.parameter_schema["examples"]:
        validate_params(op.parameter_schema, example)


def test_the_guest_group_has_one_text() -> None:
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP["guest"] == GUEST_WHEN_TO_USE
    assert _WHEN_TO_USE_BY_GROUP["guest"] == GUEST_WHEN_TO_USE
    for op_id in (
        "vmware.guest.customization_spec.list",
        "vmware.guest.customization_spec.get",
        "guest.customization_spec.delete",
    ):
        assert op_id in GUEST_WHEN_TO_USE


# ---------------------------------------------------------------------------
# Reads over mocked HTTP (vCenter 8.0.3 and 9.0)
# ---------------------------------------------------------------------------


async def test_list_returns_one_flat_row_per_spec(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    out = await connector.customization_spec_list(_operator(), _StubTarget(), {})
    assert out == {
        "specs": [
            {
                "name": _WIN,
                "description": "Windows servers",
                "os_type": "WINDOWS",
                "last_modified": "2026-09-01T10:00:00.000Z",
            },
            {
                "name": _LINUX,
                "description": "Linux web",
                "os_type": "LINUX",
                "last_modified": "2026-09-02T10:00:00.000Z",
            },
        ]
    }
    Draft202012Validator(VMWARE_CUSTOMIZATION_SPEC_LIST_OP.response_schema).validate(out)
    assert vc.routes["list"].calls.last.request.url.query == b""


@pytest.mark.parametrize(
    ("params", "names"),
    [
        ({"os_type": "LINUX"}, [_LINUX]),
        ({"name": "<WIN*"}, [_WIN]),
        ({"os_type": "WINDOWS", "name": "<linux*"}, []),
    ],
)
async def test_list_filters_in_meho(
    connector: VmwareRestConnector,
    vc: respx.MockRouter,
    params: dict[str, Any],
    names: list[str],
) -> None:
    out = await connector.customization_spec_list(_operator(), _StubTarget(), params)
    assert [row["name"] for row in out["specs"]] == names


async def test_list_accepts_the_lower_case_os_type_field(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.routes["list"].respond(200, json=[{"name": "a", "os_type": "LINUX"}])
    out = await connector.customization_spec_list(_operator(), _StubTarget(), {})
    assert out["specs"][0]["os_type"] == "LINUX"


async def test_get_hides_every_secret_of_a_windows_spec(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    out = await connector.customization_spec_get(_operator(), _StubTarget(), {"name": _WIN})
    text = json.dumps(out)
    for secret in (_ADMIN_PASSWORD, _DOMAIN_PASSWORD, _PRODUCT_KEY, _RUN_ONCE):
        assert secret not in text
    sysprep = out["customization"]["configuration_spec"]["windows_config"]["sysprep"]
    assert sysprep["gui_unattended"]["password"] == SECRET_FIELD_MARKER
    assert sysprep["gui_unattended"]["time_zone"] == 85
    assert sysprep["domain"]["domain_username"] == "<join-user>"
    assert sorted(out["hidden_fields"]) == sorted(
        [
            "configuration_spec.windows_config.sysprep.gui_run_once_commands",
            "configuration_spec.windows_config.sysprep.user_data.product_key",
            "configuration_spec.windows_config.sysprep.domain.domain_password",
            "configuration_spec.windows_config.sysprep.gui_unattended.password",
        ]
    )
    assert out["config_kind"] == "windows"
    assert out["fingerprint"] == "<fingerprint>"
    Draft202012Validator(VMWARE_CUSTOMIZATION_SPEC_GET_OP.response_schema).validate(out)


async def test_get_encodes_the_name_as_one_path_segment(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    out = await connector.customization_spec_get(_operator(), _StubTarget(), {"name": _LINUX})
    assert out["name"] == _LINUX
    assert out["config_kind"] == "linux"
    # An empty script is shown as empty: there is nothing to hide.
    assert out["customization"]["configuration_spec"]["linux_config"]["script_text"] == ""
    assert out["hidden_fields"] == []


async def test_get_of_a_missing_spec_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.customization_spec_get(_operator(), _StubTarget(), {"name": "missing"})


def test_hide_secrets_hides_cloud_init_user_data_and_keeps_flags() -> None:
    clean, hidden = hide_secrets(
        {
            "cloud_config": {
                "type": "CLOUDINIT",
                "cloudinit": {"metadata": "instance-id: x", "userdata": "#cloud-config ..."},
            },
            "password_set": True,
        }
    )
    assert clean["cloud_config"]["cloudinit"] == {
        "metadata": "instance-id: x",
        "userdata": SECRET_FIELD_MARKER,
    }
    assert clean["password_set"] is True
    assert hidden == ["cloud_config.cloudinit.userdata"]


# ---------------------------------------------------------------------------
# Delete: handler and preview against a REST fake
# ---------------------------------------------------------------------------


def _status_error(path: str, status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://vc.test{path}")
    response = httpx.Response(status, json={"error_type": "NOT_FOUND"}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _SpecFake:
    """Recording double for the connector's REST session, with a spec store."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, *, deletes: bool = True) -> None:
        self.specs: dict[str, Any] = {_WIN: copy.deepcopy(_WIN_INFO)}
        self.deletes = deletes
        self.calls: list[tuple[str, str]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.calls.append(("GET", path))
        name = _name_of(path)
        if name not in self.specs:
            raise _status_error(path, 404)
        return copy.deepcopy(self.specs[name])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        assert verb == "DELETE"
        assert json is None
        self.calls.append((verb, path))
        if self.deletes:
            self.specs.pop(_name_of(path), None)
        return None


def _name_of(path: str) -> str:
    return unquote(path.rsplit("/", 1)[1])


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _delete(conn: _SpecFake, name: str) -> Any:
    return await delete.customization_spec_delete_composite(
        operator=_operator(),
        target=_StubTarget(),
        params={"name": name},
        connector=conn,  # type: ignore[arg-type]
    )


def _ctx(conn: _SpecFake | None, **params: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=_StubTarget(),
        params=params,
    )


def test_delete_is_destructive_on_both_catalogs_with_a_preview_builder() -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == delete.OP_ID_CUSTOMIZATION_SPEC_DELETE)
    assert spec.safety_level == "destructive"
    assert spec.requires_approval is True
    assert spec.group_key == "guest"
    assert spec.llm_instructions["preconditions"]
    assert spec.op_id not in COMPOSITES_NOT_ON_8_0
    assert _PREVIEW_BUILDERS[spec.op_id] is delete.customization_spec_delete_preview
    assert _GOVERNED_SUBOP_MANIFEST[spec.op_id] == delete.SUB_OPS_CUSTOMIZATION_SPEC_DELETE


async def test_delete_sends_the_delete_and_reads_back_that_it_is_gone(
    gate: _GateRecorder,
) -> None:
    conn = _SpecFake()
    out = await _delete(conn, _WIN)
    encoded = f"{_SPECS}/%3Cwindows-spec%3E"
    assert conn.calls == [("GET", encoded), ("DELETE", encoded), ("GET", encoded)]
    assert out["status"] == "deleted"
    assert out["object"]["name"] == _WIN
    assert out["object"]["description"] == "Windows servers"
    assert _ADMIN_PASSWORD not in json.dumps(out)
    assert gate.calls[0]["op_id"] == "DELETE:/vcenter/guest/customization-specs/{name}"
    assert gate.calls[0]["params"] == {"name": _WIN}
    Draft202012Validator(delete.CUSTOMIZATION_SPEC_DELETE_RESPONSE_SCHEMA).validate(out)


async def test_delete_of_a_missing_spec_changes_nothing(gate: _GateRecorder) -> None:
    conn = _SpecFake()
    out = await _delete(conn, "missing")
    assert out["status"] == "unchanged"
    assert [verb for verb, _ in conn.calls] == ["GET"]
    assert gate.calls == []


async def test_delete_says_still_present_when_the_spec_reads_back(gate: _GateRecorder) -> None:
    conn = _SpecFake(deletes=False)
    out = await _delete(conn, _WIN)
    assert out["status"] == "still_present"


async def test_delete_held_by_policy_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="DELETE:/vcenter/guest/customization-specs/{name}",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _SpecFake()
    assert await _delete(conn, _WIN) is held
    assert [verb for verb, _ in conn.calls] == ["GET"]


async def test_preview_fills_the_blast_radius() -> None:
    preview = await delete.customization_spec_delete_preview(_ctx(_SpecFake(), name=_WIN))
    assert preview is not None
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"]["name"] == _WIN
    assert block["object"]["present"] is True
    assert block["children"] == []
    assert block["irreversibility"] == "permanent"
    assert _ADMIN_PASSWORD not in json.dumps(preview)


async def test_preview_refuses_a_spec_that_does_not_exist() -> None:
    with pytest.raises(LookupError, match="no guest customization spec"):
        await delete.customization_spec_delete_preview(_ctx(_SpecFake(), name="missing"))


async def test_preview_without_a_connector_declines() -> None:
    assert await delete.customization_spec_delete_preview(_ctx(None, name=_WIN)) is None


# ---------------------------------------------------------------------------
# Delete: governed flow through the dispatcher
# ---------------------------------------------------------------------------

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004345")
_TARGET_NAME = "example-vcenter"
_CONNECTOR_ID = "vmware-rest-9.0"


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Customization Spec Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: _SpecFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vmware_composite_operations(embedding_service=embedding)
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_customization_spec",
            )
        )
        await s.commit()


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _args(name: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": delete.OP_ID_CUSTOMIZATION_SPEC_DELETE,
        "target": _TARGET_NAME,
        "params": {"name": name},
    }


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_needs_a_preview_then_a_second_person() -> None:
    conn = _SpecFake()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = _args(_WIN)

    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert ("DELETE", f"{_SPECS}/%3Cwindows-spec%3E") not in conn.calls
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["name"] == _WIN
    assert effect["blast_radius"]["irreversibility"] == "permanent"
    assert _ADMIN_PASSWORD not in json.dumps(effect)

    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert _WIN not in conn.specs


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_of_a_missing_spec_does_not_wait_for_approval() -> None:
    conn = _SpecFake()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = _args("missing")
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert "no guest customization spec named 'missing'" in json.dumps(call)
    assert await _approval_count() == 0
    assert [verb for verb, _ in conn.calls if verb == "DELETE"] == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_delete_is_refused() -> None:
    conn = _SpecFake()
    await _bootstrap(conn)

    class _FakeTarget:
        product = "vmware"
        fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        preferred_impl_id = "vmware-rest"
        id = uuid.uuid4()
        tenant_id = _TENANT_ID
        name = _TARGET_NAME
        host = "vcenter.example.test"
        port = 443
        auth_model = "shared_service_account"

    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=delete.OP_ID_CUSTOMIZATION_SPEC_DELETE,
        target=_FakeTarget(),
        params={"name": _WIN},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert await _approval_count() == 0
    assert _WIN in conn.specs
