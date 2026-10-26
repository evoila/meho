# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.vsan_health`` typed op (#2258).

Re-shipped from the former ``vmware.composite.host.vsan_health`` read: a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector` that
queries ``VsanQueryVcClusterHealthSummary`` on the
``vsan-cluster-health-system`` singleton directly on the connector
session (no ``dispatch_child``, no ingested descriptor), so it works on a
fresh boot with zero catalog ingest. #4922 added ``from_cache`` (vSAN's
``fetchFromCache``), ``checked_at`` / ``from_cache`` in the answer, and the
fallback to the stored result when a fresh check ends with a read timeout.

The handler logic is exercised against a fake connector that records
the :meth:`_post_vsan` calls, so the assertion targets are the call-shape
contract (one cluster-scoped query on the vSAN health method, errors not
hidden) and the parse/aggregation contract, without a live httpx
transport. The real transport (VI-JSON on vCenter 9.x, SOAP on 8.0) is
covered by ``tests/test_connectors_vmware_rest_vsan_transport.py``; the
legacy ``/rest`` mount by the respx integration test in
``tests/integration/test_connectors_vmware_rest_vcsim.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    FRESH_CHECK_TIMED_OUT,
    HOST_VSAN_HEALTH_GROUP_KEY,
    VMWARE_HOST_VSAN_HEALTH_OP,
    VSAN_QUERY_HEALTH_SUMMARY,
    build_vsan_query_health_params,
    fresh_check_timeout_text,
    host_vsan_health_impl,
)
from meho_backplane.operations._errors import result_connector_timeout

_CLUSTER_MOREF = {
    "_typeName": "ManagedObjectReference",
    "type": "ClusterComputeResource",
    "value": "domain-c123",
}
_CHECKED_AT = "2026-10-25T18:30:12.345Z"

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for the typed-op handler unit tests."""
    return Operator(
        sub="op-host-vsan",
        name="Host vSAN Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


class _FakeConnector:
    """Records the vSAN calls ``host_vsan_health_impl`` makes.

    Serves one canned ``VsanClusterHealthSummary`` on :meth:`_post_vsan`
    (or raises ``post_error``). ``answers`` instead gives one answer per
    call, in order; an exception in the list is raised for that call.
    """

    def __init__(
        self,
        *,
        summary: Any = None,
        post_error: Exception | None = None,
        answers: list[Any] | None = None,
    ) -> None:
        self._summary = summary
        self._post_error = post_error
        self._answers = list(answers) if answers is not None else None
        self.post_calls: list[tuple[VsanMethod, dict[str, Any]]] = []

    async def _post_vsan(
        self,
        target: Any,
        vsan_method: VsanMethod,
        *,
        operator: Operator,
        json: dict[str, Any],
    ) -> Any:
        # The connector picks the transport (VI-JSON on 9.x, SOAP on 8.0);
        # the handler only names the vSAN method and the arguments (#4441).
        del target, operator
        self.post_calls.append((vsan_method, json))
        if self._answers is not None:
            answer = self._answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if self._post_error is not None:
            raise self._post_error
        return self._summary


def _read_timeout(seconds: float | None = 30.0) -> httpx.ReadTimeout:
    """A read timeout as the shared HTTP client raises it, with its timeout map."""
    request = httpx.Request(
        "POST",
        "https://vc.test.invalid/sdk/vim25/9.1.0.0/vsan/x",
        extensions={"timeout": {"connect": 5.0, "read": seconds, "write": 30.0, "pool": 5.0}},
    )
    return httpx.ReadTimeout("read timed out", request=request)


def _vsan_summary(overall: str, groups: list[Any]) -> dict[str, Any]:
    """Build a ``VsanClusterHealthSummary`` payload for one cluster."""
    return {"overallHealth": overall, "groups": groups, "timestamp": _CHECKED_AT}


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_build_query_params_scopes_to_cluster_moref() -> None:
    """Pins the ``_typeName``-annotated MoRef body (#3103); no ``fetchFromCache`` by default."""
    body = build_vsan_query_health_params("domain-c123")
    assert body == {"cluster": _CLUSTER_MOREF}
    assert build_vsan_query_health_params("domain-c123", from_cache=False) == body


def test_build_query_params_from_cache_adds_fetch_from_cache_after_cluster() -> None:
    """#4922: ``fetchFromCache: true`` follows ``cluster``, the order the method defines."""
    body = build_vsan_query_health_params("domain-c123", from_cache=True)
    assert body == {"cluster": _CLUSTER_MOREF, "fetchFromCache": True}
    assert list(body) == ["cluster", "fetchFromCache"]


def test_fresh_check_timeout_text_names_the_limit() -> None:
    assert fresh_check_timeout_text(30.0) == (
        "The fresh vSAN check did not finish within 30 seconds. "
        "A host of the cluster may be down or in maintenance mode."
    )
    assert "within MEHO's time limit." in fresh_check_timeout_text(None)


# ---------------------------------------------------------------------------
# host_vsan_health_impl — call-shape + aggregation
# ---------------------------------------------------------------------------


def test_health_method_is_the_vsan_health_summary_on_the_health_system() -> None:
    """The method, its vSAN object and the object's fixed id (vendor sample ``vsanapiutils.py``)."""
    assert isinstance(VSAN_QUERY_HEALTH_SUMMARY, VsanMethod)
    assert VSAN_QUERY_HEALTH_SUMMARY.mo_type == "VsanVcClusterHealthSystem"
    assert VSAN_QUERY_HEALTH_SUMMARY.moid == "vsan-cluster-health-system"
    assert VSAN_QUERY_HEALTH_SUMMARY.method == "VsanQueryVcClusterHealthSummary"


@pytest.mark.asyncio
async def test_queries_health_summary_for_cluster() -> None:
    """One health-service query fires, scoped to the cluster; groups + tests flatten."""
    summary = _vsan_summary(
        overall="green",
        groups=[
            {
                "groupId": "com.vmware.vsan.health.test.network",
                "groupName": "Network",
                "groupHealth": "green",
                "groupTests": [
                    {
                        "testId": "com.vmware.vsan.health.test.hostconnectivity",
                        "testName": "vSAN cluster partition",
                        "testHealth": "green",
                        "testShortDescription": "Checks host connectivity.",
                    },
                ],
            },
            {
                "groupId": "com.vmware.vsan.health.test.physicaldisks",
                "groupName": "Physical disk",
                "groupHealth": "yellow",
                "groupTests": [],
            },
        ],
    )
    conn = _FakeConnector(summary=summary)

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c123"})

    # One call on the vSAN transport; the cluster MoRef is the body arg.
    assert len(conn.post_calls) == 1
    vsan_method, body = conn.post_calls[0]
    assert vsan_method == VSAN_QUERY_HEALTH_SUMMARY
    assert body == {"cluster": _CLUSTER_MOREF}

    assert out["cluster"] == "domain-c123"
    assert out["overall_health"] == "green"
    assert out["checked_at"] == _CHECKED_AT
    assert out["from_cache"] is False
    assert "fresh_check" not in out
    assert "note" not in out
    assert out["groups"] == [
        {
            "group_id": "com.vmware.vsan.health.test.network",
            "group_name": "Network",
            "group_health": "green",
            "tests": [
                {
                    "test_id": "com.vmware.vsan.health.test.hostconnectivity",
                    "test_name": "vSAN cluster partition",
                    "test_health": "green",
                    "test_short_description": "Checks host connectivity.",
                },
            ],
        },
        {
            "group_id": "com.vmware.vsan.health.test.physicaldisks",
            "group_name": "Physical disk",
            "group_health": "yellow",
            "tests": [],
        },
    ]
    assert "read_note" not in out


@pytest.mark.asyncio
async def test_from_cache_sends_fetch_from_cache_and_marks_the_answer() -> None:
    """#4922: ``from_cache: true`` asks vSAN for its stored result; the answer says so."""
    conn = _FakeConnector(summary=_vsan_summary(overall="green", groups=[]))

    out = await host_vsan_health_impl(
        conn, _make_operator(), _Target(), {"cluster": "domain-c123", "from_cache": True}
    )

    assert conn.post_calls == [
        (VSAN_QUERY_HEALTH_SUMMARY, {"cluster": _CLUSTER_MOREF, "fetchFromCache": True})
    ]
    assert out["from_cache"] is True
    assert out["checked_at"] == _CHECKED_AT
    assert out["overall_health"] == "green"
    # Nothing timed out, so the answer has no fresh_check note.
    assert "fresh_check" not in out
    assert "note" not in out


@pytest.mark.asyncio
async def test_fresh_check_read_timeout_falls_back_to_the_stored_result_once() -> None:
    """#4922: a read timeout of the fresh check is followed by exactly one cached read."""
    conn = _FakeConnector(answers=[_read_timeout(30.0), _vsan_summary(overall="yellow", groups=[])])

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c123"})

    assert [body for _method, body in conn.post_calls] == [
        {"cluster": _CLUSTER_MOREF},
        {"cluster": _CLUSTER_MOREF, "fetchFromCache": True},
    ]
    assert out["overall_health"] == "yellow"
    assert out["checked_at"] == _CHECKED_AT
    assert out["from_cache"] is True
    assert out["fresh_check"] == FRESH_CHECK_TIMED_OUT
    assert out["note"] == (
        "The fresh vSAN check did not finish within 30 seconds. A host of the cluster may be "
        "down or in maintenance mode. This is the last stored result."
    )


@pytest.mark.asyncio
async def test_stored_result_failing_too_raises_the_timeout_with_the_plain_text() -> None:
    """#4922: when the cached read fails as well, the caller gets the read timeout, explained."""
    timeout = _read_timeout(30.0)
    cache_error = httpx.HTTPStatusError(
        "vSAN fault",
        request=httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.1.0.0/vsan/x"),
        response=httpx.Response(500, json={"_typeName": "VsanFault"}),
    )
    conn = _FakeConnector(answers=[timeout, cache_error])

    with pytest.raises(httpx.ReadTimeout) as raised:
        await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c123"})

    assert len(conn.post_calls) == 2
    assert raised.value.__cause__ is cache_error
    # The request of the first timeout stays on the error, so the dispatcher
    # still reports the configured read limit.
    assert raised.value.request is timeout.request
    result = result_connector_timeout("vmware.host.vsan_health", raised.value, 1.0)
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_timeout"
    assert result.extras["phase"] == "read"
    assert result.extras["configured_timeout"] == 30.0
    assert result.extras["exception_message"] == (
        "The fresh vSAN check did not finish within 30 seconds. A host of the cluster may be "
        "down or in maintenance mode. The last stored result could not be read either."
    )


@pytest.mark.asyncio
async def test_from_cache_read_timeout_is_not_retried() -> None:
    """A timed-out cached read has no faster fallback; it is raised as it is."""
    timeout = _read_timeout(30.0)
    conn = _FakeConnector(answers=[timeout])

    with pytest.raises(httpx.ReadTimeout) as raised:
        await host_vsan_health_impl(
            conn, _make_operator(), _Target(), {"cluster": "domain-c123", "from_cache": True}
        )

    assert raised.value is timeout
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_timeout_without_a_request_still_explains_itself() -> None:
    """A timeout that carries no request (no timeout map) gets the text without a number."""
    conn = _FakeConnector(answers=[httpx.ReadTimeout("read timed out"), _vsan_summary("green", [])])

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c123"})

    assert out["fresh_check"] == FRESH_CHECK_TIMED_OUT
    assert out["note"].startswith("The fresh vSAN check did not finish within MEHO's time limit.")


@pytest.mark.asyncio
async def test_failed_vsan_call_raises_instead_of_empty_health() -> None:
    """#4441: a failed vSAN call is not turned into empty health with a note."""
    error = httpx.HTTPStatusError(
        "vSAN fault",
        request=httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.1.0.0/vsan/x"),
        response=httpx.Response(500, json={"_typeName": "VsanFault"}),
    )
    conn = _FakeConnector(post_error=error)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c404"})

    assert raised.value is error


@pytest.mark.asyncio
async def test_answer_without_a_summary_raises() -> None:
    """No health summary in the answer is an error, never an empty healthy-looking result."""
    conn = _FakeConnector(summary=None)

    with pytest.raises(RuntimeError, match="no health summary"):
        await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})


@pytest.mark.asyncio
async def test_single_group_and_test_become_lists() -> None:
    """SOAP (vCenter 8.0) sends a list with one entry as a single dict; the op still lists it."""
    conn = _FakeConnector(
        summary={
            "overallHealth": "yellow",
            "groups": {
                "groupId": "com.vmware.vsan.health.test.data",
                "groupName": "Data",
                "groupHealth": "yellow",
                "groupTests": {
                    "testId": "com.vmware.vsan.health.test.objecthealth",
                    "testName": "vSAN object health",
                    "testHealth": "yellow",
                    "testShortDescription": "Checks the health of vSAN objects.",
                },
            },
        }
    )

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["groups"] == [
        {
            "group_id": "com.vmware.vsan.health.test.data",
            "group_name": "Data",
            "group_health": "yellow",
            "tests": [
                {
                    "test_id": "com.vmware.vsan.health.test.objecthealth",
                    "test_name": "vSAN object health",
                    "test_health": "yellow",
                    "test_short_description": "Checks the health of vSAN objects.",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_tolerates_legacy_value_envelope() -> None:
    conn = _FakeConnector(summary={"value": _vsan_summary(overall="red", groups=[])})

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["overall_health"] == "red"
    assert out["groups"] == []


@pytest.mark.asyncio
async def test_tolerates_missing_groups_key() -> None:
    """A summary without a ``groups`` list degrades to an empty group list."""
    conn = _FakeConnector(summary={"overallHealth": "green"})

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["overall_health"] == "green"
    assert out["groups"] == []
    assert out["checked_at"] is None


@pytest.mark.asyncio
async def test_missing_group_tests_degrade_to_empty_list() -> None:
    conn = _FakeConnector(
        summary=_vsan_summary(
            overall="yellow",
            groups=[{"groupId": "g", "groupName": "G", "groupHealth": "yellow"}],
        )
    )

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["groups"][0]["tests"] == []


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_op() -> None:
    assert VMWARE_HOST_VSAN_HEALTH_OP in VMWARE_TYPED_OPS
    assert VMWARE_HOST_VSAN_HEALTH_OP.op_id == "vmware.host.vsan_health"
    assert VMWARE_HOST_VSAN_HEALTH_OP.safety_level == "safe"
    assert VMWARE_HOST_VSAN_HEALTH_OP.requires_approval is False
    assert VMWARE_HOST_VSAN_HEALTH_OP.group_key == HOST_VSAN_HEALTH_GROUP_KEY


def test_contract_has_no_read_note_any_more() -> None:
    """#4441: failures are errors, so the answer has no ``read_note`` and always a group list."""
    schema = VMWARE_HOST_VSAN_HEALTH_OP.response_schema or {}
    assert "read_note" not in schema["properties"]
    assert schema["properties"]["groups"]["type"] == "array"
    assert schema["required"] == ["cluster", "overall_health", "groups", "checked_at", "from_cache"]
    guidance = VMWARE_HOST_VSAN_HEALTH_OP.llm_instructions or {}
    assert "read_note" not in guidance["output_shape"]
    assert "read_note" not in VMWARE_HOST_VSAN_HEALTH_OP.description


def test_contract_describes_from_cache_and_checked_at() -> None:
    """#4922: the texts tell the caller about the stored result and the time of the check."""
    params = VMWARE_HOST_VSAN_HEALTH_OP.parameter_schema["properties"]
    assert params["from_cache"]["type"] == "boolean"
    assert params["from_cache"]["default"] is False
    assert VMWARE_HOST_VSAN_HEALTH_OP.parameter_schema["required"] == ["cluster"]
    response = VMWARE_HOST_VSAN_HEALTH_OP.response_schema or {}
    assert response["properties"]["from_cache"]["type"] == "boolean"
    assert response["properties"]["checked_at"]["type"] == ["string", "null"]
    assert response["properties"]["fresh_check"]["enum"] == [FRESH_CHECK_TIMED_OUT]
    assert "note" in response["properties"]
    guidance = VMWARE_HOST_VSAN_HEALTH_OP.llm_instructions or {}
    assert "from_cache: true" in guidance["when_to_call"]
    assert "from_cache" in guidance["parameter_hints"]
    assert "checked_at" in guidance["output_shape"]
    assert "from_cache: true" in VMWARE_HOST_VSAN_HEALTH_OP.description
    # Still a plain read: no approval, safe.
    assert VMWARE_HOST_VSAN_HEALTH_OP.safety_level == "safe"
    assert VMWARE_HOST_VSAN_HEALTH_OP.requires_approval is False


def test_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_HOST_VSAN_HEALTH_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_handler_signature_has_no_dispatch_child() -> None:
    """Typed handler must NOT accept dispatch_child (else it'd be a composite)."""
    import inspect

    params = inspect.signature(VmwareRestConnector.host_vsan_health).parameters
    assert "dispatch_child" not in params
    assert "operator" in params
