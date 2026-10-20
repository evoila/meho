# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.event.tail`` request conformance on the 8.0 and 9.0 connectors (#3706).

vCenter 8.0.x rejects a ``QueryEvents`` call with no body (HTTP 500
``InvalidArgument: Invalid JSON syntax: Unexpected end of file``). These tests
run the real composite through the real versioned connector classes over a
mocked HTTP transport (respx) and check the wire request:

* the 8.0 connector posts to ``/sdk/vim25/8.0.3.0/...``, the 9.0 connector to
  ``/sdk/vim25/9.0.0.0/...``;
* both send the same non-empty, ``_typeName``-tagged ``EventFilterSpec`` body,
  with the filter params in the right members.

The parameter-schema checks run the dispatcher's own validator, so they show
what a caller can and cannot send.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites._read import event_tail_composite
from meho_backplane.connectors.vmware_rest.composites.schemas import EVENT_TAIL_PARAMETER_SCHEMA
from meho_backplane.connectors.vmware_rest.connector import VmwareRest80Connector
from meho_backplane.operations._validate import validate_params
from tests._spec_shelf import openapi_request_body_props, require_shelf_spec

_BASE = "https://vc.test.invalid"

_FILTERED_PARAMS: dict[str, Any] = {
    "begin_time": "2026-10-19T08:00:00Z",
    "end_time": "2026-10-19T10:00:00Z",
    "event_type_ids": ["esx.problem.storage.apd.start", "esx.problem.vmfs.nfs.server.disconnect"],
    "entity": {"moId": "datastore-12", "type": "Datastore"},
    "max_count": 50,
}


@dataclass
class _StubTarget:
    name: str = "vc"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator() -> Operator:
    return Operator(
        sub="op-event-tail",
        name="Event Tail Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


async def _close(connector: VmwareRestConnector) -> None:
    """Close the clients without the session-revoke DELETE."""
    connector._session_tokens.clear()
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


_VERSIONED_PATHS = pytest.mark.parametrize(
    ("connector_cls", "about_version", "release"),
    [
        (VmwareRest80Connector, "8.0.3", "8.0.3.0"),
        (VmwareRestConnector, "9.0.0.0", "9.0.0.0"),
    ],
    ids=["vmware-rest-8.0", "vmware-rest-9.0"],
)


async def _call_event_tail(
    connector_cls: type[VmwareRestConnector],
    about_version: str,
    release: str,
    params: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the composite; return ``(result, sent request body)``."""
    connector = connector_cls(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about_version})
            route = mock.post(f"/sdk/vim25/{release}/EventManager/EventManager/QueryEvents")
            route.respond(200, json=events)
            result = await event_tail_composite(
                operator=_make_operator(),
                target=_StubTarget(),
                params=params,
                connector=connector,
            )
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
    finally:
        await _close(connector)
    return result, body


@_VERSIONED_PATHS
@pytest.mark.asyncio
async def test_unfiltered_tail_sends_a_non_empty_filter_spec(
    connector_cls: type[VmwareRestConnector], about_version: str, release: str
) -> None:
    events = [
        {
            "_typeName": "UserLoginSessionEvent",
            "createdTime": "2026-10-19T08:00:01Z",
            "fullFormattedMessage": "User logged in",
        }
    ]
    result, body = await _call_event_tail(connector_cls, about_version, release, {}, events)
    assert body == {
        "filter": {"_typeName": "EventFilterSpec", "disableFullMessage": False, "maxCount": 100}
    }
    assert result["count"] == 1
    assert result["events"][0]["eventTypeId"] == "UserLoginSessionEvent"


@_VERSIONED_PATHS
@pytest.mark.asyncio
async def test_filtered_tail_lands_every_filter_key(
    connector_cls: type[VmwareRestConnector], about_version: str, release: str
) -> None:
    _, body = await _call_event_tail(connector_cls, about_version, release, _FILTERED_PARAMS, [])
    spec = body["filter"]
    assert spec["_typeName"] == "EventFilterSpec"
    assert spec["time"] == {
        "_typeName": "EventFilterSpecByTime",
        "beginTime": "2026-10-19T08:00:00Z",
        "endTime": "2026-10-19T10:00:00Z",
    }
    assert spec["eventTypeId"] == _FILTERED_PARAMS["event_type_ids"]
    assert spec["entity"] == {
        "_typeName": "EventFilterSpecByEntity",
        "entity": {
            "_typeName": "ManagedObjectReference",
            "type": "Datastore",
            "value": "datastore-12",
        },
        "recursion": "all",
    }
    assert spec["maxCount"] == 50


def test_query_events_body_key_is_served_by_the_pinned_vi_json_spec() -> None:
    """The pinned spec names ``filter`` as a QueryEvents body member (skips without the shelf)."""
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    body_props = openapi_request_body_props(spec_path)
    assert "filter" in body_props["POST:/EventManager/{moId}/QueryEvents"]


def test_schema_accepts_the_filter_params() -> None:
    assert validate_params(EVENT_TAIL_PARAMETER_SCHEMA, _FILTERED_PARAMS) == []
    assert validate_params(EVENT_TAIL_PARAMETER_SCHEMA, {}) == []
    assert EVENT_TAIL_PARAMETER_SCHEMA["additionalProperties"] is False
    assert EVENT_TAIL_PARAMETER_SCHEMA["properties"]["max_count"]["default"] == 100


@pytest.mark.parametrize(
    "params",
    [
        {"begin_time": "yesterday"},
        {"end_time": "2026-10-19 10:00:00"},
        {"max_count": 1001},
        {"max_count": 0},
        {"event_type_ids": []},
        {"entity": {"moId": "datastore-12"}},
        {"entity": {"moId": "datastore-12", "type": "Datastore", "recursion": "up"}},
        {"max_events": 10},
        {"limit": 10},
    ],
    ids=[
        "begin-not-rfc3339",
        "end-not-rfc3339",
        "max-count-over-cap",
        "max-count-zero",
        "empty-type-list",
        "entity-without-type",
        "bad-recursion",
        "old-max-events-key",
        "unknown-key",
    ],
)
def test_schema_rejects_bad_params(params: dict[str, Any]) -> None:
    assert validate_params(EVENT_TAIL_PARAMETER_SCHEMA, params) != []
