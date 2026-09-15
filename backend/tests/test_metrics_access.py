# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the opt-in ``/metrics`` + ``/ready`` bearer-token guard (#3499).

Covers :mod:`meho_backplane.metrics_access` and its wiring onto the two
exposition endpoints:

* **Default-open** — with ``METRICS_AUTH_TOKEN`` unset, ``/metrics`` and
  ``/ready`` are reachable unauthenticated exactly as before (no 401), so the
  in-cluster scraper and kubelet readiness probe are undisturbed.
* **Opt-in guard** — with the token set, ``/metrics`` and ``/ready`` refuse a
  missing / malformed / mismatched bearer with 401 and admit the correct one.
* **Liveness stays open** — ``/healthz`` is never guarded, so a bearer-less
  liveness path always exists even when the guard is on.

Drives the production ``meho_backplane.main:app`` (plain ``TestClient`` — no
lifespan, so no readiness probes are registered; a guarded-and-authed
``/ready`` therefore renders its own verdict, which is what we assert around,
never the exact 200/503).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.main import app
from meho_backplane.settings import get_settings

_TOKEN = "s3cr3t-scrape-token"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the minimal Settings env; ``METRICS_AUTH_TOKEN`` starts unset."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


def _set_token(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("METRICS_AUTH_TOKEN", value)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Default-open: behaviour unchanged when the guard is not configured
# ---------------------------------------------------------------------------


def test_metrics_open_by_default(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text or resp.text  # exposition rendered


def test_ready_open_by_default(client: TestClient) -> None:
    # No probes registered (no lifespan) -> 503, but crucially NOT 401.
    resp = client.get("/ready")
    assert resp.status_code != 401
    assert "features" in resp.json()


# ---------------------------------------------------------------------------
# Guard on: missing / malformed / wrong bearer -> 401
# ---------------------------------------------------------------------------


def test_metrics_requires_bearer_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/metrics")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "metrics_auth_required"
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_ready_requires_bearer_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/ready")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "metrics_auth_required"


def test_wrong_bearer_denied(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/metrics", headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 401


def test_non_bearer_scheme_denied(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/metrics", headers={"Authorization": f"Basic {_TOKEN}"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Guard on: correct bearer admits; liveness always open
# ---------------------------------------------------------------------------


def test_correct_bearer_admits_metrics(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/metrics", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200


def test_correct_bearer_admits_ready(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/ready", headers={"Authorization": f"Bearer {_TOKEN}"})
    # Admitted past the guard -> the readiness verdict renders (not a 401).
    assert resp.status_code != 401
    assert "features" in resp.json()


def test_healthz_never_guarded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch, _TOKEN)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
