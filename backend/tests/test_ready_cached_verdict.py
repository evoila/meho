# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ready`` runs the probes at most once per freshness window.

The probes do live network calls and ``/ready`` needs no login, so a
burst of calls must share one probe sweep. The cached part is the verdict
and the checks; ``sandbox_mode`` and ``features`` are added per call.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane import health
from meho_backplane.health import ProbeResult, clear_probes, register_probe
from meho_backplane.main import app
from meho_backplane.settings import get_settings

_TOKEN = "s3cr3t-scrape-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)
    get_settings.cache_clear()
    clear_probes()
    yield
    clear_probes()
    get_settings.cache_clear()


class _CountingProbe:
    def __init__(self, *, ok: bool = True) -> None:
        self.runs = 0
        self.ok = ok

    def __call__(self) -> ProbeResult:
        self.runs += 1
        return ProbeResult(name="counted", ok=self.ok)


def test_burst_of_calls_runs_the_probes_once() -> None:
    probe = _CountingProbe()
    register_probe("counted", probe)
    client = TestClient(app)

    bodies = [client.get("/ready").json() for _ in range(5)]

    assert probe.runs == 1
    assert all(body["status"] == "ready" for body in bodies)
    expected = [{"name": "counted", "ok": True, "detail": None}]
    assert all(body["checks"] == expected for body in bodies)


def test_callers_without_the_token_share_the_cached_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the token set, header-less callers cannot make the probes run again."""
    monkeypatch.setenv("METRICS_AUTH_TOKEN", _TOKEN)
    get_settings.cache_clear()
    probe = _CountingProbe(ok=False)
    register_probe("counted", probe)
    client = TestClient(app)

    for _ in range(5):
        response = client.get("/ready")
        assert response.status_code == 503
        assert "features" not in response.json()
    with_token = client.get("/ready", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert probe.runs == 1
    assert with_token.status_code == 503
    assert "features" in with_token.json()


def test_probes_run_again_after_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "DEFAULT_READINESS_TTL_S", 0.0)
    probe = _CountingProbe()
    register_probe("counted", probe)
    client = TestClient(app)

    client.get("/ready")
    client.get("/ready")

    assert probe.runs == 2


def test_clearing_the_registry_drops_the_cached_verdict() -> None:
    client = TestClient(app)
    register_probe("first", lambda: ProbeResult(name="first", ok=True))
    assert client.get("/ready").status_code == 200

    clear_probes()
    register_probe("second", lambda: ProbeResult(name="second", ok=False, detail="down"))
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == [{"name": "second", "ok": False, "detail": "down"}]


def test_empty_registry_is_not_ready_on_every_call() -> None:
    client = TestClient(app)
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json() == {
        "status": "not_ready",
        "checks": [],
        "sandbox_mode": "off",
        "features": client.get("/ready").json()["features"],
    }
