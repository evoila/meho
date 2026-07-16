# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Prometheus connector (Initiative #2228 / Task #2234).

Coverage matrix (per the Task #2234 acceptance criteria):

* **Registration** — ``prometheus`` resolves via ``register_connector_v2``
  (versioned triple + wildcard) and appears in ``all_connectors_v2()``.
* **Read-only gate** — a non-``/api/v1/`` path or a non-GET method is
  rejected before any upstream call (``respx`` records zero requests); the
  ``/api/v1/admin/`` blocklist and the ``..`` traversal guard hold.
* **Optional auth** — a ``secret_ref=None`` target dispatches ``query``
  with no ``Authorization`` header and no credential-loader call; a target
  with a Bearer ``token`` secret sends ``Authorization: Bearer``; a
  ``username``/``password`` secret sends ``Authorization: Basic``.
* **Fingerprint** — round-trips a recorded ``buildinfo`` fixture and
  surfaces a ``flavour`` hint distinguishing thanos/mimir from vanilla
  prometheus.
* **Recorded-fixture ops** — ``query`` / ``query_range`` / ``targets``
  round-trip recorded response fixtures and hit the correct wire path.

The wire is mocked with ``respx``; the credential loader is injected so
Vault is never touched. Handlers are invoked directly (not through the DB
dispatcher) so the suite stays a pure unit test.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.prometheus import (
    PROMETHEUS_OPS,
    PROMETHEUS_WHEN_TO_USE_BY_GROUP,
    PrometheusConnector,
)
from meho_backplane.connectors.prometheus.connector import (
    PrometheusReadOnlyError,
    _enforce_read_only,
)
from meho_backplane.connectors.registry import all_connectors_v2

_HOST = "prometheus.test.invalid"
_BASE = f"https://{_HOST}"


def _operator() -> Operator:
    return Operator(
        sub="operator@test",
        name="Op",
        email=None,
        raw_jwt="jwt",
        tenant_id=uuid.UUID(int=1),
        tenant_role=TenantRole.OPERATOR,
    )


def _target(*, secret_ref: str | None = None, extras: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        id=uuid.UUID(int=7),
        tenant_id=uuid.UUID(int=1),
        name="prom-1",
        host=_HOST,
        port=None,
        secret_ref=secret_ref,
        extras=extras or {},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registers_versioned_and_wildcard() -> None:
    """Importing the package self-registers both the triple and the wildcard."""
    v2 = all_connectors_v2()
    assert v2[("prometheus", "2.x", "prometheus-api")] is PrometheusConnector
    assert v2[("prometheus", "", "")] is PrometheusConnector


def test_connector_class_attrs() -> None:
    assert PrometheusConnector.product == "prometheus"
    assert PrometheusConnector.version == "2.x"
    assert PrometheusConnector.impl_id == "prometheus-api"
    # Loses the resolver tie-break to nothing but beats a priority-0 shim.
    assert PrometheusConnector.priority == 1


def test_ops_shape_is_read_only() -> None:
    """All eight ops are safe, no-approval, read-only, and covered by a group blurb."""
    op_ids = {op.op_id for op in PROMETHEUS_OPS}
    assert op_ids == {
        "prometheus.query",
        "prometheus.query_range",
        "prometheus.series",
        "prometheus.labels",
        "prometheus.targets",
        "prometheus.rules",
        "prometheus.alerts",
        "prometheus.get",
    }
    for op in PROMETHEUS_OPS:
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.parameter_schema.get("additionalProperties") is False
        assert op.group_key in PROMETHEUS_WHEN_TO_USE_BY_GROUP


# ---------------------------------------------------------------------------
# Read-only gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/query"),
        ("DELETE", "/api/v1/query"),
        ("GET", "/-/reload"),
        ("GET", "/metrics"),
        ("GET", "/api/v1/admin/tsdb/delete_series"),
        ("GET", "/api/v1/../-/reload"),
    ],
)
def test_gate_rejects(method: str, path: str) -> None:
    with pytest.raises(PrometheusReadOnlyError):
        _enforce_read_only(method, path)


def test_gate_allows_read_path() -> None:
    _enforce_read_only("GET", "/api/v1/query")  # must not raise


@respx.mock
async def test_passthrough_rejects_off_allowlist_with_no_upstream_call() -> None:
    """The gate fires before any HTTP request leaves the process."""
    connector = PrometheusConnector()
    target = _target()
    with pytest.raises(PrometheusReadOnlyError):
        await connector.raw_get(_operator(), target, {"path": "/-/reload"})
    assert respx.calls.call_count == 0
    await connector.aclose()


# ---------------------------------------------------------------------------
# Optional auth
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_no_secret_sends_no_auth_and_skips_loader() -> None:
    loader_calls: list[Any] = []

    async def _loader(target: Any, operator: Any) -> dict[str, object]:
        loader_calls.append(target)
        return {"token": "unused"}

    route = respx.get(f"{_BASE}/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )
    )
    connector = PrometheusConnector(secret_loader=_loader)
    result = await connector.query(_operator(), _target(secret_ref=None), {"query": "up"})
    assert result["status"] == "success"
    assert route.called
    # No credential load attempted; no Authorization header on the wire.
    assert loader_calls == []
    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}
    # PromQL forwarded as a query param.
    assert route.calls.last.request.url.params.get("query") == "up"
    await connector.aclose()


@respx.mock
async def test_query_bearer_token_sends_authorization() -> None:
    async def _loader(target: Any, operator: Any) -> dict[str, object]:
        return {"token": "s3cr3t-bearer\n"}

    route = respx.get(f"{_BASE}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    connector = PrometheusConnector(secret_loader=_loader)
    await connector.query(_operator(), _target(secret_ref="kv/prom"), {"query": "up"})
    # Newline stripped by strip_credential_value.
    assert route.calls.last.request.headers["Authorization"] == "Bearer s3cr3t-bearer"
    await connector.aclose()


@respx.mock
async def test_query_basic_auth_sends_authorization() -> None:
    async def _loader(target: Any, operator: Any) -> dict[str, object]:
        return {"username": "u", "password": "p"}

    route = respx.get(f"{_BASE}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    connector = PrometheusConnector(secret_loader=_loader)
    await connector.query(_operator(), _target(secret_ref="kv/prom"), {"query": "up"})
    # base64("u:p") == "dTpw"
    assert route.calls.last.request.headers["Authorization"] == "Basic dTpw"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Path prefix (Mimir)
# ---------------------------------------------------------------------------


@respx.mock
async def test_path_prefix_applied_to_wire_but_gate_checks_logical() -> None:
    route = respx.get(f"{_BASE}/prometheus/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    connector = PrometheusConnector()
    target = _target(extras={"path_prefix": "/prometheus", "flavour": "mimir"})
    await connector.query(_operator(), target, {"query": "up"})
    assert route.called
    await connector.aclose()


# ---------------------------------------------------------------------------
# Recorded-fixture ops
# ---------------------------------------------------------------------------


_QUERY_FIXTURE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {"__name__": "up", "job": "prometheus", "instance": "localhost:9090"},
                "value": [1721000000, "1"],
            }
        ],
    },
}

_QUERY_RANGE_FIXTURE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"__name__": "up", "job": "node"},
                "values": [[1721000000, "1"], [1721000015, "1"]],
            }
        ],
    },
}

_TARGETS_FIXTURE = {
    "status": "success",
    "data": {
        "activeTargets": [
            {
                "scrapeUrl": "http://localhost:9090/metrics",
                "health": "up",
                "lastError": "",
                "labels": {"job": "prometheus"},
            },
            {
                "scrapeUrl": "http://node:9100/metrics",
                "health": "down",
                "lastError": "connection refused",
                "labels": {"job": "node"},
            },
        ],
        "droppedTargets": [],
    },
}


@respx.mock
async def test_query_roundtrips_fixture() -> None:
    route = respx.get(f"{_BASE}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_QUERY_FIXTURE)
    )
    connector = PrometheusConnector()
    result = await connector.query(
        _operator(), _target(), {"query": "up", "time": "2024-07-15T00:00:00Z"}
    )
    assert result["data"]["resultType"] == "vector"
    assert result["data"]["result"][0]["value"] == [1721000000, "1"]
    assert route.calls.last.request.url.params.get("time") == "2024-07-15T00:00:00Z"
    await connector.aclose()


@respx.mock
async def test_query_range_roundtrips_fixture() -> None:
    route = respx.get(f"{_BASE}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_QUERY_RANGE_FIXTURE)
    )
    connector = PrometheusConnector()
    result = await connector.query_range(
        _operator(),
        _target(),
        {"query": "up", "start": "1721000000", "end": "1721000015", "step": "15s"},
    )
    assert result["data"]["resultType"] == "matrix"
    params = route.calls.last.request.url.params
    assert params.get("query") == "up"
    assert params.get("step") == "15s"
    await connector.aclose()


@respx.mock
async def test_targets_roundtrips_fixture() -> None:
    route = respx.get(f"{_BASE}/api/v1/targets").mock(
        return_value=httpx.Response(200, json=_TARGETS_FIXTURE)
    )
    connector = PrometheusConnector()
    result = await connector.targets(_operator(), _target(), {"state": "active"})
    assert len(result["data"]["activeTargets"]) == 2
    assert route.calls.last.request.url.params.get("state") == "active"
    await connector.aclose()


@respx.mock
async def test_series_uses_repeated_match_param() -> None:
    route = respx.get(f"{_BASE}/api/v1/series").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": []})
    )
    connector = PrometheusConnector()
    await connector.series(_operator(), _target(), {"match": ["up", 'node_load1{job="node"}']})
    match_values = route.calls.last.request.url.params.get_list("match[]")
    assert match_values == ["up", 'node_load1{job="node"}']
    await connector.aclose()


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


_BUILDINFO_FIXTURE = {
    "status": "success",
    "data": {
        "version": "2.53.0",
        "revision": "1a2b3c4d",
        "branch": "HEAD",
        "buildUser": "root@builder",
        "buildDate": "20240701-00:00:00",
        "goVersion": "go1.22.4",
    },
}


def _mock_fingerprint_endpoints(
    *, active: int = 2, firing: int = 1, groups: int = 3, ready: int = 200
) -> None:
    respx.get(f"{_BASE}/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json=_BUILDINFO_FIXTURE)
    )
    respx.get(f"{_BASE}/-/ready").mock(return_value=httpx.Response(ready, text="Ready"))
    respx.get(f"{_BASE}/api/v1/targets").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "data": {"activeTargets": [{}] * active}},
        )
    )
    respx.get(f"{_BASE}/api/v1/alerts").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"alerts": ([{"state": "firing"}] * firing + [{"state": "pending"}])},
            },
        )
    )
    respx.get(f"{_BASE}/api/v1/rules").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"groups": [{}] * groups}}
        )
    )


@respx.mock
async def test_fingerprint_roundtrips_buildinfo_default_flavour() -> None:
    _mock_fingerprint_endpoints()
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target())
    assert fp.reachable is True
    assert fp.product == "prometheus"
    assert fp.version == "2.53.0"
    assert fp.edition == "prometheus"
    assert fp.extras["flavour"] == "prometheus"
    assert fp.extras["revision"] == "1a2b3c4d"
    assert fp.extras["active_targets"] == 2
    assert fp.extras["firing_alerts"] == 1
    assert fp.extras["rule_groups"] == 3
    assert fp.extras["ready"] is True
    await connector.aclose()


@respx.mock
async def test_fingerprint_flavour_hint_thanos() -> None:
    _mock_fingerprint_endpoints()
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target(extras={"flavour": "thanos"}))
    assert fp.extras["flavour"] == "thanos"
    assert fp.edition == "thanos"
    await connector.aclose()


@respx.mock
async def test_fingerprint_flavour_hint_mimir() -> None:
    _mock_fingerprint_endpoints()
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target(extras={"flavour": "mimir"}))
    assert fp.extras["flavour"] == "mimir"
    await connector.aclose()


@respx.mock
async def test_fingerprint_bad_flavour_falls_back_to_prometheus() -> None:
    _mock_fingerprint_endpoints()
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target(extras={"flavour": "victoria"}))
    assert fp.extras["flavour"] == "prometheus"
    await connector.aclose()


@respx.mock
async def test_fingerprint_best_effort_augments_survive_404() -> None:
    """A backend that 404s targets/alerts/rules still fingerprints reachable."""
    respx.get(f"{_BASE}/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json=_BUILDINFO_FIXTURE)
    )
    respx.get(f"{_BASE}/-/ready").mock(return_value=httpx.Response(404))
    respx.get(f"{_BASE}/api/v1/targets").mock(return_value=httpx.Response(404))
    respx.get(f"{_BASE}/api/v1/alerts").mock(return_value=httpx.Response(404))
    respx.get(f"{_BASE}/api/v1/rules").mock(return_value=httpx.Response(404))
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target(extras={"flavour": "thanos"}))
    assert fp.reachable is True
    assert fp.version == "2.53.0"
    assert fp.extras["active_targets"] is None
    assert fp.extras["firing_alerts"] is None
    assert fp.extras["rule_groups"] is None
    assert fp.extras["ready"] is False
    await connector.aclose()


@respx.mock
async def test_fingerprint_unreachable_when_buildinfo_fails() -> None:
    respx.get(f"{_BASE}/api/v1/status/buildinfo").mock(return_value=httpx.Response(500))
    connector = PrometheusConnector()
    fp = await connector.fingerprint(_target())
    assert fp.reachable is False
    assert "error" in fp.extras
    assert fp.extras["flavour"] == "prometheus"
    await connector.aclose()


@respx.mock
async def test_probe_ok_and_failure() -> None:
    respx.get(f"{_BASE}/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json=_BUILDINFO_FIXTURE)
    )
    connector = PrometheusConnector()
    ok = await connector.probe(_target())
    assert ok.ok is True
    await connector.aclose()

    respx.get(f"{_BASE}/api/v1/status/buildinfo").mock(return_value=httpx.Response(503))
    connector2 = PrometheusConnector()
    bad = await connector2.probe(_target())
    assert bad.ok is False
    assert bad.reason
    await connector2.aclose()
