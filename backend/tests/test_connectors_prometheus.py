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
* **Derived numeric samples (#2871)** — ``query`` / ``query_range`` carry
  ``value_num`` / ``values_num`` per sample and ``first_sample_value`` on the
  envelope (``NaN`` / ``+-Inf`` / unparseable -> null); the raw ``value`` /
  ``values`` stay byte-identical; and a real result drives a threshold sensor
  end-to-end through the untouched checks evaluator (#2504).

The wire is mocked with ``respx``; the credential loader is injected so
Vault is never touched. Handlers are invoked directly (not through the DB
dispatcher) so the suite stays a pure unit test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.assertions import AssertionSpec, SelectSpec, ThresholdCompare
from meho_backplane.checks.evaluate import evaluate_assertion
from meho_backplane.connectors.prometheus import (
    PROMETHEUS_OPS,
    PROMETHEUS_WHEN_TO_USE_BY_GROUP,
    PrometheusConnector,
)
from meho_backplane.connectors.prometheus.connector import (
    PrometheusReadOnlyError,
    _enforce_read_only,
)
from meho_backplane.connectors.prometheus.ops_read import (
    _augment_numeric_samples,
    _numeric_sample_value,
    _sample_pair_value,
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
    # Raw wire value is left byte-identical.
    assert result["data"]["result"][0]["value"] == [1721000000, "1"]
    # Derived numerics: per-sample value_num + envelope first_sample_value.
    assert result["data"]["result"][0]["value_num"] == 1.0
    assert result["first_sample_value"] == 1.0
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
    series = result["data"]["result"][0]
    # Raw wire values are left byte-identical.
    assert series["values"] == [[1721000000, "1"], [1721000015, "1"]]
    # Derived numerics: values_num parallel to values + envelope alias.
    assert series["values_num"] == [1.0, 1.0]
    assert result["first_sample_value"] == 1.0
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


# ---------------------------------------------------------------------------
# Derived numeric samples (#2871)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", 1.0),
        ("0", 0.0),
        ("3.14", 3.14),
        ("-2.5", -2.5),
        ("1.5e9", 1.5e9),
        ("NaN", None),
        ("+Inf", None),
        ("-Inf", None),
        ("Inf", None),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_numeric_sample_value_parses_finite_floats_only(raw: str, expected: float | None) -> None:
    """Finite floats parse; NaN / +-Inf / unparseable derive to None (fail-safe null)."""
    result = _numeric_sample_value(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize("raw", [1, 1.0, None, ["1"], {"v": "1"}, True])
def test_numeric_sample_value_non_string_is_none(raw: Any) -> None:
    """Only JSON-string samples derive a number; a non-string never coerces."""
    assert _numeric_sample_value(raw) is None


@pytest.mark.parametrize(
    "sample,expected",
    [
        ([1721000000, "1"], 1.0),
        ([1721000000.5, "2.5"], 2.5),
        ([1721000000, "NaN"], None),
        ([1721000000, "oops"], None),
        ([1721000000], None),  # too short to be a sample pair
        ([1, "2", "3"], None),  # too long to be a sample pair
        ("nope", None),  # not a list
        (None, None),
        ([1721000000, 5], None),  # value slot is not a string
    ],
)
def test_sample_pair_value(sample: Any, expected: float | None) -> None:
    result = _sample_pair_value(sample)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_augment_vector_adds_value_num_and_envelope_alias() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"__name__": "up"}, "value": [1721000000, "7"]},
                {"metric": {"__name__": "up", "n": "2"}, "value": [1721000000, "NaN"]},
            ],
        },
    }
    out = _augment_numeric_samples(payload)
    assert out is payload  # mutated in place, same object returned
    assert out["data"]["result"][0]["value_num"] == 7.0
    assert out["data"]["result"][1]["value_num"] is None  # NaN -> null
    # Raw wire values left byte-identical.
    assert out["data"]["result"][0]["value"] == [1721000000, "7"]
    assert out["data"]["result"][1]["value"] == [1721000000, "NaN"]
    # Envelope alias = first sample's number.
    assert out["first_sample_value"] == 7.0


def test_augment_matrix_adds_values_num_parallel_to_values() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "up"},
                    "values": [[1721000000, "1"], [1721000015, "+Inf"], [1721000030, "3"]],
                },
                {"metric": {"n": "2"}, "values": [[1721000000, "9"]]},
            ],
        },
    }
    out = _augment_numeric_samples(payload)
    assert out["data"]["result"][0]["values_num"] == [1.0, None, 3.0]  # +Inf -> null
    assert out["data"]["result"][1]["values_num"] == [9.0]
    # Raw wire values left byte-identical.
    assert out["data"]["result"][0]["values"][1] == [1721000015, "+Inf"]
    # Envelope alias = first series' first sample.
    assert out["first_sample_value"] == 1.0


def test_augment_scalar_surfaces_only_envelope_alias() -> None:
    """A scalar result is a bare [ts, 'val'] pair -- only the envelope alias applies."""
    payload = {"status": "success", "data": {"resultType": "scalar", "result": [1721000000, "42"]}}
    out = _augment_numeric_samples(payload)
    assert out["first_sample_value"] == 42.0
    # The bare pair is untouched (no dict to hang value_num on).
    assert out["data"]["result"] == [1721000000, "42"]


def test_augment_string_result_alias_null_for_label_string() -> None:
    payload = {
        "status": "success",
        "data": {"resultType": "string", "result": [1721000000, "hello"]},
    }
    out = _augment_numeric_samples(payload)
    assert out["first_sample_value"] is None


def test_augment_empty_vector_alias_is_null() -> None:
    payload = {"status": "success", "data": {"resultType": "vector", "result": []}}
    out = _augment_numeric_samples(payload)
    assert out["first_sample_value"] is None
    assert out["data"]["result"] == []


def test_augment_error_envelope_gets_null_alias_and_is_otherwise_untouched() -> None:
    payload = {"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}
    out = _augment_numeric_samples(payload)
    assert out["first_sample_value"] is None
    assert out["status"] == "error"
    assert out["error"] == "parse error at char 3"


@respx.mock
async def test_first_sample_value_drives_threshold_sensor_end_to_end() -> None:
    """The reporter's live repro end-to-end through the untouched checks evaluator.

    ``vector(1)`` -> ``$.first_sample_value`` with ``threshold gt critical: 0``
    fires ``critical``, because the derived alias is a real float. Asserting
    the raw JSON-string path (``$.data.result[0].value[1]``) still lands in
    ``unknown`` -- the exact ergonomic gap #2871 closes -- proving the fix is
    the derived field, not any change to ``_compare_threshold`` (#2504).
    """
    respx.get(f"{_BASE}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1786369537.197, "1"]}],
                },
            },
        )
    )
    connector = PrometheusConnector()
    body = await connector.query(_operator(), _target(), {"query": "vector(1)"})
    await connector.aclose()

    now = datetime.now(UTC)
    fires = evaluate_assertion(
        AssertionSpec(
            select=SelectSpec(path="$.first_sample_value"),
            compare=ThresholdCompare(type="threshold", op="gt", critical=0),
        ),
        body,
        now=now,
    )
    assert fires.state == "critical"
    assert fires.value == 1.0

    raw = evaluate_assertion(
        AssertionSpec(
            select=SelectSpec(path="$.data.result[0].value[1]"),
            compare=ThresholdCompare(type="threshold", op="gt", critical=0),
        ),
        body,
        now=now,
    )
    assert raw.state == "unknown"
