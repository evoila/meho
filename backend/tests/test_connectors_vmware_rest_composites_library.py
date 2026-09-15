# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the SUBSCRIBED content-library composites (#3495).

The four ``vmware.composite.content_library.subscribed.*`` handlers dispatch
their sub-ops directly on the connector session (``_get_json`` / ``_post_json``
mounted through ``mount_op_path``), and the two writes (create / sync) route
through the #2254 ``enforce_subop_policy`` seam via ``_write._write_sub_op``.
These tests stub the connector session with a recording double and stub the
policy seam with a recorder, asserting:

* the create builds the right ``Content.LibraryModel`` body (DATASTORE storage
  backing + subscription_info), resolves the datastore by name, and returns
  the new library id;
* BASIC auth threads ``user_name`` / ``password`` into subscription_info while
  ``NONE`` (the default) sends neither;
* sync / status / items.list resolve ``library_id`` / ``library_name`` and
  refuse ambiguity before any action;
* the two writes are gated (``dangerous`` / ``requires_approval=False`` at the
  sub-op seam — the top-level ``caution`` + approval posture is proven in
  ``test_connectors_vmware_rest_composites_register`` /
  ``_write_register``) and short-circuit verbatim on a parked gate;
* the reads are never gated;
* the create's park-time preview + broadcast classification keep the
  subscription ``password`` off the governed surfaces (secret hygiene);
* each handler's payload validates against its registered response schema.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import _CREDENTIAL_WRITE_OPS, classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _library, _write
from meho_backplane.connectors.vmware_rest.composites._library import (
    content_library_subscribed_create_composite,
    content_library_subscribed_items_list_composite,
    content_library_subscribed_status_composite,
    content_library_subscribed_sync_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _content_library_subscribed_create_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import PreviewContext

_CREATE_OP = "POST:/content/subscribed-library"
_CREATE_PATH = "/api/content/subscribed-library"
_SYNC_OP = "POST:/content/subscribed-library/{libraryId}?action=sync"
_FIND_LIBRARY_PATH = "/api/content/library?action=find"
_FIND_ITEM_PATH = "/api/content/library/item?action=find"
_DATASTORE_PATH = "/api/vcenter/datastore"


def _make_operator() -> Operator:
    return Operator(
        sub="op-subscribed-library",
        name="Subscribed Library Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingConnector:
    """Stub connector session: records sub-calls, serves canned JSON by path."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: dict[str, Any] | None = None
    ) -> Any:
        del target, operator
        self.calls.append({"method": "GET", "path": path, "query": params, "body": None})
        return self._serve(path)

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
        del target, operator, data, extra_headers, timeout
        self.calls.append({"method": verb, "path": path, "query": None, "body": json})
        return self._serve(path)

    def _serve(self, path: str) -> Any:
        payload = self._responses[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


class _GateRecorder:
    """Recording stub for ``enforce_subop_policy``; returns a canned verdict."""

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "op_id": op_id,
                "connector_id": connector_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": dict(params),
            }
        )
        return self._gate_for.get(op_id)

    @property
    def gated_op_ids(self) -> list[str]:
        return [c["op_id"] for c in self.calls]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _install_gate(monkeypatch: pytest.MonkeyPatch, recorder: _GateRecorder) -> _GateRecorder:
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _awaiting(op_id: str) -> OperationResult:
    return OperationResult(
        status="awaiting_approval",
        op_id=op_id,
        result=None,
        duration_ms=1.0,
        extras={"approval_request_id": "00000000-0000-0000-0000-0000000000aa"},
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_happy_path_resolves_datastore_and_builds_model_body(
    gate: _GateRecorder,
) -> None:
    conn = _RecordingConnector(
        {
            _DATASTORE_PATH: [{"datastore": "datastore-42", "name": "nfs-lab"}],
            _CREATE_PATH: "lib-new-1",
        }
    )
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "name": "vkr-lib",
            "subscription_url": "https://wp-content.vmware.com/v2/latest/lib.json",
            "datastore": "nfs-lab",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "created"
    assert out["library_id"] == "lib-new-1"
    assert out["datastore_id"] == "datastore-42"
    assert out["on_demand"] is True  # default
    assert out["automatic_sync_enabled"] is False  # default
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA).validate(out)

    # Datastore resolve (GET) then create (POST) in order.
    assert [c["path"] for c in conn.calls] == [_DATASTORE_PATH, _CREATE_PATH]
    body = conn.calls[1]["body"]
    assert body["name"] == "vkr-lib"
    assert body["storage_backings"] == [{"type": "DATASTORE", "datastore_id": "datastore-42"}]
    info = body["subscription_info"]
    assert info["subscription_url"] == "https://wp-content.vmware.com/v2/latest/lib.json"
    assert info["authentication_method"] == "NONE"
    assert info["on_demand"] is True
    assert info["automatic_sync_enabled"] is False
    # NONE auth sends no credential.
    assert "password" not in info
    assert "user_name" not in info

    # Only the create POST is gated; the datastore read is not.
    assert gate.gated_op_ids == [_CREATE_OP]
    assert gate.calls[0]["safety_level"] == "dangerous"
    assert gate.calls[0]["requires_approval"] is False


async def test_create_basic_auth_threads_username_and_password(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        {
            _DATASTORE_PATH: [{"datastore": "ds-1", "name": "ds"}],
            _CREATE_PATH: "lib-2",
        }
    )
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "name": "priv",
            "subscription_url": "https://host/lib.json",
            "datastore": "ds",
            "authentication_method": "BASIC",
            "username": "svc",
            "password": "s3cret",
            "on_demand": False,
            "automatic_sync_enabled": True,
            "ssl_thumbprint": "AA:BB",
            "description": "private mirror",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "created"
    body = conn.calls[1]["body"]
    info = body["subscription_info"]
    assert info["authentication_method"] == "BASIC"
    assert info["user_name"] == "svc"
    assert info["password"] == "s3cret"
    assert info["ssl_thumbprint"] == "AA:BB"
    assert info["on_demand"] is False
    assert info["automatic_sync_enabled"] is True
    assert body["description"] == "private mirror"


async def test_create_datastore_not_found_refuses_before_write(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({_DATASTORE_PATH: []})
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "x", "subscription_url": "https://h/lib.json", "datastore": "gone"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "datastore_not_found"
    assert out["library_id"] is None
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA).validate(out)
    # No create POST, no gate.
    assert [c["path"] for c in conn.calls] == [_DATASTORE_PATH]
    assert gate.calls == []


async def test_create_ambiguous_datastore_refuses_with_candidates(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        {
            _DATASTORE_PATH: [
                {"datastore": "ds-a", "name": "dup"},
                {"datastore": "ds-b", "name": "dup"},
            ]
        }
    )
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "x", "subscription_url": "https://h/lib.json", "datastore": "dup"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous_datastore"
    assert set(out["candidates"]) == {"ds-a", "ds-b"}
    assert gate.calls == []


async def test_create_gate_short_circuits_before_write(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gate(monkeypatch, _GateRecorder(gate_for={_CREATE_OP: _awaiting(_CREATE_OP)}))
    conn = _RecordingConnector({_DATASTORE_PATH: [{"datastore": "ds-1", "name": "ds"}]})
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "x", "subscription_url": "https://h/lib.json", "datastore": "ds"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    # Datastore resolved, but the create POST never reached the wire.
    assert [c["path"] for c in conn.calls] == [_DATASTORE_PATH]


async def test_create_http_fault_surfaces_create_error(gate: _GateRecorder) -> None:
    fault = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "https://vc/api/content/subscribed-library"),
        response=httpx.Response(500),
    )
    conn = _RecordingConnector(
        {_DATASTORE_PATH: [{"datastore": "ds-1", "name": "ds"}], _CREATE_PATH: fault}
    )
    out = await content_library_subscribed_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "x", "subscription_url": "https://h/lib.json", "datastore": "ds"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "create_error"
    assert out["library_id"] is None
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA).validate(out)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


async def test_sync_by_library_id_triggers_and_gates(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({"/api/content/subscribed-library/lib-1?action=sync": None})
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_id": "lib-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out == {"status": "sync_triggered", "library_id": "lib-1"}
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA).validate(out)
    # No name resolution when library_id is given; only the sync POST is gated.
    assert [c["path"] for c in conn.calls] == ["/api/content/subscribed-library/lib-1?action=sync"]
    assert gate.gated_op_ids == [_SYNC_OP]
    assert gate.calls[0]["params"] == {"libraryId": "lib-1"}


async def test_sync_by_library_name_resolves_then_syncs(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        {
            _FIND_LIBRARY_PATH: ["lib-9"],
            "/api/content/subscribed-library/lib-9?action=sync": None,
        }
    )
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_name": "vkr-lib"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "sync_triggered"
    assert out["library_id"] == "lib-9"
    assert [c["path"] for c in conn.calls] == [
        _FIND_LIBRARY_PATH,
        "/api/content/subscribed-library/lib-9?action=sync",
    ]
    # The find is un-gated; only the sync write hits the seam.
    assert gate.gated_op_ids == [_SYNC_OP]


async def test_sync_invalid_reference_when_neither_supplied(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({})
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "invalid_reference"
    assert conn.calls == []
    assert gate.calls == []


async def test_sync_library_not_found(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({_FIND_LIBRARY_PATH: []})
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_name": "nope"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "library_not_found"
    assert gate.calls == []


async def test_sync_ambiguous_library(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({_FIND_LIBRARY_PATH: ["a", "b"]})
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_name": "dup"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous_library"
    assert set(out["candidates"]) == {"a", "b"}
    assert gate.calls == []


async def test_sync_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gate(monkeypatch, _GateRecorder(gate_for={_SYNC_OP: _awaiting(_SYNC_OP)}))
    conn = _RecordingConnector({})
    out = await content_library_subscribed_sync_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_id": "lib-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert conn.calls == []  # sync POST never issued


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_reads_model_without_secret(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        {
            "/api/content/subscribed-library/lib-1": {
                "name": "vkr-lib",
                "type": "SUBSCRIBED",
                "last_sync_time": "2026-09-08T10:00:00Z",
                "description": "TKr source",
                "storage_backings": [{"type": "DATASTORE", "datastore_id": "ds-1"}],
                "subscription_info": {
                    "subscription_url": "https://wp-content.vmware.com/v2/latest/lib.json",
                    "authentication_method": "NONE",
                    "automatic_sync_enabled": False,
                    "on_demand": True,
                },
            }
        }
    )
    out = await content_library_subscribed_status_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_id": "lib-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ok"
    assert out["library_id"] == "lib-1"
    assert out["name"] == "vkr-lib"
    assert out["type"] == "SUBSCRIBED"
    assert out["last_sync_time"] == "2026-09-08T10:00:00Z"
    assert out["subscription_url"] == "https://wp-content.vmware.com/v2/latest/lib.json"
    assert out["on_demand"] is True
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA).validate(out)
    # Reads are never gated.
    assert gate.calls == []


async def test_status_invalid_reference(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({})
    out = await content_library_subscribed_status_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "invalid_reference"
    assert conn.calls == []


# ---------------------------------------------------------------------------
# items.list
# ---------------------------------------------------------------------------


async def test_items_list_returns_tkr_rows(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        {
            _FIND_ITEM_PATH: ["item-1", "item-2"],
            "/api/content/library/item/item-1": {
                "id": "item-1",
                "name": "v1.33.6---vmware.1-fips-vkr.2",
                "type": "vmtx",
                "version": "3",
                "cached": True,
                "size": 3221225472,
                "last_sync_time": "2026-09-08T09:00:00Z",
            },
            "/api/content/library/item/item-2": {
                "id": "item-2",
                "name": "v1.32.4---vmware.1-vkr.1",
                "type": "vmtx",
                "version": "1",
                "cached": False,
                "size": None,
                "last_sync_time": None,
            },
        }
    )
    out = await content_library_subscribed_items_list_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_id": "lib-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["library_id"] == "lib-1"
    assert out["item_count"] == 2
    names = {row["name"] for row in out["items"]}
    assert names == {"v1.33.6---vmware.1-fips-vkr.2", "v1.32.4---vmware.1-vkr.1"}
    assert out["items"][0]["cached"] is True
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA).validate(out)
    # find (POST, un-gated) + one GET per item; no write gate.
    assert conn.calls[0]["path"] == _FIND_ITEM_PATH
    assert gate.calls == []


async def test_items_list_empty_library(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({_FIND_ITEM_PATH: []})
    out = await content_library_subscribed_items_list_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_id": "lib-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["item_count"] == 0
    assert out["items"] == []
    Draft202012Validator(CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA).validate(out)


async def test_items_list_ambiguous_library_refuses(gate: _GateRecorder) -> None:
    conn = _RecordingConnector({_FIND_LIBRARY_PATH: ["a", "b"]})
    out = await content_library_subscribed_items_list_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_name": "dup"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous_library"
    # Refused at resolution; the item find never ran.
    assert [c["path"] for c in conn.calls] == [_FIND_LIBRARY_PATH]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_create_preview_echoes_identity_only_never_the_password() -> None:
    ctx = PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_make_operator(),
        target=object(),
        params={
            "name": "vkr-lib",
            "subscription_url": "https://wp-content.vmware.com/v2/latest/lib.json",
            "datastore": "nfs-lab",
            "authentication_method": "BASIC",
            "username": "svc",
            "password": "s3cret",
            "ssl_thumbprint": "AA:BB",
        },
    )
    preview = await _content_library_subscribed_create_preview(ctx)
    assert preview is not None
    # The reviewer sees the blast radius...
    assert preview["name"] == "vkr-lib"
    assert preview["subscription_url"] == "https://wp-content.vmware.com/v2/latest/lib.json"
    assert preview["datastore"] == "nfs-lab"
    assert preview["authentication_method"] == "BASIC"
    assert preview["ssl_thumbprint_pinned"] is True
    # ...but never the credential material.
    flat = repr(preview)
    assert "s3cret" not in flat
    assert "svc" not in flat
    assert "password" not in preview
    assert "username" not in preview


def test_create_op_is_pinned_credential_write() -> None:
    """The create op broadcasts aggregate-only (its params carry a password)."""
    assert "vmware.composite.content_library.subscribed.create" in _CREDENTIAL_WRITE_OPS
    assert classify_op("vmware.composite.content_library.subscribed.create") == "credential_write"
    # The sync / reads carry no secret and are not pinned.
    assert "vmware.composite.content_library.subscribed.sync" not in _CREDENTIAL_WRITE_OPS


def test_governed_subop_manifest_lists_the_two_writes() -> None:
    from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
        _GOVERNED_SUBOP_MANIFEST,
    )

    assert (
        _GOVERNED_SUBOP_MANIFEST["vmware.composite.content_library.subscribed.create"]
        == _library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_CREATE
    )
    assert (
        _GOVERNED_SUBOP_MANIFEST["vmware.composite.content_library.subscribed.sync"]
        == _library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_SYNC
    )
    # The reads auto-execute and are not in the grant-discovery manifest.
    assert "vmware.composite.content_library.subscribed.status" not in _GOVERNED_SUBOP_MANIFEST
