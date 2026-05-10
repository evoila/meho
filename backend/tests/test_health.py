# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``/healthz``, ``/version``, and ``/ready``.

Coverage matrix (per Task #19 acceptance criteria):

* ``/healthz`` always returns 200, even with a failing probe registered.
* ``/version`` returns the env-injected triple (``GIT_SHA``,
  ``BUILD_DATE``) and falls back to ``"unknown"`` when env vars are
  absent. ``chart_version`` is always ``None`` at the chassis stage.
* ``/ready`` returns 503 with an empty registry (default v0.1 state),
  200 once a passing probe is registered, and 503 again with one
  failing probe registered alongside a passing one (so the failure
  detail is visible in the payload).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.health import (
    ProbeResult,
    clear_probes,
    register_probe,
    run_probes,
)
from meho_backplane.main import app


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Reset the module-level probe registry around every test.

    The registry is a module global; without this fixture, tests that
    register probes leak state into siblings and run-order becomes
    load-bearing. Clearing both before *and* after defends against
    failures that abort mid-test.
    """
    clear_probes()
    yield
    clear_probes()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Registry API surface
# ---------------------------------------------------------------------------


def test_registry_symbols_importable() -> None:
    """``register_probe`` and ``run_probes`` are part of the public API."""
    # Imports at module top would already have failed; this assertion
    # documents the contract for the acceptance-criteria reviewer.
    assert callable(register_probe)
    assert callable(run_probes)
    assert run_probes() == []


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_ignores_failing_probes(client: TestClient) -> None:
    """``/healthz`` is liveness, not readiness — registry state is irrelevant."""
    register_probe("always-fail", lambda: ProbeResult(name="always-fail", ok=False))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------


def test_version_falls_back_to_unknown(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_DATE", raising=False)

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "unknown",
        "build_date": "unknown",
        "chart_version": None,
    }


def test_version_reads_env_vars(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_DATE", "2026-05-09T12:00:00Z")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "abc1234",
        "build_date": "2026-05-09T12:00:00Z",
        "chart_version": None,
    }


def test_version_treats_empty_env_as_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty strings are as uninformative as unset; coerce to ``"unknown"``."""
    monkeypatch.setenv("GIT_SHA", "")
    monkeypatch.setenv("BUILD_DATE", "")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "unknown",
        "build_date": "unknown",
        "chart_version": None,
    }


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------


def test_ready_with_empty_registry_returns_503(client: TestClient) -> None:
    """The chassis fails closed until a downstream Initiative wires probes."""
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": []}


def test_ready_with_all_passing_probes_returns_200(client: TestClient) -> None:
    register_probe(
        "vault",
        lambda: ProbeResult(name="vault", ok=True, detail="auth ok"),
    )
    register_probe("db", lambda: ProbeResult(name="db", ok=True))

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == [
        {"name": "vault", "ok": True, "detail": "auth ok"},
        {"name": "db", "ok": True, "detail": None},
    ]


def test_ready_with_one_failing_probe_returns_503_with_detail(
    client: TestClient,
) -> None:
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe(
        "db",
        lambda: ProbeResult(name="db", ok=False, detail="migration pending"),
    )

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == [
        {"name": "vault", "ok": True, "detail": None},
        {"name": "db", "ok": False, "detail": "migration pending"},
    ]
