# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the content-library capture, publish and export composites.

* ``vmware.composite.vm.capture_to_library`` and
  ``vmware.composite.content_library.publish.set`` call vCenter REST on the
  connector session. A recording double stands in for the session, and a
  recorder for the write check (``enforce_subop_policy``).
* ``vmware.composite.content_library.item.export`` and
  ``vmware.composite.vm.export_ovf`` use the same double for the vCenter REST
  calls, and ``respx`` for the file bytes: the vCenter download and the
  destination that takes ``PUT``.

They check the request bodies and the ``Client-Token`` header, that a change
waits when its step waits for approval, the vendor faults, the read-back,
the clean-up after a good run and after a failure in the middle, the
keep-alive during a long copy, and that no answer holds a password, a token
or a download URL. Saved answers use made-up names and ids.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest import _transfer
from meho_backplane.connectors.vmware_rest.composites import (
    _library_capture,
    _library_export_session,
    _library_publish,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._library_capture import (
    CAPTURE_COMPOSITE,
    vm_capture_to_library_composite,
)
from meho_backplane.connectors.vmware_rest.composites._library_export import (
    EXPORT_OVF_COMPOSITE,
    ITEM_EXPORT_COMPOSITE,
    content_library_item_export_composite,
    vm_export_ovf_composite,
)
from meho_backplane.connectors.vmware_rest.composites._library_export_schemas import (
    CAPTURE_RESPONSE_SCHEMA,
    EXPORT_RESPONSE_SCHEMA,
    PUBLISH_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._library_publish import (
    PUBLISH_SET_COMPOSITE,
    content_library_publish_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD

_VC = "https://vc.example.test"
_DEST = "https://store.example.test/exports/"
_TOKEN = "dest-token-value"
_SESSION_HEADER = "vcenter-session-value"
_PASSWORD = "publish-password-value"
_CURRENT = "current-password-value"
_LIB = "11111111-2222-4333-8444-555555555555"
_ITEM = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_OLD = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
_YOUNG = "cccccccc-dddd-4eee-8fff-000000000000"
_OTHER = "dddddddd-eeee-4fff-8000-111111111111"
_DS_ID = "download-session-1"

_FIND_LIB = "/api/content/library?action=find"
_GET_LIB = f"/api/content/library/{_LIB}"
_GET_LOCAL = f"/api/content/local-library/{_LIB}"
_FIND_ITEM = "/api/content/library/item?action=find"
_GET_ITEM = f"/api/content/library/item/{_ITEM}"
_GET_VM = "/api/vcenter/vm/vm-42"
_CAPTURE_OVF = "/api/vcenter/ovf/library-item"
_CAPTURE_TEMPLATE = "/api/vcenter/vm-template/library-items"
_SESSION = "/api/content/library/item/download-session"
_SESSION_ONE = f"{_SESSION}/{_DS_ID}"
_FILES = f"{_SESSION_ONE}/file"
_PREPARE = f"{_SESSION_ONE}/file?action=prepare"
_KEEP_ALIVE = f"{_SESSION_ONE}?action=keep-alive"
_ITEMS = "/api/content/library/item"

_LOCAL_LIB = {"id": _LIB, "name": "<local-library>", "type": "LOCAL"}
_OVF_ITEM = {"name": "<ovf-item>", "type": "ovf", "size": 3072, "content_version": "1"}


def _operator() -> Operator:
    return Operator(
        sub="op-library-export",
        name="Library Export Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000e0e1"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-example"
    host = "vc.example.test"


def _http_error(
    status: int, error_type: str | None = None, message: str = "vCenter says no"
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{_VC}/api/x")
    body = {"error_type": error_type, "messages": [{"default_message": message}]}
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _Seq:
    """One answer of a path that answers differently on each call."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _Connector:
    """Records vCenter REST calls and serves canned JSON by path.

    A GET with a query is served by ``path?key=value``. A value may be an
    exception (raised), a list of :class:`_Seq` (one per call, the last one
    repeats), or a plain payload. A path that is not listed answers 404.
    """

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, responses: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self._responses = responses
        self._served: dict[str, int] = {}
        self._client = client
        self.calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        del target, operator
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: dict[str, Any] | None = None
    ) -> Any:
        del target, operator
        key = path
        if params:
            key += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.calls.append({"method": "GET", "path": key, "body": None, "headers": None})
        return self._serve(key)

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        **_: Any,
    ) -> Any:
        del target, operator
        self.calls.append({"method": verb, "path": path, "body": json, "headers": extra_headers})
        return self._serve(f"{verb} {path}" if f"{verb} {path}" in self._responses else path)

    async def _http_client(self, target: Any) -> httpx.AsyncClient:
        del target
        assert self._client is not None
        return self._client

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        del target, operator
        return {"vmware-api-session-id": _SESSION_HEADER}

    def _serve(self, key: str) -> Any:
        if key not in self._responses:
            raise _http_error(404, "NOT_FOUND")
        payload = self._responses[key]
        if isinstance(payload, list) and payload and isinstance(payload[0], _Seq):
            index = self._served.get(key, 0)
            self._served[key] = index + 1
            payload = payload[min(index, len(payload) - 1)].value
        if isinstance(payload, Exception):
            raise payload
        return payload

    def call(self, method: str, path: str) -> dict[str, Any]:
        return next(c for c in self.calls if c["method"] == method and c["path"] == path)

    def paths(self, method: str | None = None) -> list[str]:
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


class _Gate:
    """Stub for ``enforce_subop_policy``; parks the op ids in *park*."""

    def __init__(self) -> None:
        self.op_ids: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.park: set[str] = set()

    async def __call__(self, *, op_id: str, params: dict[str, Any], **_: Any) -> Any:
        self.op_ids.append(op_id)
        self.params.append(params)
        if op_id in self.park:
            return OperationResult(
                status="awaiting_approval", op_id=op_id, result=None, duration_ms=1.0
            )
        return None


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    recorder = _Gate()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_library_capture, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_library_export_session, "_PREPARE_POLL_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_token(target: Any, operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        assert param == "destination_token_ref"
        assert target.secret_ref == "<vault-path-of-token>"
        return {"token": _TOKEN}

    async def fake_password(target: Any, operator: Any, *, param: str) -> dict[str, Any]:
        return {"password": _PASSWORD if param == "password_ref" else _CURRENT}

    monkeypatch.setattr(_transfer, "load_param_secret_data", fake_token)
    monkeypatch.setattr(_library_publish, "load_param_secret_data", fake_password)


@pytest.fixture
async def vc_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_VC) as client:
        yield client


def _check(schema: dict[str, Any], payload: Any) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=str)
    assert not errors, [e.message for e in errors]


def _no_secrets(payload: Any) -> None:
    text = json.dumps(payload, default=str)
    for secret in (_TOKEN, _SESSION_HEADER, _PASSWORD, _CURRENT, "/cls/data/"):
        assert secret not in text


async def _run(handler: Any, conn: _Connector, params: dict[str, Any]) -> Any:
    return await handler(operator=_operator(), target=_Target(), params=params, connector=conn)


# ===========================================================================
# vm.capture_to_library
# ===========================================================================


def _capture_responses(**overrides: Any) -> dict[str, Any]:
    return {
        _GET_VM: {"name": "<vm-name>", "power_state": "POWERED_OFF"},
        _FIND_LIB: [_LIB],
        _GET_LIB: _LOCAL_LIB,
        _FIND_ITEM: [],
        _CAPTURE_OVF: {"succeeded": True, "ovf_library_item_id": _ITEM},
        _CAPTURE_TEMPLATE: _ITEM,
        _GET_ITEM: _OVF_ITEM,
        **overrides,
    }


_CAPTURE_PARAMS = {"vm": "vm-42", "library_name": "<local-library>", "item_name": "<item-name>"}


async def test_capture_ovf_sends_a_client_token_and_reads_the_item_back(gate: _Gate) -> None:
    conn = _Connector(_capture_responses())
    out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)

    assert out["status"] == "captured"
    assert out["library_item_id"] == _ITEM
    assert out["item"]["type"] == "ovf"
    call = conn.call("POST", _CAPTURE_OVF)
    assert call["body"] == {
        "source": {"type": "VirtualMachine", "id": "vm-42"},
        "target": {"library_id": _LIB},
        "create_spec": {"name": "<item-name>"},
    }
    token = call["headers"]["Client-Token"]
    assert str(uuid.UUID(token)) == token
    assert out["client_token"] == token
    assert gate.op_ids == ["POST:/vcenter/ovf/library-item"]
    assert conn.paths()[-1] == _GET_ITEM
    _check(CAPTURE_RESPONSE_SCHEMA, out)


async def test_each_capture_gets_a_new_client_token(gate: _Gate) -> None:
    tokens = set()
    for _ in range(2):
        conn = _Connector(_capture_responses())
        out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)
        tokens.add(out["client_token"])
    assert len(tokens) == 2


async def test_capture_vm_template_body(gate: _Gate) -> None:
    conn = _Connector(_capture_responses(**{_GET_ITEM: {**_OVF_ITEM, "type": "vm-template"}}))
    params = {
        **_CAPTURE_PARAMS,
        "template_type": "VM_TEMPLATE",
        "placement": {"cluster": "domain-c7"},
        "datastore": "datastore-17",
        "description": "<reason>",
    }
    out = await _run(vm_capture_to_library_composite, conn, params)

    assert out["status"] == "captured"
    assert out["client_token"] is None
    call = conn.call("POST", _CAPTURE_TEMPLATE)
    assert call["body"] == {
        "source_vm": "vm-42",
        "name": "<item-name>",
        "library": _LIB,
        "placement": {"cluster": "domain-c7"},
        "description": "<reason>",
        "vm_home_storage": {"datastore": "datastore-17"},
        "disk_storage": {"datastore": "datastore-17"},
    }
    assert call["headers"] is None
    _check(CAPTURE_RESPONSE_SCHEMA, out)


async def test_capture_waits_for_approval_and_sends_nothing(gate: _Gate) -> None:
    gate.park = {"POST:/vcenter/ovf/library-item"}
    conn = _Connector(_capture_responses())
    out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert _CAPTURE_OVF not in conn.paths("POST")


async def test_capture_vendor_fault_reaches_the_answer(gate: _Gate) -> None:
    fault = _http_error(400, "NOT_ALLOWED_IN_CURRENT_STATE")
    conn = _Connector(_capture_responses(**{_CAPTURE_OVF: fault}))
    out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)
    assert out["status"] == "failed"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert "vCenter says no" in out["guidance"]
    assert out["issues"][0]["severity"] == "error"
    _check(CAPTURE_RESPONSE_SCHEMA, out)


async def test_capture_that_does_not_succeed_lists_the_ovf_errors(gate: _Gate) -> None:
    result = {
        "succeeded": False,
        "error": {
            "errors": [{"message": {"default_message": "disk not readable"}}],
            "warnings": [],
            "information": [],
        },
    }
    conn = _Connector(_capture_responses(**{_CAPTURE_OVF: result}))
    out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)
    assert out["status"] == "failed"
    assert out["messages"] == ["disk not readable"]
    _check(CAPTURE_RESPONSE_SCHEMA, out)


async def test_capture_of_a_taken_name_changes_nothing(gate: _Gate) -> None:
    conn = _Connector(_capture_responses(**{_FIND_ITEM: [_ITEM]}))
    out = await _run(vm_capture_to_library_composite, conn, _CAPTURE_PARAMS)
    assert out["status"] == "already_exists"
    assert out["library_item_id"] == _ITEM
    assert gate.op_ids == []
    _check(CAPTURE_RESPONSE_SCHEMA, out)


@pytest.mark.parametrize(
    ("overrides", "params", "status"),
    [
        ({_GET_LIB: {**_LOCAL_LIB, "type": "SUBSCRIBED"}}, {}, "precondition_failed"),
        ({_GET_VM: _http_error(404)}, {}, "not_found"),
        ({_FIND_LIB: []}, {}, "not_found"),
        ({}, {"template_type": "VM_TEMPLATE"}, "invalid_request"),
    ],
)
async def test_capture_stops_before_any_change(
    gate: _Gate, overrides: dict[str, Any], params: dict[str, Any], status: str
) -> None:
    conn = _Connector(_capture_responses(**overrides))
    out = await _run(vm_capture_to_library_composite, conn, {**_CAPTURE_PARAMS, **params})
    assert out["status"] == status
    assert gate.op_ids == []
    _check(CAPTURE_RESPONSE_SCHEMA, out)


# ===========================================================================
# content_library.publish.set
# ===========================================================================


def _publish_responses(publish_info: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        _FIND_LIB: [_LIB],
        _GET_LIB: _LOCAL_LIB,
        _GET_LOCAL: [_Seq({"publish_info": publish_info}), _Seq({"publish_info": after})],
        f"PATCH {_GET_LOCAL}": {},
    }


_OFF = {"published": False, "authentication_method": "NONE"}
_ON_BASIC = {
    "published": True,
    "authentication_method": "BASIC",
    "user_name": "vcsp",
    "publish_url": "https://vc.example.test/cls/vcsp/lib/x/lib.json",
    "password": "should-never-show",
}


async def test_publish_turns_publishing_on_and_reads_it_back(gate: _Gate) -> None:
    after = {**_OFF, "published": True, "publish_url": "https://vc.example.test/lib.json"}
    conn = _Connector(_publish_responses(_OFF, after))
    out = await _run(
        content_library_publish_set_composite,
        conn,
        {"library_id": _LIB, "published": True, "authentication": "NONE"},
    )
    assert out["status"] == "updated"
    assert conn.call("PATCH", _GET_LOCAL)["body"] == {
        "publish_info": {"published": True, "authentication_method": "NONE"}
    }
    assert out["before"]["published"] is False
    assert out["publish"]["published"] is True
    assert gate.op_ids == ["PATCH:/content/local-library/{libraryId}"]
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


async def test_publish_with_a_password_shows_only_the_vault_reference(gate: _Gate) -> None:
    conn = _Connector(_publish_responses(_OFF, _ON_BASIC))
    out = await _run(
        content_library_publish_set_composite,
        conn,
        {
            "library_id": _LIB,
            "published": True,
            "authentication": "BASIC",
            "password_ref": "<vault-path-of-password>",
        },
    )
    assert out["status"] == "updated"
    assert out["password_changed"] is True
    sent = conn.call("PATCH", _GET_LOCAL)
    # The request to vCenter carried the password; the recorder kept a copy of
    # the body dict, which the handler empties of passwords after the call.
    assert "password" not in sent["body"]["publish_info"]
    assert gate.params[0] == {
        "libraryId": _LIB,
        "publish_info": {
            "published": True,
            "authentication_method": "BASIC",
            "password_ref": "<vault-path-of-password>",
        },
    }
    _no_secrets(out)
    _no_secrets(gate.params)
    assert "should-never-show" not in json.dumps(out)
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


async def test_publish_sends_the_password_to_vcenter(gate: _Gate) -> None:
    seen: list[dict[str, Any]] = []
    conn = _Connector(_publish_responses(_ON_BASIC, _ON_BASIC))
    original = conn._post_json

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(json.loads(json.dumps(kwargs.get("json"))))
        return await original(*args, **kwargs)

    conn._post_json = spy  # type: ignore[method-assign]
    out = await _run(
        content_library_publish_set_composite,
        conn,
        {
            "library_id": _LIB,
            "published": True,
            "password_ref": "<vault-path-of-password>",
            "current_password_ref": "<vault-path-of-current-password>",
        },
    )
    assert out["status"] == "updated"
    assert seen[0]["publish_info"]["password"] == _PASSWORD
    assert seen[0]["publish_info"]["current_password"] == _CURRENT
    _no_secrets(out)


async def test_publish_needs_the_current_password_to_unpublish_a_basic_library(
    gate: _Gate,
) -> None:
    conn = _Connector(_publish_responses(_ON_BASIC, _ON_BASIC))
    out = await _run(
        content_library_publish_set_composite, conn, {"library_id": _LIB, "published": False}
    )
    assert out["status"] == "precondition_failed"
    assert gate.op_ids == []
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


async def test_publish_with_the_same_settings_is_unchanged(gate: _Gate) -> None:
    conn = _Connector(_publish_responses(_OFF, _OFF))
    out = await _run(
        content_library_publish_set_composite, conn, {"library_id": _LIB, "published": False}
    )
    assert out["status"] == "unchanged"
    assert gate.op_ids == []
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


async def test_publish_waits_for_approval_and_sends_nothing(gate: _Gate) -> None:
    gate.park = {"PATCH:/content/local-library/{libraryId}"}
    conn = _Connector(_publish_responses(_OFF, _OFF))
    out = await _run(
        content_library_publish_set_composite, conn, {"library_id": _LIB, "published": True}
    )
    assert isinstance(out, OperationResult)
    assert "PATCH" not in [c["method"] for c in conn.calls]


@pytest.mark.parametrize(("status", "expected"), [(400, "refused"), (500, "failed")])
async def test_publish_vendor_fault(gate: _Gate, status: int, expected: str) -> None:
    responses = _publish_responses(_OFF, _OFF)
    responses[f"PATCH {_GET_LOCAL}"] = _http_error(status, "INVALID_ARGUMENT")
    conn = _Connector(responses)
    out = await _run(
        content_library_publish_set_composite, conn, {"library_id": _LIB, "published": True}
    )
    assert out["status"] == expected
    assert out["error_type"] == "INVALID_ARGUMENT"
    assert f"HTTP {status} (INVALID_ARGUMENT)" in out["guidance"]
    assert VENDOR_TEXT_WITHHELD in out["guidance"]
    assert "vCenter says no" not in json.dumps(out)
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


@pytest.mark.parametrize(("status", "expected"), [(400, "refused"), (500, "failed")])
async def test_publish_vendor_fault_keeps_no_password(
    gate: _Gate, status: int, expected: str
) -> None:
    responses = _publish_responses(_ON_BASIC, _ON_BASIC)
    responses[f"PATCH {_GET_LOCAL}"] = _http_error(
        status, "INVALID_ARGUMENT", f"bad password {_PASSWORD}, current {_CURRENT}"
    )
    conn = _Connector(responses)
    out = await _run(
        content_library_publish_set_composite,
        conn,
        {
            "library_id": _LIB,
            "published": True,
            "password_ref": "<vault-path-of-password>",
            "current_password_ref": "<vault-path-of-current-password>",
        },
    )
    assert out["status"] == expected
    assert out["error_type"] == "INVALID_ARGUMENT"
    assert f"HTTP {status}" in out["guidance"]
    _no_secrets(out)
    _check(PUBLISH_SET_RESPONSE_SCHEMA, out)


async def test_publish_reads_no_password_when_the_step_waits(
    gate: _Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []

    async def fake_password(target: Any, operator: Any, *, param: str) -> dict[str, Any]:
        reads.append(param)
        return {"password": _PASSWORD}

    monkeypatch.setattr(_library_publish, "load_param_secret_data", fake_password)
    gate.park = {"PATCH:/content/local-library/{libraryId}"}
    conn = _Connector(_publish_responses(_OFF, _OFF))
    out = await _run(
        content_library_publish_set_composite,
        conn,
        {
            "library_id": _LIB,
            "published": True,
            "authentication": "BASIC",
            "password_ref": "<vault-path-of-password>",
        },
    )
    assert isinstance(out, OperationResult)
    assert reads == []
    assert "PATCH" not in [c["method"] for c in conn.calls]


# ===========================================================================
# The exports
# ===========================================================================

_FILE_NAMES = ["<vm-name>.ovf", "<vm-name>-disk-0.vmdk"]
_BYTES = {_FILE_NAMES[0]: b"<ovf/>" * 10, _FILE_NAMES[1]: b"d" * 4096}


def _file_key(name: str) -> str:
    return f"{_FILES}?file_name={name}"


def _uri(name: str) -> str:
    # vCenter may name another host for itself; MEHO keeps only the path.
    return f"https://vcenter-fqdn.example.test:443/cls/data/{_DS_ID}/{name}"


def _export_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        _GET_ITEM: _OVF_ITEM,
        _SESSION: _DS_ID,
        _FILES: [{"name": name, "status": "UNPREPARED"} for name in _FILE_NAMES],
        _PREPARE: {},
        _KEEP_ALIVE: {},
        f"DELETE {_SESSION_ONE}": {},
    }
    for name in _FILE_NAMES:
        responses[_file_key(name)] = [
            _Seq({"name": name, "status": "PREPARING"}),
            _Seq({"name": name, "status": "PREPARED", "download_endpoint": {"uri": _uri(name)}}),
        ]
    responses.update(overrides)
    return responses


def _mock_bytes(
    mock: respx.MockRouter, *, fail_second: bool = False, delay: float = 0.0
) -> dict[str, respx.Route]:
    routes: dict[str, respx.Route] = {}
    for index, name in enumerate(_FILE_NAMES):
        data = _BYTES[name]

        async def gen(data: bytes = data) -> AsyncIterator[bytes]:
            for start in range(0, len(data), 1024):
                if delay:
                    await asyncio.sleep(delay)
                yield data[start : start + 1024]

        if fail_second and index == 1:
            response = httpx.Response(500)
        else:
            response = httpx.Response(
                200, headers={"content-length": str(len(data))}, content=gen()
            )
        routes[f"src:{name}"] = mock.get(f"{_VC}/cls/data/{_DS_ID}/{name}").mock(
            return_value=response
        )
        final = f"{_DEST}{name}"
        partial = f"{final}.meho-partial"
        mock.head(final).mock(return_value=httpx.Response(404))
        mock.head(partial).mock(return_value=httpx.Response(404))
        routes[f"put:{name}"] = mock.put(partial).mock(return_value=httpx.Response(201))
        mock.route(method="MOVE", url=partial).mock(return_value=httpx.Response(201))
    return routes


_EXPORT_PARAMS = {
    "library_item_id": _ITEM,
    "destination_url": _DEST,
    "destination_token_ref": "<vault-path-of-token>",
}


@respx.mock(assert_all_called=False)
async def test_item_export_copies_every_file_and_ends_the_session(
    respx_mock: respx.MockRouter, vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    routes = _mock_bytes(respx_mock)
    conn = _Connector(_export_responses(), vc_client)
    out = await _run(content_library_item_export_composite, conn, _EXPORT_PARAMS)

    assert out["status"] == "exported"
    assert [row["name"] for row in out["files"]] == _FILE_NAMES
    for row in out["files"]:
        assert row["status"] == "done"
        assert row["size"] == len(_BYTES[row["name"]])
        assert len(row["sha256"]) == 64
    assert out["cleanup"] == {
        "download_session_deleted": True,
        "temporary_item_deleted": None,
        "note": None,
    }
    create = conn.call("POST", _SESSION)
    assert create["body"] == {"library_item_id": _ITEM}
    token = create["headers"]["Client-Token"]
    assert str(uuid.UUID(token)) == token
    assert out["client_tokens"] == {"capture": None, "download_session": token}
    assert conn.paths("DELETE") == [_SESSION_ONE]
    # The vCenter session went only to the target host; the token only to the store.
    for name in _FILE_NAMES:
        src = routes[f"src:{name}"].calls.last.request
        assert src.url.host == "vc.example.test"
        assert src.headers["vmware-api-session-id"] == _SESSION_HEADER
        assert "authorization" not in src.headers
        put = routes[f"put:{name}"].calls.last.request
        assert put.headers["authorization"] == f"Bearer {_TOKEN}"
        assert "vmware-api-session-id" not in put.headers
    assert gate.op_ids == []
    _no_secrets(out)
    _check(EXPORT_RESPONSE_SCHEMA, out)


@respx.mock(assert_all_called=False)
async def test_item_export_failure_in_the_middle_still_cleans_up(
    respx_mock: respx.MockRouter, vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    _mock_bytes(respx_mock, fail_second=True)
    conn = _Connector(_export_responses(), vc_client)
    out = await _run(content_library_item_export_composite, conn, _EXPORT_PARAMS)

    assert out["status"] == "failed"
    assert [row["status"] for row in out["files"]] == ["done", "failed"]
    assert "1 file(s) were copied" in out["guidance"]
    assert out["cleanup"]["download_session_deleted"] is True
    assert conn.paths("DELETE") == [_SESSION_ONE]
    _no_secrets(out)
    _check(EXPORT_RESPONSE_SCHEMA, out)


@respx.mock(assert_all_called=False)
async def test_item_export_keeps_the_session_open_during_a_long_copy(
    respx_mock: respx.MockRouter,
    vc_client: httpx.AsyncClient,
    gate: _Gate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_transfer, "_KEEP_ALIVE_INTERVAL", 0.01)
    _mock_bytes(respx_mock, delay=0.02)
    conn = _Connector(_export_responses(), vc_client)
    out = await _run(content_library_item_export_composite, conn, _EXPORT_PARAMS)

    assert out["status"] == "exported"
    assert conn.paths("POST").count(_KEEP_ALIVE) >= 1


async def test_item_export_stops_when_vcenter_cannot_prepare_a_file(
    vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    responses = _export_responses(**{_file_key(_FILE_NAMES[1]): {"status": "ERROR"}})
    conn = _Connector(responses, vc_client)
    out = await _run(content_library_item_export_composite, conn, _EXPORT_PARAMS)
    assert out["status"] == "failed"
    assert "could not prepare" in out["guidance"]
    assert out["cleanup"]["download_session_deleted"] is True
    _check(EXPORT_RESPONSE_SCHEMA, out)


async def test_item_export_of_a_missing_item_opens_no_session(
    vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    conn = _Connector(_export_responses(**{_GET_ITEM: _http_error(404)}), vc_client)
    out = await _run(content_library_item_export_composite, conn, _EXPORT_PARAMS)
    assert out["status"] == "not_found"
    assert _SESSION not in conn.paths("POST")
    _check(EXPORT_RESPONSE_SCHEMA, out)


def _created(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _export_ovf_responses(**overrides: Any) -> dict[str, Any]:
    return _export_responses(
        **{
            _GET_VM: {"name": "<vm-name>"},
            _FIND_LIB: [_LIB],
            _GET_LIB: _LOCAL_LIB,
            _CAPTURE_OVF: {"succeeded": True, "ovf_library_item_id": _ITEM},
            f"{_ITEMS}?library_id={_LIB}": [_ITEM, _OLD, _YOUNG, _OTHER],
            f"/api/content/library/item/{_OLD}": {
                "name": "meho-transfer-ovf-export-0123456789ab",
                "creation_time": _created(3),
            },
            f"/api/content/library/item/{_YOUNG}": {
                "name": "meho-transfer-ovf-export-ba9876543210",
                "creation_time": _created(0.1),
            },
            f"/api/content/library/item/{_OTHER}": {
                "name": "<kept-item>",
                "creation_time": _created(30),
            },
            f"DELETE /api/content/library/item/{_ITEM}": {},
            f"DELETE /api/content/library/item/{_OLD}": {},
            **overrides,
        }
    )


_OVF_PARAMS = {
    "vm": "vm-42",
    "library_name": "<local-library>",
    "destination_url": _DEST,
    "destination_token_ref": "<vault-path-of-token>",
}


@respx.mock(assert_all_called=False)
async def test_export_ovf_captures_copies_and_deletes_the_temporary_item(
    respx_mock: respx.MockRouter, vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    _mock_bytes(respx_mock)
    conn = _Connector(_export_ovf_responses(), vc_client)
    out = await _run(vm_export_ovf_composite, conn, _OVF_PARAMS)

    assert out["status"] == "exported"
    assert out["temporary_item"] is True
    capture = conn.call("POST", _CAPTURE_OVF)
    assert capture["body"]["create_spec"]["name"].startswith("meho-transfer-ovf-export-")
    assert out["client_tokens"]["capture"] == capture["headers"]["Client-Token"]
    assert out["client_tokens"]["download_session"] is not None
    assert out["cleanup"] == {
        "download_session_deleted": True,
        "temporary_item_deleted": True,
        "note": None,
    }
    deletes = conn.paths("DELETE")
    assert f"/api/content/library/item/{_OLD}" in deletes
    assert f"/api/content/library/item/{_YOUNG}" not in deletes
    assert f"/api/content/library/item/{_OTHER}" not in deletes
    # The session ends before the temporary item goes.
    assert deletes[-2:] == [_SESSION_ONE, f"/api/content/library/item/{_ITEM}"]
    assert out["leftovers_removed"] == [
        f"library item meho-transfer-ovf-export-0123456789ab ({_OLD})"
    ]
    assert gate.op_ids == ["POST:/vcenter/ovf/library-item"]
    _no_secrets(out)
    _check(EXPORT_RESPONSE_SCHEMA, out)


@respx.mock(assert_all_called=False)
async def test_export_ovf_failure_in_the_middle_still_deletes_the_temporary_item(
    respx_mock: respx.MockRouter, vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    _mock_bytes(respx_mock, fail_second=True)
    conn = _Connector(_export_ovf_responses(), vc_client)
    out = await _run(vm_export_ovf_composite, conn, _OVF_PARAMS)

    assert out["status"] == "failed"
    assert out["cleanup"]["temporary_item_deleted"] is True
    assert conn.paths("DELETE")[-1] == f"/api/content/library/item/{_ITEM}"
    _check(EXPORT_RESPONSE_SCHEMA, out)


async def test_export_ovf_reports_a_temporary_item_it_could_not_delete(
    vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    responses = _export_ovf_responses(
        **{
            _file_key(_FILE_NAMES[0]): {"status": "ERROR"},
            f"DELETE /api/content/library/item/{_ITEM}": _http_error(500),
        }
    )
    conn = _Connector(responses, vc_client)
    out = await _run(vm_export_ovf_composite, conn, _OVF_PARAMS)
    assert out["status"] == "failed"
    assert out["cleanup"]["temporary_item_deleted"] is False
    assert _ITEM in out["cleanup"]["note"]
    _check(EXPORT_RESPONSE_SCHEMA, out)


async def test_export_ovf_waits_for_approval_of_the_capture(
    vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    gate.park = {"POST:/vcenter/ovf/library-item"}
    conn = _Connector(_export_ovf_responses(), vc_client)
    out = await _run(vm_export_ovf_composite, conn, _OVF_PARAMS)
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert _SESSION not in conn.paths("POST")
    assert conn.paths("DELETE") == []


async def test_export_ovf_refuses_an_item_that_is_not_ovf(
    vc_client: httpx.AsyncClient, gate: _Gate
) -> None:
    conn = _Connector(_export_responses(**{_GET_ITEM: {**_OVF_ITEM, "type": "iso"}}), vc_client)
    params = {k: v for k, v in _OVF_PARAMS.items() if k not in ("vm", "library_name")}
    out = await _run(vm_export_ovf_composite, conn, {**params, "library_item_id": _ITEM})
    assert out["status"] == "precondition_failed"
    assert _SESSION not in conn.paths("POST")
    _check(EXPORT_RESPONSE_SCHEMA, out)


# ===========================================================================
# Registration, schemas, previews
# ===========================================================================

_ROWS = {
    row["op_id"]: row
    for row in (
        CAPTURE_COMPOSITE,
        PUBLISH_SET_COMPOSITE,
        ITEM_EXPORT_COMPOSITE,
        EXPORT_OVF_COMPOSITE,
    )
}
_LEVELS = {
    "vmware.composite.vm.capture_to_library": "caution",
    "vmware.composite.content_library.publish.set": "caution",
    "vmware.composite.content_library.item.export": "dangerous",
    "vmware.composite.vm.export_ovf": "dangerous",
}


@pytest.mark.parametrize("op_id", sorted(_LEVELS))
def test_each_change_waits_for_approval_at_its_level(op_id: str) -> None:
    spec = next(spec for spec in _COMPOSITES if spec.op_id == op_id)
    assert spec.requires_approval is True
    assert spec.safety_level == _LEVELS[op_id]
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _GOVERNED_SUBOP_MANIFEST
    assert op_id in _PREVIEW_BUILDERS


@pytest.mark.parametrize("op_id", sorted(_LEVELS))
def test_examples_validate(op_id: str) -> None:
    schema = _ROWS[op_id]["parameter_schema"]
    for example in schema["examples"]:
        assert validate_params(schema, example) == []


def test_the_secret_bearing_rows_keep_bodies_out_of_the_flight_recorder() -> None:
    assert "password" in PUBLISH_SET_COMPOSITE["tags"]
    assert "token" in ITEM_EXPORT_COMPOSITE["tags"]
    assert "token" in EXPORT_OVF_COMPOSITE["tags"]


def test_export_ovf_schema_wants_a_vm_with_a_library_or_an_item() -> None:
    schema = EXPORT_OVF_COMPOSITE["parameter_schema"]
    base = {"destination_url": _DEST, "destination_token_ref": "<ref>"}
    assert validate_params(schema, {**base, "vm": "vm-42"}) != []
    assert validate_params(schema, {**base, "vm": "vm-42", "library_item_id": _ITEM}) != []
    assert validate_params(schema, {**base, "library_item_id": _ITEM}) == []
    assert validate_params(schema, {**base, "vm": "vm-42", "library_id": _LIB}) == []
    assert (
        validate_params(schema, {**base, "library_item_id": _ITEM, "destination_url": "http://x/"})
        != []
    )


async def test_publish_preview_shows_the_reference_not_the_password(gate: _Gate) -> None:
    conn = _Connector(_publish_responses(_OFF, _OFF))
    ctx = PreviewContext(
        descriptor=EndpointDescriptor(op_id="vmware.composite.content_library.publish.set"),
        params={
            "library_id": _LIB,
            "published": True,
            "authentication": "BASIC",
            "password_ref": "<vault-path-of-password>",
        },
        target=_Target(),
        operator=_operator(),
        connector_instance=conn,
    )
    effect = await _PREVIEW_BUILDERS["vmware.composite.content_library.publish.set"](ctx)
    assert effect is not None
    assert effect["password_ref"] == "<vault-path-of-password>"
    assert "turned on" in effect["summary"]
    _no_secrets(effect)


async def test_export_preview_names_the_item_and_the_destination(
    vc_client: httpx.AsyncClient,
) -> None:
    conn = _Connector(_export_responses(), vc_client)
    ctx = PreviewContext(
        descriptor=EndpointDescriptor(op_id="vmware.composite.content_library.item.export"),
        params=dict(_EXPORT_PARAMS),
        target=_Target(),
        operator=_operator(),
        connector_instance=conn,
    )
    effect = await _PREVIEW_BUILDERS["vmware.composite.content_library.item.export"](ctx)
    assert effect is not None
    assert "<ovf-item>" in effect["summary"]
    assert _DEST in effect["summary"]
    assert conn.paths("POST") == []
