# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The chart replaces the backplane pod safely on an update (#5024).

* Default render: ``strategy: type: Recreate``, so the old pod has stopped
  before the new one starts.
* The pod's grace period covers the pre-stop wait, the drain and 30
  seconds more. The pre-stop wait is a ``sleep`` in the container, and
  both drain limits (MEHO's and uvicorn's) get the same value.
* The same holds for the Linux-server install (form 2), which renders the
  chart with its own values files.
* An upgrade with ``--reuse-values`` from a release older than these
  values still renders, with the defaults.
* An operator can still pick ``RollingUpdate``. ``Recreate`` with a
  ``rollingUpdate`` block is refused, as Kubernetes would refuse it.

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_FORM_2_VALUES = [
    _REPO_ROOT / "deploy" / "linux-server" / "packaging" / "render-values.yaml",
    _REPO_ROOT / "deploy" / "linux-server" / "values-single-node.yaml",
]

_BASE = [
    # Bring-your-own: the built-in database, login server and secret store
    # are off, as in the other chart tests (#4047).
    "--set",
    "corePostgres.enabled=false",
    "--set",
    "coreKeycloak.enabled=false",
    "--set",
    "coreVault.enabled=false",
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
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _helm(*args: str, release: str = "test") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", release, str(_CHART_DIR), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _deployment(*args: str, release: str = "test") -> dict[str, Any]:
    result = _helm(*args, release=release)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    found = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and any(c["name"] == "backplane" for c in d["spec"]["template"]["spec"]["containers"])
    ]
    assert len(found) == 1
    return found[0]


def _backplane(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "backplane")


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container["env"]}


def test_default_render_uses_recreate() -> None:
    deployment = _deployment(*_BASE)

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_default_render_waits_drains_and_has_room_for_both() -> None:
    deployment = _deployment(*_BASE)
    pod = deployment["spec"]["template"]["spec"]
    backplane = _backplane(deployment)

    assert backplane["lifecycle"] == {"preStop": {"exec": {"command": ["sleep", "5"]}}}
    env = _env(backplane)
    assert env["SHUTDOWN_DRAIN_SECONDS"] == "60"
    assert env["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"] == "60"
    assert pod["terminationGracePeriodSeconds"] == 5 + 60 + 30


def test_upgrade_from_a_release_without_the_new_values_renders_the_defaults() -> None:
    # ``helm upgrade --reuse-values`` from an older release has no shutdown
    # or updateStrategy value; null removes the key the same way.
    deployment = _deployment(*_BASE, "--set", "shutdown=null", "--set", "updateStrategy=null")
    pod = deployment["spec"]["template"]["spec"]
    backplane = _backplane(deployment)

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert pod["terminationGracePeriodSeconds"] == 95
    env = _env(backplane)
    assert env["SHUTDOWN_DRAIN_SECONDS"] == "60"
    assert env["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"] == "60"
    assert backplane["lifecycle"] == {"preStop": {"exec": {"command": ["sleep", "5"]}}}


def test_zero_shutdown_values_are_kept() -> None:
    deployment = _deployment(
        *_BASE,
        "--set",
        "shutdown.preStopSleepSeconds=0",
        "--set",
        "shutdown.drainSeconds=0",
    )
    backplane = _backplane(deployment)

    assert "lifecycle" not in backplane
    assert _env(backplane)["SHUTDOWN_DRAIN_SECONDS"] == "0"
    assert deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 30


def test_shutdown_values_set_every_limit() -> None:
    deployment = _deployment(
        *_BASE,
        "--set",
        "shutdown.preStopSleepSeconds=10",
        "--set",
        "shutdown.drainSeconds=120",
    )
    backplane = _backplane(deployment)

    assert backplane["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "10"]
    env = _env(backplane)
    assert env["SHUTDOWN_DRAIN_SECONDS"] == "120"
    assert env["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"] == "120"
    assert deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 160


def test_no_pre_stop_wait_when_it_is_zero() -> None:
    deployment = _deployment(*_BASE, "--set", "shutdown.preStopSleepSeconds=0")

    assert "lifecycle" not in _backplane(deployment)
    assert deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 90


def test_linux_server_install_uses_the_defaults() -> None:
    args: list[str] = []
    for values in _FORM_2_VALUES:
        args += ["-f", str(values)]
    # The release name, namespace and flags of the form-2 render (chart_image_refs).
    deployment = _deployment(
        *args, "--namespace", "meho", "--set", "image.tag=test", release="meho"
    )

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert "preStop" in _backplane(deployment)["lifecycle"]


def test_operator_can_pick_rolling_update() -> None:
    deployment = _deployment(
        *_BASE,
        "--set",
        "updateStrategy.type=RollingUpdate",
        "--set",
        "updateStrategy.rollingUpdate.maxSurge=0",
        "--set",
        "updateStrategy.rollingUpdate.maxUnavailable=1",
    )

    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }


@pytest.mark.parametrize(
    "bad",
    [
        [
            "--set",
            "updateStrategy.type=Recreate",
            "--set",
            "updateStrategy.rollingUpdate.maxSurge=1",
        ],
        ["--set", "updateStrategy.type=Replace"],
        ["--set", "shutdown.drainSeconds=-1"],
        ["--set", "shutdown.preStopSleepSeconds=600"],
    ],
    ids=["recreate-with-rolling-block", "unknown-type", "negative-drain", "long-pre-stop"],
)
def test_schema_refuses_wrong_values(bad: list[str]) -> None:
    result = _helm(*_BASE, *bad)

    assert result.returncode != 0
    assert "values don't meet the specifications of the schema" in result.stderr
