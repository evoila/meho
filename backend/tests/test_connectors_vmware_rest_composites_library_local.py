# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the local content-library create and the item upload.

``vmware.composite.content_library.create`` and
``vmware.composite.content_library.item.upload`` call vCenter REST on the
connector session. These tests replace the session with a recording double
and the write gate (``enforce_subop_policy``) with a recorder. They check:

* the request bodies (the LOCAL library model, the item, the PULL file);
* that nothing is written when the name is taken, the datastore or library is
  not found, the library is not local, or the URL is not allowed;
* the read-back of the new library and the item;
* the download states (done, still running, failed) and vCenter faults;
* that a parked gate is returned as is and stops the call;
* the approval previews;
* that every answer fits the registered response schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _library_upload, _write
from meho_backplane.connectors.vmware_rest.composites._library_local import (
    content_library_create_composite,
)
from meho_backplane.connectors.vmware_rest.composites._library_local_schemas import (
    CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._library_upload import (
    content_library_item_upload_composite,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext

_LIB = "11111111-2222-3333-4444-555555555555"
_LIB2 = "11111111-2222-3333-4444-666666666666"
_ITEM = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_SESSION = "session-1"

_FIND_LIB = "/api/content/library?action=find"
_GET_LIB = f"/api/content/library/{_LIB}"
_GET_LIB2 = f"/api/content/library/{_LIB2}"
_DATASTORES = "/api/vcenter/datastore"
_CREATE_LIB = "/api/content/local-library"
_FIND_ITEM = "/api/content/library/item?action=find"
_GET_ITEM = f"/api/content/library/item/{_ITEM}"
_CREATE_ITEM = "/api/content/library/item"
_CREATE_SESSION = "/api/content/library/item/update-session"
_ADD_FILE = f"/api/content/library/item/update-session/{_SESSION}/file"
_COMPLETE = f"/api/content/library/item/update-session/{_SESSION}?action=complete"
_GET_SESSION = f"/api/content/library/item/update-session/{_SESSION}"

_LOCAL_LIB = {"id": _LIB, "name": "lab-lib", "type": "LOCAL"}
_URL = "https://depot.example.test/iso/installer.iso"


def _operator() -> Operator:
    return Operator(
        sub="op-local-library",
        name="Local Library Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _http_error(status: int, error_type: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.example.test/api/x")
    body = {"error_type": error_type, "messages": [{"default_message": "no"}]}
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _Connector:
    """Records sub-calls and serves canned JSON by path.

    A value may be an exception (raised), a list (one entry per call, the last
    one repeats), or a plain payload. A path that is not listed answers 404.
    """

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self._served: dict[str, int] = {}
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
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
        **_: Any,
    ) -> Any:
        del target, operator, timeout
        self.calls.append({"method": verb, "path": path, "query": None, "body": json})
        return self._serve(path)

    def _serve(self, path: str) -> Any:
        if path not in self._responses:
            raise _http_error(404, "NOT_FOUND")
        payload = self._responses[path]
        if isinstance(payload, list) and payload and isinstance(payload[0], _Seq):
            index = self._served.get(path, 0)
            self._served[path] = index + 1
            payload = payload[min(index, len(payload) - 1)].value
        if isinstance(payload, Exception):
            raise payload
        return payload

    def body_of(self, path: str) -> Any:
        return next(c["body"] for c in self.calls if c["path"] == path)

    def paths(self, method: str | None = None) -> list[str]:
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


class _Seq:
    """One answer of a path that answers differently on each call."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _Gate:
    """Stub for ``enforce_subop_policy``; parks the op ids in *park*."""

    def __init__(self, park: set[str] | None = None) -> None:
        self.op_ids: list[str] = []
        self._park = park or set()

    async def __call__(self, *, op_id: str, **_: Any) -> OperationResult | None:
        self.op_ids.append(op_id)
        if op_id in self._park:
            return OperationResult(
                status="awaiting_approval", op_id=op_id, result=None, duration_ms=1.0
            )
        return None


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    recorder = _Gate()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_library_upload, "_POLL_INTERVAL_SECONDS", 0.0)


def _valid(schema: dict[str, Any]) -> Callable[[Any], None]:
    validator = Draft202012Validator(schema)

    def check(payload: Any) -> None:
        errors = sorted(validator.iter_errors(payload), key=str)
        assert not errors, [e.message for e in errors]

    return check


_check_create = _valid(CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA)
_check_upload = _valid(CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA)


async def _create(conn: _Connector, **params: Any) -> Any:
    return await content_library_create_composite(
        operator=_operator(),
        target=object(),
        params={"name": "lab-lib", "datastore": "ds-1", **params},
        connector=conn,  # type: ignore[arg-type]
    )


async def _upload(conn: _Connector, **params: Any) -> Any:
    base = {"library_id": _LIB, "item_name": "installer", "source_url": _URL}
    return await content_library_item_upload_composite(
        operator=_operator(),
        target=object(),
        params={**base, **params},
        connector=conn,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# content_library.create
# ---------------------------------------------------------------------------


def _create_responses(**overrides: Any) -> dict[str, Any]:
    return {
        _FIND_LIB: [],
        _DATASTORES: [{"datastore": "datastore-12", "name": "ds-1"}],
        _CREATE_LIB: _LIB,
        _GET_LIB: _LOCAL_LIB,
        **overrides,
    }


async def test_create_sends_a_local_library_body_and_reads_it_back(gate: _Gate) -> None:
    conn = _Connector(_create_responses())
    out = await _create(conn, description="for the lab")
    assert out["status"] == "created"
    assert out["library_id"] == _LIB
    assert out["datastore_id"] == "datastore-12"
    assert out["type"] == "LOCAL"
    assert conn.body_of(_CREATE_LIB) == {
        "name": "lab-lib",
        "description": "for the lab",
        "storage_backings": [{"type": "DATASTORE", "datastore_id": "datastore-12"}],
    }
    assert conn.body_of(_FIND_LIB) == {"name": "lab-lib"}
    assert gate.op_ids == ["POST:/content/local-library"]
    assert conn.paths()[-1] == _GET_LIB
    _check_create(out)


async def test_create_answers_unchanged_when_one_local_library_has_the_name(gate: _Gate) -> None:
    conn = _Connector(_create_responses(**{_FIND_LIB: [_LIB]}))
    out = await _create(conn)
    assert out["status"] == "unchanged"
    assert out["library_id"] == _LIB
    assert gate.op_ids == []
    assert _CREATE_LIB not in conn.paths()
    _check_create(out)


@pytest.mark.parametrize(
    "found",
    [
        [_LIB, _LIB2],
        [_LIB2],
    ],
)
async def test_create_refuses_a_name_used_by_other_libraries(gate: _Gate, found: list[str]) -> None:
    responses = _create_responses(**{_FIND_LIB: found})
    responses[_GET_LIB2] = {"id": _LIB2, "name": "lab-lib", "type": "SUBSCRIBED"}
    conn = _Connector(responses)
    out = await _create(conn)
    assert out["status"] == "precondition_failed"
    assert len(out["existing"]) == len(found)
    assert gate.op_ids == []
    _check_create(out)


@pytest.mark.parametrize(
    ("listing", "status"),
    [
        ([], "datastore_not_found"),
        (
            [{"datastore": "datastore-1", "name": "ds-1"}, {"datastore": "datastore-2"}],
            "ambiguous_datastore",
        ),
    ],
)
async def test_create_stops_when_the_datastore_is_not_found_once(
    gate: _Gate, listing: list[dict[str, Any]], status: str
) -> None:
    conn = _Connector(_create_responses(**{_DATASTORES: listing}))
    out = await _create(conn)
    assert out["status"] == status
    assert gate.op_ids == []
    _check_create(out)


async def test_create_maps_a_vcenter_400_to_refused(gate: _Gate) -> None:
    conn = _Connector(_create_responses(**{_CREATE_LIB: _http_error(400, "INVALID_ARGUMENT")}))
    out = await _create(conn)
    assert out["status"] == "refused"
    assert out["error_type"] == "INVALID_ARGUMENT"
    _check_create(out)


async def test_create_raises_other_vcenter_faults(gate: _Gate) -> None:
    conn = _Connector(_create_responses(**{_CREATE_LIB: _http_error(500)}))
    with pytest.raises(httpx.HTTPStatusError):
        await _create(conn)


async def test_create_answers_not_verified_when_the_library_does_not_read_back(
    gate: _Gate,
) -> None:
    responses = _create_responses()
    del responses[_GET_LIB]
    conn = _Connector(responses)
    out = await _create(conn)
    assert out["status"] == "not_verified"
    assert out["library_id"] == _LIB
    _check_create(out)


async def test_create_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", _Gate({"POST:/content/local-library"}))
    conn = _Connector(_create_responses())
    out = await _create(conn)
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert _CREATE_LIB not in conn.paths()


async def test_create_preview_names_the_datastore_and_the_existing_library() -> None:
    builder = _PREVIEW_BUILDERS["vmware.composite.content_library.create"]
    conn = _Connector(_create_responses())
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=object(),
        params={"name": "lab-lib", "datastore": "ds-1"},
    )
    effect = await builder(ctx)
    assert effect is not None
    assert effect["datastore_id"] == "datastore-12"
    assert effect["existing"] == []
    assert "Create the local library" in effect["summary"]
    assert conn.paths("POST") == [_FIND_LIB]


def test_create_schema_example_validates() -> None:
    validator = Draft202012Validator(CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA)
    for example in CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA["examples"]:
        validator.validate(example)


# ---------------------------------------------------------------------------
# content_library.item.upload
# ---------------------------------------------------------------------------


def _upload_responses(**overrides: Any) -> dict[str, Any]:
    return {
        _GET_LIB: _LOCAL_LIB,
        _FIND_ITEM: [],
        _CREATE_ITEM: _ITEM,
        _CREATE_SESSION: _SESSION,
        _ADD_FILE: {"name": "installer.iso", "status": "WAITING_FOR_TRANSFER"},
        _COMPLETE: None,
        _GET_SESSION: [_Seq({"state": "ACTIVE"}), _Seq({"state": "DONE"})],
        _GET_ITEM: {"id": _ITEM, "name": "installer", "type": "iso", "size": 42, "cached": True},
        **overrides,
    }


_UPLOAD_WRITES = [
    "POST:/content/library/item",
    "POST:/content/library/item/update-session",
    "POST:/content/library/item/update-session/{updateSessionId}/file",
    "POST:/content/library/item/update-session/{updateSessionId}?action=complete",
]


async def test_upload_creates_the_item_and_pulls_the_file(gate: _Gate) -> None:
    conn = _Connector(_upload_responses())
    out = await _upload(conn, item_type="iso", ssl_certificate_thumbprint="AA:BB")
    assert out["status"] == "uploaded"
    assert out["item_id"] == _ITEM
    assert out["session_id"] == _SESSION
    assert out["session_state"] == "DONE"
    assert out["item"]["size_bytes"] == 42
    assert gate.op_ids == _UPLOAD_WRITES
    assert conn.body_of(_CREATE_ITEM) == {"library_id": _LIB, "name": "installer", "type": "iso"}
    assert conn.body_of(_CREATE_SESSION) == {"library_item_id": _ITEM}
    assert conn.body_of(_ADD_FILE) == {
        "name": "installer.iso",
        "source_type": "PULL",
        "source_endpoint": {"uri": _URL, "ssl_certificate_thumbprint": "AA:BB"},
    }
    assert conn.body_of(_COMPLETE) is None
    assert conn.body_of(_FIND_ITEM) == {"library_id": _LIB, "name": "installer"}
    assert conn.paths("GET").count(_GET_SESSION) == 2
    _check_upload(out)


async def test_upload_finds_the_library_by_name(gate: _Gate) -> None:
    conn = _Connector(_upload_responses(**{_FIND_LIB: [_LIB]}))
    out = await content_library_item_upload_composite(
        operator=_operator(),
        target=object(),
        params={"library_name": "lab-lib", "item_name": "installer", "source_url": _URL},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "uploaded"
    assert out["library_id"] == _LIB


async def test_upload_answers_unchanged_for_an_item_with_content(gate: _Gate) -> None:
    conn = _Connector(_upload_responses(**{_FIND_ITEM: [_ITEM]}))
    out = await _upload(conn)
    assert out["status"] == "unchanged"
    assert out["item"]["size_bytes"] == 42
    assert gate.op_ids == []
    _check_upload(out)


async def test_upload_fills_an_empty_item_left_by_a_failed_try(gate: _Gate) -> None:
    empty = {"id": _ITEM, "name": "installer", "type": "iso", "size": 0}
    full = {**empty, "size": 42}
    conn = _Connector(
        _upload_responses(**{_FIND_ITEM: [_ITEM], _GET_ITEM: [_Seq(empty), _Seq(full)]})
    )
    out = await _upload(conn)
    assert out["status"] == "uploaded"
    assert _CREATE_ITEM not in conn.paths()
    assert gate.op_ids == _UPLOAD_WRITES[1:]
    _check_upload(out)


@pytest.mark.parametrize(
    "params",
    [
        {"source_url": "ftp://depot.example.test/a.iso"},
        {"source_url": "https://user:secret@depot.example.test/a.iso"},  # trufflehog:ignore
        {"source_url": "https://depot.example.test/iso/"},
    ],
)
async def test_upload_refuses_a_bad_url_before_any_call(
    gate: _Gate, params: dict[str, Any]
) -> None:
    conn = _Connector(_upload_responses())
    out = await _upload(conn, **params)
    assert out["status"] == "invalid_request"
    assert "secret" not in str(out)
    assert conn.calls == []
    _check_upload(out)


async def test_upload_takes_file_name_when_the_url_path_has_none(gate: _Gate) -> None:
    conn = _Connector(_upload_responses())
    out = await _upload(conn, source_url="https://depot.example.test/get?id=7", file_name="a.iso")
    assert out["status"] == "uploaded"
    assert conn.body_of(_ADD_FILE)["name"] == "a.iso"


async def test_upload_refuses_a_subscribed_library(gate: _Gate) -> None:
    conn = _Connector(_upload_responses(**{_GET_LIB: {**_LOCAL_LIB, "type": "SUBSCRIBED"}}))
    out = await _upload(conn)
    assert out["status"] == "precondition_failed"
    assert gate.op_ids == []
    _check_upload(out)


async def test_upload_answers_library_not_found(gate: _Gate) -> None:
    responses = _upload_responses()
    del responses[_GET_LIB]
    conn = _Connector(responses)
    out = await _upload(conn)
    assert out["status"] == "library_not_found"
    assert gate.op_ids == []
    _check_upload(out)


async def test_upload_reports_a_failed_download(gate: _Gate) -> None:
    session = {"state": "ERROR", "error_message": {"default_message": "404 from depot"}}
    conn = _Connector(_upload_responses(**{_GET_SESSION: session}))
    out = await _upload(conn)
    assert out["status"] == "upload_failed"
    assert out["session_state"] == "ERROR"
    assert "404 from depot" in out["guidance"]
    _check_upload(out)


async def test_upload_answers_in_progress_when_the_wait_runs_out(gate: _Gate) -> None:
    conn = _Connector(_upload_responses(**{_GET_SESSION: {"state": "ACTIVE"}}))
    out = await _upload(conn, wait_seconds=0)
    assert out["status"] == "in_progress"
    assert out["session_id"] == _SESSION
    assert conn.paths("GET").count(_GET_SESSION) == 1
    _check_upload(out)


async def test_upload_maps_a_400_on_the_item_create_to_refused(gate: _Gate) -> None:
    conn = _Connector(_upload_responses(**{_CREATE_ITEM: _http_error(400, "INVALID_ARGUMENT")}))
    out = await _upload(conn)
    assert out["status"] == "refused"
    assert out["item_id"] is None
    _check_upload(out)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (_http_error(400, "RESOURCE_BUSY"), "in_progress"),
        (_http_error(500, "ERROR"), "upload_failed"),
        (httpx.ConnectError("down"), "upload_failed"),
    ],
)
async def test_upload_reports_a_fault_after_the_item_exists(
    gate: _Gate, error: Exception, status: str
) -> None:
    conn = _Connector(_upload_responses(**{_CREATE_SESSION: error}))
    out = await _upload(conn)
    assert out["status"] == status
    assert out["item_id"] == _ITEM
    _check_upload(out)


async def test_upload_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", _Gate({"POST:/content/library/item"}))
    conn = _Connector(_upload_responses())
    out = await _upload(conn)
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert _CREATE_ITEM not in conn.paths()


async def test_upload_preview_shows_the_library_and_the_url() -> None:
    builder = _PREVIEW_BUILDERS["vmware.composite.content_library.item.upload"]
    conn = _Connector(_upload_responses())
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=object(),
        params={"library_id": _LIB, "item_name": "installer", "source_url": _URL},
    )
    effect = await builder(ctx)
    assert effect is not None
    assert effect["library"] == {"id": _LIB, "name": "lab-lib", "type": "LOCAL"}
    assert effect["file_name"] == "installer.iso"
    assert effect["existing_item"] is None
    assert conn.paths("POST") == [_FIND_ITEM]


def test_upload_schema_rejects_a_url_with_a_password() -> None:
    validator = Draft202012Validator(CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA)
    for example in CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA["examples"]:
        validator.validate(example)
    bad = {
        "library_id": _LIB,
        "item_name": "x",
        "source_url": "https://u:p@host/a.iso",  # trufflehog:ignore
    }
    assert not validator.is_valid(bad)
