# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G3.2-T4 (#324) K8s network + config + event ops.

Coverage matrix (per Issue #324 acceptance criteria):

* :data:`KUBERNETES_OPS` exposes the five new ops alongside the T1/T2/T5
  surface; ``register_operations`` lands every row in
  ``endpoint_descriptor``.
* :meth:`KubernetesConnector.k8s_service_list` projects every
  :class:`V1Service` through
  :func:`~meho_backplane.connectors.kubernetes.ops_network.service_row`
  and returns ``{rows, total}`` with the expected shape; ports +
  selector populated.
* :meth:`KubernetesConnector.k8s_ingress_list` projects every
  :class:`V1Ingress` through
  :func:`~meho_backplane.connectors.kubernetes.ops_network.ingress_row`;
  hosts + TLS hosts deduplicated and sorted.
* :meth:`KubernetesConnector.k8s_configmap_list` returns rows with
  ``keys`` populated and ``data``/``binary_data`` ABSENT (privacy
  contract -- pinned explicitly).
* :meth:`KubernetesConnector.k8s_configmap_info` returns the full
  configmap with data + binary_data.
* :meth:`KubernetesConnector.k8s_event_list` forwards
  ``field_selector`` to the K8s API and respects the client-side
  most-recent-first sort + ``limit`` truncation.
* Pure helpers (:func:`service_row`, :func:`service_port_row`,
  :func:`ingress_row`, :func:`ingress_rule_row`,
  :func:`ingress_path_row`, :func:`configmap_list_row`,
  :func:`configmap_info`, :func:`event_row`,
  :func:`sort_event_rows_recent_first`) pinned against synthetic
  Kubernetes model objects.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.models import (
    CoreV1Event,
    CoreV1EventSeries,
    V1ConfigMap,
    V1EventSource,
    V1HTTPIngressPath,
    V1HTTPIngressRuleValue,
    V1Ingress,
    V1IngressBackend,
    V1IngressRule,
    V1IngressServiceBackend,
    V1IngressSpec,
    V1IngressTLS,
    V1ListMeta,
    V1ObjectMeta,
    V1ObjectReference,
    V1Service,
    V1ServiceBackendPort,
    V1ServicePort,
    V1ServiceSpec,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_config import (
    CONFIG_OPS,
    configmap_info,
    configmap_list_row,
)
from meho_backplane.connectors.kubernetes.ops_events import (
    DEFAULT_EVENT_LIMIT,
    EVENT_OPS,
    MAX_EVENT_LIMIT,
    event_row,
    sort_event_rows_recent_first,
)
from meho_backplane.connectors.kubernetes.ops_network import (
    NETWORK_OPS,
    ingress_path_row,
    ingress_row,
    ingress_rule_row,
    service_port_row,
    service_row,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Required-settings env -- the connector's register_operations path imports
# the embedding service which reaches into Settings.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    """Re-register :class:`KubernetesConnector` between tests.

    Mirrors test_connectors_k8s_core.py's discipline: clear any
    cross-module registry state, then put back the K8s entries the
    package's ``__init__.py`` would have registered.
    """
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=KubernetesConnector,
    )
    yield


# ---------------------------------------------------------------------------
# Target / connector fixtures
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="rke2-meho",
    host="rke2-meho.test.invalid",
    port=6443,
    secret_ref="k8s/rke2-meho",
)


def _stub_kubeconfig() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return _stub_kubeconfig()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    """Build a non-system operator carrying a non-empty ``raw_jwt``.

    G3.10-T4 (#948) added ``operator`` to every typed handler's
    signature; the injected loader here ignores it.
    """
    return Operator(
        sub="op-net-config-test",
        name="Net/Config Test Operator",
        email=None,
        raw_jwt="op.net.jwt",
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Tests -- registration surface
# ---------------------------------------------------------------------------


def test_network_and_config_ops_in_kubernetes_ops_tuple() -> None:
    """``KUBERNETES_OPS`` now exposes the five new T4 ops."""
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    assert "k8s.service.list" in op_ids
    assert "k8s.ingress.list" in op_ids
    assert "k8s.configmap.list" in op_ids
    assert "k8s.configmap.info" in op_ids
    assert "k8s.event.list" in op_ids


def test_network_ops_metadata_shape() -> None:
    """Each network op declares safe / no-approval / read-only network tags."""
    by_id = {op.op_id: op for op in NETWORK_OPS}
    for op_id in ("k8s.service.list", "k8s.ingress.list"):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "network"
        assert op.llm_instructions is not None


def test_config_ops_metadata_shape() -> None:
    """Each configmap op declares safe / no-approval / read-only tags."""
    by_id = {op.op_id: op for op in CONFIG_OPS}
    for op_id in ("k8s.configmap.list", "k8s.configmap.info"):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "config"


def test_event_ops_metadata_shape() -> None:
    """``k8s.event.list`` lives in EVENT_OPS under group_key='events'."""
    by_id = {op.op_id: op for op in EVENT_OPS}
    events_op = by_id["k8s.event.list"]
    assert events_op.group_key == "events"
    assert "read-only" in events_op.tags
    assert events_op.safety_level == "safe"
    assert events_op.requires_approval is False


def test_handler_attr_resolves_to_bound_method() -> None:
    """Every T4 op's ``handler_attr`` points at a real async method."""
    import inspect

    for op in (*NETWORK_OPS, *CONFIG_OPS, *EVENT_OPS):
        method = getattr(KubernetesConnector, op.handler_attr, None)
        assert method is not None, f"{op.op_id!r} declares missing handler {op.handler_attr!r}"
        assert inspect.iscoroutinefunction(method), (
            f"handler {op.handler_attr!r} for {op.op_id!r} must be ``async def``"
        )


# ---------------------------------------------------------------------------
# service_row / service_port_row pure helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    name: str,
    namespace: str = "argocd",
    type_: str = "ClusterIP",
    cluster_ip: str | None = "10.43.123.45",
    external_ips: list[str] | None = None,
    ports: list[V1ServicePort] | None = None,
    selector: dict[str, str] | None = None,
) -> V1Service:
    return V1Service(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1ServiceSpec(
            type=type_,
            cluster_ip=cluster_ip,
            external_ips=external_ips or [],
            ports=ports or [],
            selector=selector or {},
        ),
    )


def test_service_port_row_flat_four_tuple() -> None:
    port = V1ServicePort(name="http", port=80, target_port=8080, protocol="TCP")
    assert service_port_row(port) == {
        "name": "http",
        "port": 80,
        "target_port": 8080,
        "protocol": "TCP",
    }


def test_service_port_row_named_target_port_preserved() -> None:
    """A named-port reference surfaces as a string, not coerced to int."""
    port = V1ServicePort(name="http", port=80, target_port="http", protocol="TCP")
    assert service_port_row(port)["target_port"] == "http"


def test_service_row_with_ports_and_selector() -> None:
    """A typical ClusterIP service projects to {name, namespace, type, ...}."""
    ports = [
        V1ServicePort(name="http", port=80, target_port=8080, protocol="TCP"),
        V1ServicePort(name="https", port=443, target_port=8083, protocol="TCP"),
    ]
    svc = _make_service(
        name="argocd-server",
        ports=ports,
        selector={"app.kubernetes.io/name": "argocd-server"},
    )
    row = service_row(svc)
    assert row["name"] == "argocd-server"
    assert row["namespace"] == "argocd"
    assert row["type"] == "ClusterIP"
    assert row["cluster_ip"] == "10.43.123.45"
    assert row["external_ips"] == []
    assert row["selector"] == {"app.kubernetes.io/name": "argocd-server"}
    assert len(row["ports"]) == 2
    assert row["ports"][0]["port"] == 80
    assert row["ports"][1]["port"] == 443


def test_service_row_handles_missing_selector() -> None:
    """An ExternalName service with no selector surfaces ``{}``, not ``None``."""
    svc = V1Service(
        metadata=V1ObjectMeta(name="external", namespace="default"),
        spec=V1ServiceSpec(type="ExternalName", external_name="db.example.com"),
    )
    row = service_row(svc)
    assert row["selector"] == {}
    assert row["ports"] == []


def test_service_row_external_ips_forwarded() -> None:
    svc = _make_service(
        name="lb",
        type_="LoadBalancer",
        external_ips=["1.2.3.4", "5.6.7.8"],
    )
    assert service_row(svc)["external_ips"] == ["1.2.3.4", "5.6.7.8"]


# ---------------------------------------------------------------------------
# ingress_row / ingress_rule_row / ingress_path_row pure helpers
# ---------------------------------------------------------------------------


def _make_ingress(
    *,
    name: str,
    namespace: str = "argocd",
    ingress_class: str | None = "nginx",
    rules: list[V1IngressRule] | None = None,
    tls: list[V1IngressTLS] | None = None,
) -> V1Ingress:
    return V1Ingress(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1IngressSpec(
            ingress_class_name=ingress_class,
            rules=rules or [],
            tls=tls or [],
        ),
    )


def _http_path(
    *,
    path: str = "/",
    path_type: str = "Prefix",
    service_name: str = "argocd-server",
    port_number: int | None = 443,
    port_name: str | None = None,
) -> V1HTTPIngressPath:
    port: V1ServiceBackendPort
    if port_number is not None:
        port = V1ServiceBackendPort(number=port_number)
    else:
        port = V1ServiceBackendPort(name=port_name)
    return V1HTTPIngressPath(
        path=path,
        path_type=path_type,
        backend=V1IngressBackend(service=V1IngressServiceBackend(name=service_name, port=port)),
    )


def test_ingress_path_row_with_numeric_port() -> None:
    path = _http_path(path="/", port_number=443)
    row = ingress_path_row(path)
    assert row == {
        "path": "/",
        "path_type": "Prefix",
        "service": "argocd-server",
        "port": 443,
    }


def test_ingress_path_row_with_named_port() -> None:
    path = _http_path(path="/api", port_number=None, port_name="http")
    assert ingress_path_row(path)["port"] == "http"


def test_ingress_rule_row_collects_paths() -> None:
    rule = V1IngressRule(
        host="argocd.evba.lab",
        http=V1HTTPIngressRuleValue(
            paths=[_http_path(path="/"), _http_path(path="/api", port_number=443)]
        ),
    )
    row = ingress_rule_row(rule)
    assert row["host"] == "argocd.evba.lab"
    assert len(row["paths"]) == 2
    assert row["paths"][0]["path"] == "/"
    assert row["paths"][1]["path"] == "/api"


def test_ingress_row_with_tls_dedup() -> None:
    rules = [
        V1IngressRule(
            host="argocd.evba.lab",
            http=V1HTTPIngressRuleValue(paths=[_http_path()]),
        ),
        V1IngressRule(
            host="argocd.evba.lab",  # duplicate host, different path
            http=V1HTTPIngressRuleValue(paths=[_http_path(path="/api")]),
        ),
    ]
    tls = [V1IngressTLS(hosts=["argocd.evba.lab"], secret_name="argocd-tls")]
    ingress = _make_ingress(name="argocd-ingress", rules=rules, tls=tls)
    row = ingress_row(ingress)
    assert row["name"] == "argocd-ingress"
    assert row["class"] == "nginx"
    # hosts deduplicated, sorted
    assert row["hosts"] == ["argocd.evba.lab"]
    assert row["tls_hosts"] == ["argocd.evba.lab"]
    assert len(row["rules"]) == 2


def test_ingress_row_no_tls_yields_empty_tls_hosts() -> None:
    ingress = _make_ingress(
        name="plain",
        rules=[
            V1IngressRule(
                host="plain.example.com",
                http=V1HTTPIngressRuleValue(paths=[_http_path()]),
            )
        ],
    )
    row = ingress_row(ingress)
    assert row["tls_hosts"] == []
    assert row["hosts"] == ["plain.example.com"]


def test_ingress_row_sorted_hosts_across_rules() -> None:
    """Hosts across rules are deduplicated and sorted alphabetically."""
    rules = [
        V1IngressRule(host="zebra.example.com", http=V1HTTPIngressRuleValue(paths=[_http_path()])),
        V1IngressRule(host="apple.example.com", http=V1HTTPIngressRuleValue(paths=[_http_path()])),
    ]
    ingress = _make_ingress(name="multi", rules=rules)
    row = ingress_row(ingress)
    assert row["hosts"] == ["apple.example.com", "zebra.example.com"]


# ---------------------------------------------------------------------------
# configmap_list_row / configmap_info pure helpers -- PRIVACY CONTRACT
# ---------------------------------------------------------------------------


def _make_configmap(
    *,
    name: str,
    namespace: str = "argocd",
    data: dict[str, str] | None = None,
    binary_data: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    created: datetime | None = None,
) -> V1ConfigMap:
    return V1ConfigMap(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations=annotations,
            creation_timestamp=created,
        ),
        data=data,
        binary_data=binary_data,
    )


def test_configmap_list_row_keys_only_no_data_field() -> None:
    """Critical privacy contract: list row carries ``keys``, never ``data``."""
    cm = _make_configmap(
        name="argocd-cm",
        data={"repositories": "secret-repo-url", "url": "https://argocd.evba.lab"},
    )
    row = configmap_list_row(cm)
    assert row["name"] == "argocd-cm"
    assert row["namespace"] == "argocd"
    assert row["keys"] == ["repositories", "url"]
    # The privacy contract: NO data, NO binary_data on the row.
    assert "data" not in row
    assert "binary_data" not in row
    # Values must not leak via any other field on the row. ``in`` on a
    # list checks element-equality; the privacy contract is substring
    # absence, so list-typed values are flattened element-by-element
    # and each string element is substring-scanned.
    for value in row.values():
        if isinstance(value, str):
            assert "secret-repo-url" not in value
        elif isinstance(value, list):
            assert not any(isinstance(elem, str) and "secret-repo-url" in elem for elem in value)


def test_configmap_list_row_merges_data_and_binary_data_keys() -> None:
    """``keys`` is the sorted union of ``data`` + ``binary_data`` keys."""
    cm = _make_configmap(
        name="mixed",
        data={"text-key": "value"},
        binary_data={"binary-key": "base64=="},
    )
    row = configmap_list_row(cm)
    assert row["keys"] == ["binary-key", "text-key"]


def test_configmap_list_row_empty_configmap() -> None:
    cm = _make_configmap(name="empty")
    row = configmap_list_row(cm)
    assert row["keys"] == []


def test_configmap_list_row_age_seconds_set() -> None:
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    cm = _make_configmap(name="aged", created=now - timedelta(seconds=86400))
    row = configmap_list_row(cm, now=now)
    assert row["age_seconds"] == 86400


def test_configmap_info_includes_full_data_and_binary_data() -> None:
    """``info`` is the counterpart -- the targeted-read shape carries values."""
    cm = _make_configmap(
        name="argocd-cm",
        data={"url": "https://argocd.evba.lab"},
        binary_data={"cert": "base64-encoded-bytes"},
        labels={"app": "argocd"},
        annotations={"managed-by": "helm"},
    )
    info = configmap_info(cm)
    assert info["name"] == "argocd-cm"
    assert info["namespace"] == "argocd"
    assert info["data"] == {"url": "https://argocd.evba.lab"}
    assert info["binary_data"] == {"cert": "base64-encoded-bytes"}
    assert info["metadata"]["labels"] == {"app": "argocd"}
    assert info["metadata"]["annotations"] == {"managed-by": "helm"}


def test_configmap_info_empty_data_surfaces_empty_dicts() -> None:
    cm = _make_configmap(name="empty")
    info = configmap_info(cm)
    assert info["data"] == {}
    assert info["binary_data"] == {}


# ---------------------------------------------------------------------------
# event_row + sort helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    name: str = "evt-1",
    namespace: str = "argocd",
    type_: str = "Warning",
    reason: str = "BackOff",
    message: str = "Back-off restarting failed container",
    involved_kind: str = "Pod",
    involved_name: str = "argocd-server-xyz",
    involved_namespace: str = "argocd",
    source_component: str | None = "kubelet",
    count: int | None = 42,
    first_timestamp: datetime | None = None,
    last_timestamp: datetime | None = None,
    event_time: datetime | None = None,
    series: CoreV1EventSeries | None = None,
) -> CoreV1Event:
    return CoreV1Event(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        type=type_,
        reason=reason,
        message=message,
        involved_object=V1ObjectReference(
            kind=involved_kind, name=involved_name, namespace=involved_namespace
        ),
        source=V1EventSource(component=source_component) if source_component else None,
        count=count,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        event_time=event_time,
        series=series,
    )


def test_event_row_classic_shape() -> None:
    """A pre-1.27-style event with first/last timestamps + count + source."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    last = now - timedelta(seconds=60)
    first = now - timedelta(seconds=7200)
    event = _make_event(
        first_timestamp=first,
        last_timestamp=last,
    )
    row = event_row(event, now=now)
    assert row["type"] == "Warning"
    assert row["reason"] == "BackOff"
    assert row["count"] == 42
    assert row["source"] == "kubelet"
    assert row["first_seen_seconds"] == 7200
    assert row["last_seen_seconds"] == 60
    assert row["involved_object"] == {
        "kind": "Pod",
        "name": "argocd-server-xyz",
        "namespace": "argocd",
    }


def test_event_row_event_time_fallback_for_eventseries_shape() -> None:
    """The 1.27+ EventSeries shape uses ``event_time`` when last_timestamp is unset."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    event = _make_event(
        first_timestamp=None,
        last_timestamp=None,
        event_time=now - timedelta(seconds=120),
        count=None,
    )
    row = event_row(event, now=now)
    assert row["last_seen_seconds"] == 120
    assert row["first_seen_seconds"] == 120
    # ``count=None`` AND ``series=None`` coerces to 1 (singleton event).
    assert row["count"] == 1


def test_event_row_count_from_event_series_when_flat_count_unset() -> None:
    """K8s 1.27+ EventSeries: authoritative count lives on ``series.count``.

    Regression for M1 (PR #561): when ``event.count`` is None but the
    event carries a populated ``series`` object, the row's ``count``
    must reflect ``series.count`` (the recurring-event occurrence
    total), not the 1-fallback used for singletons. ``kubectl
    describe`` shows the series count for these events; the connector
    must agree.
    """
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    series = CoreV1EventSeries(
        count=47,
        last_observed_time=now - timedelta(seconds=30),
    )
    event = _make_event(
        first_timestamp=None,
        last_timestamp=None,
        event_time=now - timedelta(seconds=3600),
        count=None,
        series=series,
    )
    row = event_row(event, now=now)
    # series.count wins over the None flat count.
    assert row["count"] == 47
    # And series.last_observed_time wins over event_time for last_seen.
    assert row["last_seen_seconds"] == 30


def test_event_row_count_prefers_series_count_over_flat_count() -> None:
    """Disagreement case: when both surfaces carry a count, the series
    count is authoritative.

    The wire reality is that ``event.count`` is left at its
    pre-series value (frozen at the moment the API server upgraded
    the event into a series); ``event.series.count`` is the live,
    updated tally. Operators reading ``kubectl describe`` see the
    series count.
    """
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    series = CoreV1EventSeries(
        count=99,
        last_observed_time=now - timedelta(seconds=10),
    )
    event = _make_event(
        last_timestamp=now - timedelta(seconds=10),
        first_timestamp=now - timedelta(seconds=3600),
        count=3,
        series=series,
    )
    row = event_row(event, now=now)
    assert row["count"] == 99


def test_event_row_missing_involved_object_surfaces_none_fields() -> None:
    """An event with no involvedObject still produces a stable row shape.

    The K8s API model rejects ``involved_object=None`` on construction
    (client-side validation), but the over-the-wire deserialiser can
    land it as None on synthetic / malformed responses; the helper
    must be robust either way. Use a MagicMock to bypass the
    constructor's None-guard for this defence-in-depth case.
    """
    event = MagicMock(spec=CoreV1Event)
    event.metadata = V1ObjectMeta(name="orphan", namespace="default")
    event.type = "Normal"
    event.reason = "X"
    event.message = "m"
    event.involved_object = None
    event.source = None
    event.reporting_component = ""
    event.count = 1
    event.first_timestamp = None
    event.last_timestamp = None
    event.event_time = None
    event.series = None
    row = event_row(event)
    assert row["involved_object"] == {"kind": None, "name": None, "namespace": None}


def test_sort_event_rows_recent_first_orders_by_last_seen() -> None:
    rows = [
        {"name": "old", "last_seen_seconds": 7200},
        {"name": "recent", "last_seen_seconds": 60},
        {"name": "middle", "last_seen_seconds": 600},
    ]
    sorted_rows = sort_event_rows_recent_first(rows)
    assert [r["name"] for r in sorted_rows] == ["recent", "middle", "old"]


def test_sort_event_rows_untimestamped_sort_last() -> None:
    """Rows with ``last_seen_seconds=None`` sort to the end deterministically."""
    rows = [
        {"name": "no-ts", "last_seen_seconds": None},
        {"name": "recent", "last_seen_seconds": 5},
    ]
    sorted_rows = sort_event_rows_recent_first(rows)
    assert [r["name"] for r in sorted_rows] == ["recent", "no-ts"]


# ---------------------------------------------------------------------------
# k8s_service_list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_service_list_returns_rows_and_total() -> None:
    """``k8s.service.list --namespace argocd`` returns services with ports + selector."""
    ports = [V1ServicePort(name="http", port=80, target_port=8080, protocol="TCP")]
    services = [
        _make_service(name="argocd-server", ports=ports, selector={"app": "argocd-server"}),
        _make_service(name="argocd-repo-server", ports=ports, selector={"app": "repo-server"}),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = services
        list_resp.metadata = V1ListMeta()
        core_v1_cls.return_value.list_namespaced_service = AsyncMock(return_value=list_resp)
        result = await connector.k8s_service_list(
            _make_operator(), _TARGET, {"namespace": "argocd"}
        )

        core_v1_cls.return_value.list_namespaced_service.assert_awaited_once_with(
            namespace="argocd"
        )

    assert result["total"] == 2
    names = [r["name"] for r in result["rows"]]
    assert names == ["argocd-server", "argocd-repo-server"]
    for row in result["rows"]:
        assert row["ports"][0]["port"] == 80
        assert row["selector"]


@pytest.mark.asyncio
async def test_k8s_service_list_empty_namespace() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_namespaced_service = AsyncMock(return_value=list_resp)
        result = await connector.k8s_service_list(_make_operator(), _TARGET, {"namespace": "empty"})
    assert result == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# k8s_ingress_list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_ingress_list_returns_hosts_and_tls() -> None:
    """``k8s.ingress.list --namespace argocd`` returns ingresses with hosts + TLS."""
    ingress = _make_ingress(
        name="argocd-ingress",
        rules=[
            V1IngressRule(
                host="argocd.evba.lab",
                http=V1HTTPIngressRuleValue(paths=[_http_path()]),
            )
        ],
        tls=[V1IngressTLS(hosts=["argocd.evba.lab"], secret_name="argocd-tls")],
    )
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.NetworkingV1Api"
        ) as networking_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [ingress]
        networking_cls.return_value.list_namespaced_ingress = AsyncMock(return_value=list_resp)
        result = await connector.k8s_ingress_list(
            _make_operator(), _TARGET, {"namespace": "argocd"}
        )

        networking_cls.return_value.list_namespaced_ingress.assert_awaited_once_with(
            namespace="argocd"
        )

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["name"] == "argocd-ingress"
    assert row["hosts"] == ["argocd.evba.lab"]
    assert row["tls_hosts"] == ["argocd.evba.lab"]


# ---------------------------------------------------------------------------
# k8s_configmap_list handler -- PRIVACY CONTRACT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_configmap_list_keys_only_data_absent() -> None:
    """``k8s.configmap.list --namespace argocd`` returns ``keys`` populated and ``data`` ABSENT."""
    cm = _make_configmap(
        name="argocd-cm",
        data={
            "repositories": "https://github.com/argoproj/argocd-example-apps",
            "url": "https://argocd.evba.lab",
            "policy.default": "role:readonly",
        },
    )
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [cm]
        core_v1_cls.return_value.list_namespaced_config_map = AsyncMock(return_value=list_resp)
        result = await connector.k8s_configmap_list(
            _make_operator(), _TARGET, {"namespace": "argocd"}
        )

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["name"] == "argocd-cm"
    assert row["keys"] == ["policy.default", "repositories", "url"]
    # Privacy contract: NO data, NO binary_data on the list row.
    assert "data" not in row
    assert "binary_data" not in row


# ---------------------------------------------------------------------------
# k8s_configmap_info handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_configmap_info_returns_full_data_and_binary_data() -> None:
    """``k8s.configmap.info argocd-cm --namespace argocd`` returns full data + binary_data."""
    cm = _make_configmap(
        name="argocd-cm",
        data={"repositories": "...", "url": "https://argocd.evba.lab"},
        binary_data={"cert.pem": "base64-bytes"},
        labels={"app": "argocd"},
        annotations={"managed-by": "helm"},
    )
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.read_namespaced_config_map = AsyncMock(return_value=cm)
        result = await connector.k8s_configmap_info(
            _make_operator(), _TARGET, {"name": "argocd-cm", "namespace": "argocd"}
        )

        core_v1_cls.return_value.read_namespaced_config_map.assert_awaited_once_with(
            name="argocd-cm", namespace="argocd"
        )

    assert result["data"] == {"repositories": "...", "url": "https://argocd.evba.lab"}
    assert result["binary_data"] == {"cert.pem": "base64-bytes"}
    assert result["metadata"]["labels"] == {"app": "argocd"}


# ---------------------------------------------------------------------------
# k8s_event_list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_event_list_field_selector_forwarded() -> None:
    """``k8s.event.list --namespace argocd --field-selector type=Warning`` filters correctly."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    warn = _make_event(
        name="warn-1",
        type_="Warning",
        last_timestamp=now - timedelta(seconds=60),
        first_timestamp=now - timedelta(seconds=3600),
    )
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [warn]
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        result = await connector.k8s_event_list(
            _make_operator(),
            _TARGET,
            {"namespace": "argocd", "field_selector": "type=Warning"},
        )

        kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
        assert kwargs["namespace"] == "argocd"
        assert kwargs["field_selector"] == "type=Warning"
        # The wire request always asks for MAX_EVENT_LIMIT; the
        # caller's (or default) ``limit`` is the post-sort truncation
        # bound, not the API-side limit. See the
        # ``test_k8s_event_list_fetches_max_then_sorts_client_side``
        # regression for rationale.
        assert kwargs["limit"] == MAX_EVENT_LIMIT

    assert result["total"] == 1
    assert result["rows"][0]["type"] == "Warning"


@pytest.mark.asyncio
async def test_k8s_event_list_default_limit() -> None:
    """Without an explicit ``limit``, the post-sort truncation uses DEFAULT_EVENT_LIMIT.

    The wire request still asks for the MAX_EVENT_LIMIT superset (so
    the client-side recency sort is correct); the truncation that
    follows uses DEFAULT_EVENT_LIMIT when the caller didn't override
    ``limit``. This test pins both: the wire-side cap and the
    result-side default truncation.
    """
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    # Build DEFAULT_EVENT_LIMIT+10 events so the default truncation
    # actually fires; without enough rows the test would silently
    # pass for any default value.
    api_events: list[CoreV1Event] = [
        _make_event(
            name=f"evt-{i}",
            last_timestamp=now - timedelta(seconds=10 + i),
            first_timestamp=now - timedelta(seconds=3600),
        )
        for i in range(DEFAULT_EVENT_LIMIT + 10)
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = api_events
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        result = await connector.k8s_event_list(_make_operator(), _TARGET, {"namespace": "argocd"})
        kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
        # Wire: always MAX_EVENT_LIMIT.
        assert kwargs["limit"] == MAX_EVENT_LIMIT
        # No field_selector kwarg when caller didn't pass one.
        assert "field_selector" not in kwargs

    # Post-sort truncation: DEFAULT_EVENT_LIMIT.
    assert result["total"] == DEFAULT_EVENT_LIMIT
    assert len(result["rows"]) == DEFAULT_EVENT_LIMIT


@pytest.mark.asyncio
async def test_k8s_event_list_limit_respects_value_and_ordered_by_last_seen() -> None:
    """``--limit 10`` respects the limit; rows ordered by last_seen descending.

    The handler's ``event_row`` computes ``last_seen_seconds`` against
    ``datetime.now(UTC)`` (no ``now`` seam at the handler layer -- it
    flows through ``ops_events.event_row``'s default branch). The test
    is invariant to the wall clock: it constructs events whose
    last_timestamps are evenly spaced from a moving reference and
    asserts the **relative ordering** + post-sort name sequence.

    Also pins the wire-side contract: the handler must request
    ``MAX_EVENT_LIMIT`` rows from the API regardless of the caller's
    ``limit``, because the K8s events endpoint has no server-side
    last-seen ordering guarantee. See
    ``test_k8s_event_list_fetches_max_then_sorts_client_side`` for
    the dedicated regression on that wire contract.
    """
    # Use a recent reference so the elapsed-seconds values are bounded
    # and never negative regardless of test latency.
    reference = datetime.now(UTC)
    # Event ``evt-i`` last fired ``10 + 10*i`` seconds before reference,
    # so evt-0 is the most recent and evt-11 is the oldest.
    api_events: list[CoreV1Event] = [
        _make_event(
            name=f"evt-{i}",
            last_timestamp=reference - timedelta(seconds=10 + 10 * i),
            first_timestamp=reference - timedelta(seconds=10 + 10 * i + 600),
        )
        for i in range(12)
    ]
    # Shuffle order so the test catches a missing sort.
    random.Random(42).shuffle(api_events)

    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = api_events
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        result = await connector.k8s_event_list(
            _make_operator(), _TARGET, {"namespace": "argocd", "limit": 10}
        )
        kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
        # The handler always pulls up to MAX_EVENT_LIMIT, never the
        # caller's smaller ``limit`` -- the recency sort needs the
        # superset to be correct.
        assert kwargs["limit"] == MAX_EVENT_LIMIT

    # Limit truncates to 10 most-recent (after sort).
    assert result["total"] == 10
    seen = [row["last_seen_seconds"] for row in result["rows"]]
    # Ascending last_seen_seconds = descending real-time order (smaller =
    # more recent). Sorted output proves the client-side sort happened.
    assert seen == sorted(seen)
    # The 10 kept rows are the 10 most-recent input events (evt-0..evt-9).
    names = [row["name"] for row in result["rows"]]
    assert names == [f"evt-{i}" for i in range(10)]


@pytest.mark.asyncio
async def test_k8s_event_list_fetches_max_then_sorts_client_side() -> None:
    """Regression for the "most-recent-N" acceptance criterion (B1).

    The K8s events endpoint returns rows in resource-version order
    with **no server-side ordering guarantee** by ``lastTimestamp`` --
    see
    https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions.
    If the handler passed the caller's ``limit`` straight to the API,
    the server-truncated subset would not contain the actual N most
    recent events. The handler therefore must request up to
    :data:`MAX_EVENT_LIMIT` rows, sort by ``last_seen_seconds``
    client-side, then truncate to the caller's ``limit``.

    This test mocks the API to return 25 events deliberately
    creation-ordered so the "wrong" prefix (the first 5) is **not**
    the recency-correct answer. With ``limit=5`` the kept rows must
    be the 5 most-recent by last_seen, not the first 5 the mock
    returned.
    """
    reference = datetime.now(UTC)
    # Build 25 events where ``evt-{i}`` last fired ``10 + 10*i`` seconds
    # ago, so evt-0 is most recent and evt-24 oldest. Return them in
    # *reverse* recency order from the mock API so a naive
    # "trust the API order + truncate" implementation would keep the
    # 5 OLDEST instead of the 5 newest.
    api_events: list[CoreV1Event] = [
        _make_event(
            name=f"evt-{i}",
            last_timestamp=reference - timedelta(seconds=10 + 10 * i),
            first_timestamp=reference - timedelta(seconds=10 + 10 * i + 600),
        )
        for i in range(25)
    ]
    api_events.reverse()  # API returns oldest-first; sort must overcome this.

    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = api_events
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        result = await connector.k8s_event_list(
            _make_operator(), _TARGET, {"namespace": "argocd", "limit": 5}
        )
        kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
        # Wire-side: ask for the superset, not the caller's limit.
        assert kwargs["limit"] == MAX_EVENT_LIMIT

    # Client-side: kept rows are the 5 truly most-recent (evt-0..evt-4),
    # not the 5 the API server returned at the head of its response.
    assert result["total"] == 5
    names = [row["name"] for row in result["rows"]]
    assert names == [f"evt-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_k8s_event_list_limit_clamps_at_max() -> None:
    """Defence in depth: clamp at MAX_EVENT_LIMIT if schema lets a larger value through."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        await connector.k8s_event_list(
            _make_operator(), _TARGET, {"namespace": "argocd", "limit": 99999}
        )
        kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
        # The API request always asks for MAX_EVENT_LIMIT regardless
        # of how the caller's ``limit`` was clamped -- the clamp
        # affects the post-sort truncation, not the wire request.
        assert kwargs["limit"] == MAX_EVENT_LIMIT


# ---------------------------------------------------------------------------
# G0.17-T1 (#1330): request-shape parity sweep
#
# The pod / deployment list ops use a shared ``namespace`` XOR
# ``all_namespaces`` clause + ``label_selector`` forwarding (covered by
# ``test_list_schemas_enforce_namespace_xor_all_namespaces`` in
# ``test_connectors_k8s_workload.py``). G0.17-T1 (#1330) converged the
# event / service / ingress / configmap list ops onto the same shape;
# the tests below pin that contract.
# ---------------------------------------------------------------------------


#: The four ops G0.17-T1 (#1330) widened to the shared request shape.
#: ``test_list_schemas_enforce_namespace_xor_all_namespaces`` in
#: ``test_connectors_k8s_workload.py`` covers ``k8s.pod.list`` and
#: ``k8s.deployment.list``; this parametrization extends the parity
#: contract to the rest of the namespaced list-op family on this
#: connector.
_G0_17_T1_LIST_OP_IDS: tuple[str, ...] = (
    "k8s.event.list",
    "k8s.service.list",
    "k8s.ingress.list",
    "k8s.configmap.list",
)


@pytest.mark.parametrize("op_id", _G0_17_T1_LIST_OP_IDS)
def test_list_schemas_enforce_namespace_xor_all_namespaces(op_id: str) -> None:
    """Every G0.17-T1 list op enforces ``namespace`` XOR ``all_namespaces``.

    Mirrors the workload-list test (``test_connectors_k8s_workload.py:
    test_list_schemas_enforce_namespace_xor_all_namespaces``) but
    extended to the event / service / ingress / configmap list ops
    via the shared schema building blocks in :mod:`ops_listparams`.
    See ``docs/codebase/api-shape-conventions.md`` §10 for the
    convention.
    """
    from jsonschema import Draft202012Validator

    op = next(op for op in KUBERNETES_OPS if op.op_id == op_id)
    validator = Draft202012Validator(op.parameter_schema)
    # Valid: namespace alone.
    assert validator.is_valid({"namespace": "argocd"})
    # Valid: all_namespaces=true alone.
    assert validator.is_valid({"all_namespaces": True})
    # Invalid: neither.
    assert not validator.is_valid({})
    # Invalid: both, with all_namespaces=true (conflict).
    assert not validator.is_valid({"namespace": "argocd", "all_namespaces": True})
    # Valid: both, with all_namespaces=false (effectively just namespace).
    assert validator.is_valid({"namespace": "argocd", "all_namespaces": False})
    # Valid: label_selector forwarded alongside namespace.
    assert validator.is_valid({"namespace": "argocd", "label_selector": "app=argocd-server"})
    # Valid: label_selector forwarded alongside all_namespaces.
    assert validator.is_valid({"all_namespaces": True, "label_selector": "app in (a,b)"})


# ---------------------------------------------------------------------------
# Per-op all_namespaces dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_event_list_all_namespaces_uses_for_all_namespaces() -> None:
    """``all_namespaces=true`` routes through ``list_event_for_all_namespaces``.

    Mirrors ``test_k8s_pod_list_all_namespaces_uses_for_all_namespaces``
    in ``test_connectors_k8s_workload.py`` for the event op (G0.17-T1
    #1330).
    """
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_event_for_all_namespaces = AsyncMock(return_value=list_resp)
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        await connector.k8s_event_list(_make_operator(), _TARGET, {"all_namespaces": True})

    core_v1_cls.return_value.list_event_for_all_namespaces.assert_awaited_once()
    core_v1_cls.return_value.list_namespaced_event.assert_not_awaited()
    # Wire still asks for MAX_EVENT_LIMIT on the all-namespaces path.
    kwargs = core_v1_cls.return_value.list_event_for_all_namespaces.call_args.kwargs
    assert kwargs["limit"] == MAX_EVENT_LIMIT
    # No namespace passed on the cluster-wide call.
    assert "namespace" not in kwargs


@pytest.mark.asyncio
async def test_k8s_event_list_label_selector_flows_through() -> None:
    """``label_selector`` forwards to the API as ``label_selector=...``."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_namespaced_event = AsyncMock(return_value=list_resp)
        await connector.k8s_event_list(
            _make_operator(),
            _TARGET,
            {"namespace": "argocd", "label_selector": "app=argocd-server"},
        )
    kwargs = core_v1_cls.return_value.list_namespaced_event.call_args.kwargs
    assert kwargs["label_selector"] == "app=argocd-server"


@pytest.mark.asyncio
async def test_k8s_service_list_all_namespaces_uses_for_all_namespaces() -> None:
    """``all_namespaces=true`` routes through ``list_service_for_all_namespaces``."""
    services = [
        _make_service(name="svc-1", namespace="argocd"),
        _make_service(name="svc-2", namespace="kube-system"),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = services
        list_resp.metadata = V1ListMeta()
        core_v1_cls.return_value.list_service_for_all_namespaces = AsyncMock(return_value=list_resp)
        core_v1_cls.return_value.list_namespaced_service = AsyncMock()
        result = await connector.k8s_service_list(
            _make_operator(), _TARGET, {"all_namespaces": True}
        )

    assert result["total"] == 2
    core_v1_cls.return_value.list_service_for_all_namespaces.assert_awaited_once()
    core_v1_cls.return_value.list_namespaced_service.assert_not_awaited()
    # No namespace kwarg on the cluster-wide call.
    kwargs = core_v1_cls.return_value.list_service_for_all_namespaces.call_args.kwargs
    assert "namespace" not in kwargs


@pytest.mark.asyncio
async def test_k8s_service_list_label_selector_flows_through() -> None:
    """``label_selector`` forwards to the API on the per-namespace path."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_namespaced_service = AsyncMock(return_value=list_resp)
        await connector.k8s_service_list(
            _make_operator(),
            _TARGET,
            {"namespace": "argocd", "label_selector": "app=argocd-server"},
        )
    kwargs = core_v1_cls.return_value.list_namespaced_service.call_args.kwargs
    assert kwargs["label_selector"] == "app=argocd-server"
    assert kwargs["namespace"] == "argocd"


@pytest.mark.asyncio
async def test_k8s_ingress_list_all_namespaces_uses_for_all_namespaces() -> None:
    """``all_namespaces=true`` routes through ``list_ingress_for_all_namespaces``."""
    ingress = _make_ingress(
        name="ing-1",
        rules=[
            V1IngressRule(
                host="argocd.evba.lab",
                http=V1HTTPIngressRuleValue(paths=[_http_path()]),
            )
        ],
    )
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.NetworkingV1Api"
        ) as networking_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [ingress]
        networking_cls.return_value.list_ingress_for_all_namespaces = AsyncMock(
            return_value=list_resp
        )
        networking_cls.return_value.list_namespaced_ingress = AsyncMock()
        result = await connector.k8s_ingress_list(
            _make_operator(), _TARGET, {"all_namespaces": True}
        )

    assert result["total"] == 1
    networking_cls.return_value.list_ingress_for_all_namespaces.assert_awaited_once()
    networking_cls.return_value.list_namespaced_ingress.assert_not_awaited()
    kwargs = networking_cls.return_value.list_ingress_for_all_namespaces.call_args.kwargs
    assert "namespace" not in kwargs


@pytest.mark.asyncio
async def test_k8s_ingress_list_label_selector_flows_through() -> None:
    """``label_selector`` forwards to the API on the per-namespace path."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.NetworkingV1Api"
        ) as networking_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        networking_cls.return_value.list_namespaced_ingress = AsyncMock(return_value=list_resp)
        await connector.k8s_ingress_list(
            _make_operator(),
            _TARGET,
            {"namespace": "argocd", "label_selector": "app=argocd-server"},
        )
    kwargs = networking_cls.return_value.list_namespaced_ingress.call_args.kwargs
    assert kwargs["label_selector"] == "app=argocd-server"
    assert kwargs["namespace"] == "argocd"


@pytest.mark.asyncio
async def test_k8s_configmap_list_all_namespaces_uses_for_all_namespaces() -> None:
    """``all_namespaces=true`` routes through ``list_config_map_for_all_namespaces``.

    Privacy contract still holds on the all-namespaces path: keys
    only, no values. The list_row helper enforces it for both branches.
    """
    cms = [
        _make_configmap(name="cm-1", namespace="argocd", data={"k": "v"}),
        _make_configmap(name="cm-2", namespace="kube-system", data={"k2": "v2"}),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = cms
        core_v1_cls.return_value.list_config_map_for_all_namespaces = AsyncMock(
            return_value=list_resp
        )
        core_v1_cls.return_value.list_namespaced_config_map = AsyncMock()
        result = await connector.k8s_configmap_list(
            _make_operator(), _TARGET, {"all_namespaces": True}
        )

    assert result["total"] == 2
    core_v1_cls.return_value.list_config_map_for_all_namespaces.assert_awaited_once()
    core_v1_cls.return_value.list_namespaced_config_map.assert_not_awaited()
    kwargs = core_v1_cls.return_value.list_config_map_for_all_namespaces.call_args.kwargs
    assert "namespace" not in kwargs
    # Privacy contract preserved on the cluster-wide path.
    for row in result["rows"]:
        assert "data" not in row
        assert "binary_data" not in row


@pytest.mark.asyncio
async def test_k8s_configmap_list_label_selector_flows_through() -> None:
    """``label_selector`` forwards to the API on the per-namespace path."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = []
        core_v1_cls.return_value.list_namespaced_config_map = AsyncMock(return_value=list_resp)
        await connector.k8s_configmap_list(
            _make_operator(),
            _TARGET,
            {"namespace": "argocd", "label_selector": "managed-by=helm"},
        )
    kwargs = core_v1_cls.return_value.list_namespaced_config_map.call_args.kwargs
    assert kwargs["label_selector"] == "managed-by=helm"
    assert kwargs["namespace"] == "argocd"
