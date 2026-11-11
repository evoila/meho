# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The first-start destination proof uses ``PARAMS_HASH_KEY`` only.

With ``PARAMS_HASH_KEY`` set, ``GET /first-start/destination`` answers the
proof, and the proof does not change with ``DATABASE_URL``. Without it,
``derive_install_key`` raises, the route answers 404, the DNS line is a
``warn`` that names the setting, and MEHO logs one warning at start.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from meho_backplane import first_start
from meho_backplane.main import app
from meho_backplane.operations._validate import (
    InstallKeyMissingError,
    derive_install_key,
    warn_if_params_hash_key_missing,
)
from meho_backplane.settings import get_settings

_SITE = "https://meho.example.com"
_NONCE = "ab" * 16
_KEY = "made-up-test-key-of-this-install"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _SITE)
    monkeypatch.delenv("PARAMS_HASH_KEY", raising=False)
    get_settings.cache_clear()
    first_start.reset_public_run_for_testing()
    yield
    get_settings.cache_clear()
    first_start.reset_public_run_for_testing()


def _set_key(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PARAMS_HASH_KEY", value)
    get_settings.cache_clear()


def _set_database_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / name}")
    get_settings.cache_clear()


def _patch_green_site(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every outside source answer like a healthy site; record proof fetches."""
    fetched: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        return ["192.0.2.10"]

    async def cert(host: str, port: int) -> dict[str, object]:
        end = datetime.now(UTC) + timedelta(days=90)
        return {"notAfter": end.strftime("%b %d %H:%M:%S %Y GMT")}

    async def now() -> datetime:
        return datetime.now(UTC)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return []

    async def proof(backplane_url: str, nonce: str) -> str | None:
        fetched.append(nonce)
        return first_start._destination_proof(nonce)

    monkeypatch.setattr(first_start, "_resolve_host", resolve)
    monkeypatch.setattr(first_start, "_fetch_destination_proof", proof)
    monkeypatch.setattr(first_start, "_fetch_peer_certificate", cert)
    monkeypatch.setattr(first_start, "_keycloak_time", now)
    monkeypatch.setattr(first_start, "_database_time", now)
    monkeypatch.setattr(first_start, "_live_targets", targets)
    return fetched


def _dns_line() -> dict[str, Any]:
    response = TestClient(app).get("/first-start")
    assert response.status_code == 200
    body = response.json()
    line: dict[str, Any] = body["checks"][0]
    assert line["name"] == "dns"
    assert body["ok"] is True
    return line


# --- without PARAMS_HASH_KEY -------------------------------------------------


def test_derive_install_key_raises_without_params_hash_key() -> None:
    with pytest.raises(InstallKeyMissingError):
        derive_install_key(b"any-label")


def test_derive_install_key_raises_for_a_blank_params_hash_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch, "   ")
    with pytest.raises(InstallKeyMissingError):
        derive_install_key(b"any-label")


def test_destination_route_answers_404_without_params_hash_key() -> None:
    response = TestClient(app).get("/first-start/destination", params={"nonce": _NONCE})
    assert response.status_code == 404
    assert "proof" not in response.text


def test_destination_route_answers_404_for_any_nonce_without_params_hash_key() -> None:
    for nonce in ("0" * 32, "f" * 32, _NONCE):
        response = TestClient(app).get("/first-start/destination", params={"nonce": nonce})
        assert response.status_code == 404


def test_dns_line_warns_and_calls_nothing_without_params_hash_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _patch_green_site(monkeypatch)
    line = _dns_line()
    assert line["status"] == "warn"
    assert "PARAMS_HASH_KEY is not set" in line["message"]
    assert fetched == []


def test_start_warns_once_without_params_hash_key() -> None:
    with capture_logs() as logs:
        assert warn_if_params_hash_key_missing(get_settings().params_hash_key) is False
    assert [entry["event"] for entry in logs] == ["params_hash_key_missing"]
    assert logs[0]["log_level"] == "warning"


# --- with PARAMS_HASH_KEY ----------------------------------------------------


def test_destination_route_answers_the_proof_with_params_hash_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch, _KEY)
    response = TestClient(app).get("/first-start/destination", params={"nonce": _NONCE})
    assert response.status_code == 200
    assert response.json() == {"proof": first_start._destination_proof(_NONCE)}


def test_dns_line_is_green_with_params_hash_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, _KEY)
    fetched = _patch_green_site(monkeypatch)
    line = _dns_line()
    assert line["status"] == "ok"
    assert "points at this MEHO" in line["message"]
    assert len(fetched) == 1


def test_proof_stays_the_same_when_database_url_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_key(monkeypatch, _KEY)
    _set_database_url(monkeypatch, tmp_path, "one.db")
    first = first_start._destination_proof(_NONCE)
    _set_database_url(monkeypatch, tmp_path, "two.db")
    assert first_start._destination_proof(_NONCE) == first


def test_proof_changes_when_params_hash_key_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, _KEY)
    first = first_start._destination_proof(_NONCE)
    _set_key(monkeypatch, "another-made-up-test-key")
    assert first_start._destination_proof(_NONCE) != first


def test_start_does_not_warn_with_params_hash_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, _KEY)
    with capture_logs() as logs:
        assert warn_if_params_hash_key_missing(get_settings().params_hash_key) is True
    assert logs == []
