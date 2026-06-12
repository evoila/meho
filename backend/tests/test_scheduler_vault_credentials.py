# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.scheduler.vault_credentials` (#1478).

The scheduler-service-token Vault broker writes an agent's
``client_credentials`` secret at registration and reads it back at fire
time, both under the scheduler's static service token. These tests cover:

* the raw-API-path -> ``(mount, logical_path)`` splitter;
* the not-configured guard (no ``VAULT_SCHEDULER_TOKEN``);
* the write happy path (payload shape + path derivation);
* the read happy path, the not-found (``None``) path, and error mapping.

The hvac client is faked via the ``_build_client`` seam — no running
Vault. The live round-trip is covered by the integration suite.
"""

from __future__ import annotations

from typing import Any

import hvac.exceptions
import pytest
import requests.exceptions

import meho_backplane.scheduler.vault_credentials as vc
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN", "scheduler-tok")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeKvV2:
    """In-memory stand-in for ``client.secrets.kv.v2``."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.read_result: Any = None
        self.read_raises: BaseException | None = None
        self.last_read: dict[str, Any] | None = None

    def create_or_update_secret(
        self, *, path: str, secret: dict[str, Any], mount_point: str
    ) -> dict[str, Any]:
        self.writes.append({"path": path, "secret": secret, "mount_point": mount_point})
        return {"data": {"version": 1}}

    def read_secret_version(
        self, *, path: str, mount_point: str, raise_on_deleted_version: bool
    ) -> Any:
        self.last_read = {"path": path, "mount_point": mount_point}
        if self.read_raises is not None:
            raise self.read_raises
        return self.read_result


class _FakeSecrets:
    def __init__(self, kv: _FakeKvV2) -> None:
        self.kv = type("KV", (), {"v2": kv})()


class _FakeClient:
    def __init__(self, kv: _FakeKvV2) -> None:
        self.secrets = _FakeSecrets(kv)


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch) -> _FakeKvV2:
    """Patch the ``_build_client`` seam to return a fake hvac client."""
    kv = _FakeKvV2()

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        # Assert the scheduler token is threaded through.
        assert token == "scheduler-tok"
        return _FakeClient(kv)

    monkeypatch.setattr(vc, "_build_client", _fake_build_client)
    return kv


# --- splitter ---------------------------------------------------------


@pytest.mark.parametrize(
    ("api_path", "expected"),
    [
        (
            "secret/data/agents/AGENT_REPORTER/credentials",
            ("secret", "agents/AGENT_REPORTER/credentials"),
        ),
        ("kv/data/foo/bar", ("kv", "foo/bar")),
        ("secret/data/x", ("secret", "x")),
        # No data/ infix -> treated as logical on default mount.
        ("agents/x/credentials", ("secret", "agents/x/credentials")),
        # Surrounding slashes tolerated.
        ("/secret/data/a/b/", ("secret", "a/b")),
    ],
)
def test_split_kv_v2_api_path(api_path: str, expected: tuple[str, str]) -> None:
    assert vc.split_kv_v2_api_path(api_path) == expected


@pytest.mark.parametrize("bad", ["", "   ", "secret/data/", "secret/data"])
def test_split_kv_v2_api_path_rejects_empty_logical(bad: str) -> None:
    with pytest.raises(ValueError):
        vc.split_kv_v2_api_path(bad)


def test_vault_path_for_client_id_sanitises_and_uppercases() -> None:
    path = vc.vault_path_for_client_id("agent:reporter")
    assert path == "secret/data/agents/AGENT_REPORTER/credentials"


# --- not configured ---------------------------------------------------


async def test_write_not_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_SCHEDULER_TOKEN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(vc.SchedulerVaultNotConfiguredError):
        await vc.write_agent_secret("agent:reporter", "s3cr3t")


async def test_read_not_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_SCHEDULER_TOKEN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(vc.SchedulerVaultNotConfiguredError):
        await vc.read_agent_secret("agent:reporter")


# --- write ------------------------------------------------------------


async def test_write_persists_secret_at_derived_path(fake_kv: _FakeKvV2) -> None:
    api_path = await vc.write_agent_secret("agent:reporter", "gen-secret")

    assert api_path == "secret/data/agents/AGENT_REPORTER/credentials"
    assert len(fake_kv.writes) == 1
    write = fake_kv.writes[0]
    assert write["mount_point"] == "secret"
    assert write["path"] == "agents/AGENT_REPORTER/credentials"
    assert write["secret"] == {vc.SECRET_FIELD: "gen-secret"}


async def test_write_unreachable_maps_to_broker_error(fake_kv: _FakeKvV2) -> None:
    def _boom(**_: Any) -> None:
        raise requests.exceptions.ConnectionError("down")

    fake_kv.create_or_update_secret = _boom  # type: ignore[assignment]
    with pytest.raises(vc.SchedulerVaultBrokerError):
        await vc.write_agent_secret("agent:reporter", "s")


# --- read -------------------------------------------------------------


async def test_read_returns_secret(fake_kv: _FakeKvV2) -> None:
    fake_kv.read_result = {
        "data": {"data": {vc.SECRET_FIELD: "gen-secret"}, "metadata": {"version": 1}}
    }
    secret = await vc.read_agent_secret("agent:reporter")
    assert secret == "gen-secret"
    assert fake_kv.last_read == {
        "path": "agents/AGENT_REPORTER/credentials",
        "mount_point": "secret",
    }


async def test_read_missing_path_returns_none(fake_kv: _FakeKvV2) -> None:
    """A KV-v2 read of a non-existent path (InvalidPath) returns ``None``."""
    fake_kv.read_raises = hvac.exceptions.InvalidPath("404")
    assert await vc.read_agent_secret("agent:reporter") is None


async def test_read_missing_field_returns_none(fake_kv: _FakeKvV2) -> None:
    """A payload without the secret field is treated as 'not in Vault'."""
    fake_kv.read_result = {"data": {"data": {"other": "x"}, "metadata": {}}}
    assert await vc.read_agent_secret("agent:reporter") is None


async def test_read_malformed_payload_returns_none(fake_kv: _FakeKvV2) -> None:
    fake_kv.read_result = {"data": {}}
    assert await vc.read_agent_secret("agent:reporter") is None


async def test_read_unreachable_maps_to_broker_error(fake_kv: _FakeKvV2) -> None:
    fake_kv.read_raises = requests.exceptions.Timeout("slow")
    with pytest.raises(vc.SchedulerVaultBrokerError):
        await vc.read_agent_secret("agent:reporter")


async def test_read_vault_error_maps_to_broker_error(fake_kv: _FakeKvV2) -> None:
    fake_kv.read_raises = hvac.exceptions.Forbidden("denied")
    with pytest.raises(vc.SchedulerVaultBrokerError):
        await vc.read_agent_secret("agent:reporter")
