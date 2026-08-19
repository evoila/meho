# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the observability scrape wiring (#2885).

Initiative #2884 ships the first Prometheus Operator resources in the
chart: an optional ``ServiceMonitor`` that scrapes the backplane
``/metrics`` endpoint and an optional starter ``PrometheusRule`` with three
alerts. Both are **disabled by default** — a default install carries
neither, so a cluster without the Prometheus Operator CRDs
(``monitoring.coreos.com/v1``) is unaffected — and each is gated on its
own ``enabled`` knob.

These tests pin:

* the safe-by-default posture (neither resource renders without opt-in);
* the ServiceMonitor opt-in shape (selects this release's Service by the
  name+instance selector labels, scrapes the named service port at
  ``/metrics``, same-namespace discovery);
* the discovery-``labels`` merge both resources need so a Prometheus
  ``serviceMonitorSelector`` / ``ruleSelector`` actually picks them up;
* the starter-alert contract (the alert names, the release-scoped
  ``absent(up)`` scrape alert, the ``broadcast_publish_errors_total``
  rate alert, the #2888 background-loop liveness alert in its own
  ``meho.loops`` group, the by-concern group split, and the tunable
  ``for`` / ``severity`` / ``missedTicks`` knobs).

The authoritative chart gate is ``.github/workflows/chart.yml`` (lint +
``helm template`` + kubeconform + these render assertions). This test
mirrors the assertions at the unit layer so the regression is catchable
from ``pytest`` on any machine with ``helm`` installed; it skips cleanly
where ``helm`` is absent (the backend unit-test sandbox does not ship it
— the workflow gate covers that environment).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

# Minimal chassis-required overrides that satisfy values.schema.json.
_BASE_OVERRIDES = [
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "ingress.tls.secretName=meho-tls",
    "--set",
    "postgres.credentialsSecret=meho-postgres",
    "--set",
    "vault.address=https://vault.test",
    "--set",
    "keycloak.issuer=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakAudience=meho-backplane",
    "--set",
    "config.vaultAddr=https://vault.test",
    "--set",
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set",
    "networkPolicy.keycloakCIDR=10.0.3.0/24",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm not installed in this sandbox; chart.yml workflow gate covers CI",
)


def _render_show_only(template: str, *extra: str) -> dict[str, Any]:
    """Return the parsed single manifest for ``--show-only <template>``."""
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            *extra,
            "--show-only",
            template,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return cast("dict[str, Any]", yaml.safe_load(result.stdout))


def _render_full(*extra: str) -> str:
    """Return the full multi-document render (no ``--show-only``)."""
    result = subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _alerts(rule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten a PrometheusRule's groups into ``{alert_name: rule}``."""
    return {
        r["alert"]: r for group in rule["spec"]["groups"] for r in group["rules"] if "alert" in r
    }


def test_service_monitor_absent_by_default() -> None:
    """A default install renders NO ServiceMonitor (no operator dependency)."""
    assert "kind: ServiceMonitor" not in _render_full()


def test_prometheus_rule_absent_by_default() -> None:
    """A default install renders NO PrometheusRule."""
    assert "kind: PrometheusRule" not in _render_full()


def test_service_monitor_renders_when_enabled() -> None:
    """Opt-in renders a ServiceMonitor targeting this release's Service."""
    sm = _render_show_only("templates/servicemonitor.yaml", "--set", "serviceMonitor.enabled=true")
    assert sm["apiVersion"] == "monitoring.coreos.com/v1"
    assert sm["kind"] == "ServiceMonitor"
    # Selects THIS release's backplane Service by name + instance.
    match = sm["spec"]["selector"]["matchLabels"]
    assert match["app.kubernetes.io/name"] == "meho"
    assert match["app.kubernetes.io/instance"] == "test"
    # Same-namespace discovery.
    assert sm["spec"]["namespaceSelector"]["matchNames"] == ["default"]
    # Scrapes the named service port at /metrics.
    endpoint = sm["spec"]["endpoints"][0]
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"


def test_service_monitor_merges_discovery_labels() -> None:
    """The label a Prometheus serviceMonitorSelector matches must survive.

    Without this label a kube-prometheus-stack Prometheus silently never
    generates the scrape config — the #1 ServiceMonitor pickup gotcha.
    """
    sm = _render_show_only(
        "templates/servicemonitor.yaml",
        "--set",
        "serviceMonitor.enabled=true",
        "--set",
        "serviceMonitor.labels.release=kube-prometheus-stack",
    )
    assert sm["metadata"]["labels"]["release"] == "kube-prometheus-stack"


def test_prometheus_rule_renders_starter_alerts() -> None:
    """Opt-in renders both starter alerts under by-concern groups."""
    pr = _render_show_only("templates/prometheusrule.yaml", "--set", "prometheusRule.enabled=true")
    assert pr["apiVersion"] == "monitoring.coreos.com/v1"
    assert pr["kind"] == "PrometheusRule"
    # By-concern group split — #2888 adds a NEW group here, it does not
    # append a rule to an existing group.
    group_names = {g["name"] for g in pr["spec"]["groups"]}
    assert {"meho.scrape", "meho.broadcast"} <= group_names

    alerts = _alerts(pr)
    # Scrape-absent alert is release-scoped absent(up).
    assert (
        alerts["MehoMetricsScrapeAbsent"]["expr"]
        == 'absent(up{job="test-meho", namespace="default"})'
    )
    # Broadcast publish-error alert keys off the fail-open counter's rate.
    assert (
        "rate(broadcast_publish_errors_total[5m])" in alerts["MehoBroadcastPublishErrors"]["expr"]
    )


def test_prometheus_rule_merges_discovery_labels() -> None:
    """The label a Prometheus ruleSelector matches must survive."""
    pr = _render_show_only(
        "templates/prometheusrule.yaml",
        "--set",
        "prometheusRule.enabled=true",
        "--set",
        "prometheusRule.labels.release=kube-prometheus-stack",
    )
    assert pr["metadata"]["labels"]["release"] == "kube-prometheus-stack"


def test_alert_for_and_severity_are_tunable() -> None:
    """Alert `for` windows and severities are operator-tunable via values."""
    pr = _render_show_only(
        "templates/prometheusrule.yaml",
        "--set",
        "prometheusRule.enabled=true",
        "--set",
        "prometheusRule.scrapeAbsent.severity=warning",
        "--set",
        "prometheusRule.broadcastPublishErrors.for=30m",
    )
    alerts = _alerts(pr)
    assert alerts["MehoMetricsScrapeAbsent"]["labels"]["severity"] == "warning"
    assert alerts["MehoBroadcastPublishErrors"]["for"] == "30m"


def test_prometheus_rule_renders_loop_liveness_alert() -> None:
    """The #2888 background-loop liveness alert lands as a NEW group.

    ``meho.loops`` is added alongside ``meho.scrape`` / ``meho.broadcast``,
    not by editing an existing group. The alert thresholds each loop's
    staleness against its own published interval gauge (``N x interval``),
    so the expr references both ``background_loop_last_tick_timestamp_seconds``
    and ``background_loop_interval_seconds``, and the Prometheus
    ``$labels.loop`` templating survives Helm rendering (so Alertmanager
    gets the loop name, not a Helm parse error).
    """
    pr = _render_show_only("templates/prometheusrule.yaml", "--set", "prometheusRule.enabled=true")
    group_names = {g["name"] for g in pr["spec"]["groups"]}
    # New group, existing groups untouched.
    assert "meho.loops" in group_names
    assert {"meho.scrape", "meho.broadcast"} <= group_names

    alert = _alerts(pr)["MehoBackgroundLoopStalled"]
    expr = alert["expr"]
    assert "background_loop_last_tick_timestamp_seconds" in expr
    assert "background_loop_interval_seconds" in expr
    # Per-loop threshold = default missedTicks (10) x each loop's interval.
    assert "(10 * background_loop_interval_seconds)" in expr
    # Backtick-escaped Prometheus templating renders through Helm intact.
    assert "{{ $labels.loop }}" in alert["annotations"]["summary"]


def test_loop_liveness_alert_knobs_are_tunable() -> None:
    """``missedTicks`` / ``for`` / ``severity`` on loopLiveness flow through."""
    pr = _render_show_only(
        "templates/prometheusrule.yaml",
        "--set",
        "prometheusRule.enabled=true",
        "--set",
        "prometheusRule.loopLiveness.missedTicks=3",
        "--set",
        "prometheusRule.loopLiveness.for=1m",
        "--set",
        "prometheusRule.loopLiveness.severity=critical",
    )
    alert = _alerts(pr)["MehoBackgroundLoopStalled"]
    assert "(3 * background_loop_interval_seconds)" in alert["expr"]
    assert alert["for"] == "1m"
    assert alert["labels"]["severity"] == "critical"


def test_service_monitor_and_prometheus_rule_are_independent() -> None:
    """Each resource is gated on its own knob — enabling one is not both."""
    only_sm = _render_full("--set", "serviceMonitor.enabled=true")
    assert "kind: ServiceMonitor" in only_sm
    assert "kind: PrometheusRule" not in only_sm

    only_pr = _render_full("--set", "prometheusRule.enabled=true")
    assert "kind: PrometheusRule" in only_pr
    assert "kind: ServiceMonitor" not in only_pr
