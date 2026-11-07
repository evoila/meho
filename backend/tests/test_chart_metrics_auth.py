# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the metrics token (``metricsAuth``).

By default the chart makes the Secret ``<release>-metrics-auth``, hands the
token to the backplane as ``METRICS_AUTH_TOKEN`` by ``secretKeyRef``, and
makes the ServiceMonitor send it as a bearer token. An operator-named
Secret replaces the chart-made one, and ``metricsAuth.enabled: false``
renders none of it. Skips where ``helm`` is absent, like the other chart
render tests.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest
import yaml

from tests.test_chart_observability_scrape import _render_full, _render_show_only

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _backplane_env(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    backplane = next(c for c in containers if c["name"] == "backplane")
    return {env["name"]: env for env in backplane["env"]}


def test_secret_is_made_by_default() -> None:
    secret = _render_show_only("templates/metrics-auth-secret.yaml")
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "test-metrics-auth"
    assert set(secret["data"]) == {"token"}


def test_backplane_reads_the_token_by_secret_key_ref() -> None:
    deployment = _render_show_only("templates/deployment.yaml")
    env = _backplane_env(deployment)["METRICS_AUTH_TOKEN"]
    assert "value" not in env
    assert env["valueFrom"]["secretKeyRef"] == {"name": "test-metrics-auth", "key": "token"}
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/metrics-auth" in annotations


def test_service_monitor_sends_the_token() -> None:
    sm = _render_show_only("templates/servicemonitor.yaml", "--set", "serviceMonitor.enabled=true")
    endpoint = sm["spec"]["endpoints"][0]
    assert endpoint["authorization"] == {
        "type": "Bearer",
        "credentials": {"name": "test-metrics-auth", "key": "token"},
    }
    assert "bearerTokenSecret" not in endpoint


def test_operator_named_secret_replaces_the_chart_made_one() -> None:
    extra = ["--set", "metricsAuth.existingSecret=my-metrics", "--set", "metricsAuth.secretKey=tok"]
    rendered = _render_full(*extra, "--set", "serviceMonitor.enabled=true")
    assert "name: test-metrics-auth" not in rendered
    deployment = _render_show_only("templates/deployment.yaml", *extra)
    env = _backplane_env(deployment)["METRICS_AUTH_TOKEN"]
    assert env["valueFrom"]["secretKeyRef"] == {"name": "my-metrics", "key": "tok"}
    assert "checksum/metrics-auth" not in deployment["spec"]["template"]["metadata"]["annotations"]
    sm = _render_show_only(
        "templates/servicemonitor.yaml", *extra, "--set", "serviceMonitor.enabled=true"
    )
    assert sm["spec"]["endpoints"][0]["authorization"]["credentials"] == {
        "name": "my-metrics",
        "key": "tok",
    }


def test_disabled_renders_no_token() -> None:
    rendered = _render_full(
        "--set", "metricsAuth.enabled=false", "--set", "serviceMonitor.enabled=true"
    )
    assert "metrics-auth" not in rendered
    assert "METRICS_AUTH_TOKEN" not in rendered
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ServiceMonitor":
            assert "authorization" not in doc["spec"]["endpoints"][0]
