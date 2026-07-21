# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.scheduler.vault_credentials` (#1478).

The scheduler-service-token Vault broker writes an agent's
``client_credentials`` secret at registration and reads it back at fire
time, both under the scheduler's static service token. These tests cover:

* the raw-API-path -> ``(mount, logical_path)`` splitter;
* the not-configured guard (no ``VAULT_SCHEDULER_TOKEN``);
* the write happy path (payload shape + path derivation);
* the read happy path, the not-found (``None``) path, and error mapping;
* the write-failure disposition split (#2652) — a Vault 403 plus a
  failing ``lookup-self`` means the token is dead, a Vault 403 plus a
  succeeding ``lookup-self`` means the policy is under-scoped.

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


class _FakeTokenApi:
    """In-memory stand-in for ``client.auth.token`` (renew/lookup-self)."""

    def __init__(self) -> None:
        self.renew_calls = 0
        self.renew_raises: BaseException | None = None
        self.lookup_result: Any = None
        self.lookup_raises: BaseException | None = None
        self.lookup_calls = 0

    def renew_self(self, increment: int | None = None) -> Any:
        self.renew_calls += 1
        if self.renew_raises is not None:
            raise self.renew_raises
        return {"auth": {"lease_duration": 2764800}}

    def lookup_self(self, mount_point: str = "token") -> Any:
        self.lookup_calls += 1
        if self.lookup_raises is not None:
            raise self.lookup_raises
        return self.lookup_result


class _FakeKvV2:
    """In-memory stand-in for ``client.secrets.kv.v2``."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.read_result: Any = None
        self.read_raises: BaseException | None = None
        self.last_read: dict[str, Any] | None = None
        #: Co-located so a test holding the kv fake can assert
        #: renew-on-use without threading a second fixture through.
        self.token_api = _FakeTokenApi()

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


class _FakeAuth:
    def __init__(self, token: _FakeTokenApi) -> None:
        self.token = token


class _FakeClient:
    def __init__(self, kv: _FakeKvV2) -> None:
        self.secrets = _FakeSecrets(kv)
        self.auth = _FakeAuth(kv.token_api)


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


@pytest.fixture
def fake_kv_any_token(monkeypatch: pytest.MonkeyPatch) -> _FakeKvV2:
    """Like :func:`fake_kv` but does not pin the threaded-through token.

    Used by the token-source and ``lookup-self`` tests, where the token
    comes from a file / differs from the env-var default.
    """
    kv = _FakeKvV2()

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
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
    with pytest.raises(vc.SchedulerVaultBrokerError) as excinfo:
        await vc.write_agent_secret("agent:reporter", "s")
    # An unreachable Vault is already an unambiguous diagnosis; the
    # write path must not spend a lookup-self on it (#2652).
    assert excinfo.value.token_invalid is False
    assert fake_kv.token_api.lookup_calls == 0


# --- write-failure disposition: dead token vs. under-scoped policy (#2652) ---


def _deny_write(fake_kv: _FakeKvV2) -> None:
    """Make the KV write answer with Vault's 403 (the ambiguous case)."""

    def _forbidden(**_: Any) -> None:
        raise hvac.exceptions.Forbidden("permission denied")

    fake_kv.create_or_update_secret = _forbidden  # type: ignore[assignment]


async def test_write_denied_with_dead_token_sets_token_invalid(fake_kv: _FakeKvV2) -> None:
    """Write 403 + lookup-self 403 -> the token itself is the fault."""
    _deny_write(fake_kv)
    fake_kv.token_api.lookup_raises = hvac.exceptions.Forbidden("permission denied")

    with pytest.raises(vc.SchedulerVaultBrokerError) as excinfo:
        await vc.write_agent_secret("agent:reporter", "s")

    assert excinfo.value.token_invalid is True
    assert fake_kv.token_api.lookup_calls == 1
    # Fail fast: the write is diagnosed, never retried.
    assert fake_kv.writes == []


async def test_write_denied_with_live_token_keeps_policy_disposition(
    fake_kv: _FakeKvV2,
) -> None:
    """Write 403 + lookup-self OK -> the policy scope is the fault."""
    _deny_write(fake_kv)
    fake_kv.token_api.lookup_result = {"data": {"ttl": 2764800, "expire_time": None}}

    with pytest.raises(vc.SchedulerVaultBrokerError) as excinfo:
        await vc.write_agent_secret("agent:reporter", "s")

    assert excinfo.value.token_invalid is False
    assert fake_kv.token_api.lookup_calls == 1


async def test_write_denied_with_unreachable_lookup_stays_conservative(
    fake_kv: _FakeKvV2,
) -> None:
    """A transport failure on lookup-self is not evidence of a dead token."""
    _deny_write(fake_kv)
    fake_kv.token_api.lookup_raises = requests.exceptions.ConnectionError("down")

    with pytest.raises(vc.SchedulerVaultBrokerError) as excinfo:
        await vc.write_agent_secret("agent:reporter", "s")

    assert excinfo.value.token_invalid is False


async def test_broker_error_defaults_to_token_valid() -> None:
    """Every pre-existing raise site keeps the policy-scope disposition."""
    assert vc.SchedulerVaultBrokerError("boom").token_invalid is False
    assert vc.SchedulerVaultNotConfiguredError("boom").token_invalid is False


def test_write_denied_detail_is_unchanged() -> None:
    """The under-scoped-policy remediation is preserved verbatim (#2652)."""
    assert vc.SCHEDULER_VAULT_WRITE_DENIED_DETAIL == (
        "scheduler Vault write failed — VAULT_SCHEDULER_TOKEN policy must "
        "grant create/update on the agent-credentials path"
    )


def test_token_invalid_detail_names_the_remint() -> None:
    """The dead-token remediation names re-minting, not the policy."""
    detail = vc.SCHEDULER_VAULT_TOKEN_INVALID_DETAIL
    assert detail.startswith("scheduler_vault_token_invalid:")
    assert "invalid or expired" in detail
    assert "re-mint" in detail.lower()
    assert "VAULT_SCHEDULER_TOKEN" in detail
    assert "docs/cross-repo/vault-provisioning.md" in detail
    assert "policy must grant" not in detail


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


# --- renew-on-use (#2328) --------------------------------------------


async def test_read_renews_token_on_success(fake_kv: _FakeKvV2) -> None:
    fake_kv.read_result = {"data": {"data": {vc.SECRET_FIELD: "s"}, "metadata": {}}}
    assert await vc.read_agent_secret("agent:reporter") == "s"
    assert fake_kv.token_api.renew_calls == 1


async def test_write_renews_token_on_success(fake_kv: _FakeKvV2) -> None:
    await vc.write_agent_secret("agent:reporter", "s")
    assert fake_kv.token_api.renew_calls == 1


async def test_renew_failure_does_not_break_read(fake_kv: _FakeKvV2) -> None:
    """A failed best-effort renew is swallowed — the read still returns."""
    fake_kv.read_result = {"data": {"data": {vc.SECRET_FIELD: "s"}, "metadata": {}}}
    fake_kv.token_api.renew_raises = hvac.exceptions.Forbidden("not renewable")
    assert await vc.read_agent_secret("agent:reporter") == "s"
    assert fake_kv.token_api.renew_calls == 1


async def test_missing_path_read_does_not_renew(fake_kv: _FakeKvV2) -> None:
    """InvalidPath (secret absent) returns before the renew step."""
    fake_kv.read_raises = hvac.exceptions.InvalidPath("404")
    assert await vc.read_agent_secret("agent:reporter") is None
    assert fake_kv.token_api.renew_calls == 0


# --- token source re-read (#2328) ------------------------------------


def test_current_scheduler_token_prefers_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN_FILE", str(token_file))
    get_settings.cache_clear()
    assert vc._current_scheduler_token(get_settings()) == "file-token"


def test_current_scheduler_token_falls_back_when_file_absent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN_FILE", str(tmp_path / "nope"))
    get_settings.cache_clear()
    # env token from the autouse fixture is "scheduler-tok".
    assert vc._current_scheduler_token(get_settings()) == "scheduler-tok"


def test_current_scheduler_token_empty_file_falls_back(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN_FILE", str(token_file))
    get_settings.cache_clear()
    assert vc._current_scheduler_token(get_settings()) == "scheduler-tok"


def test_current_scheduler_token_reread_per_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is re-read on every call — a re-mint is picked up live."""
    token_file = tmp_path / "tok"
    token_file.write_text("first", encoding="utf-8")
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN_FILE", str(token_file))
    get_settings.cache_clear()
    settings = get_settings()
    assert vc._current_scheduler_token(settings) == "first"
    token_file.write_text("rotated", encoding="utf-8")
    # Same (cached) settings object — but the token is resolved fresh.
    assert vc._current_scheduler_token(settings) == "rotated"


# --- lookup-self health check (#2328) --------------------------------


async def test_verify_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_SCHEDULER_TOKEN", raising=False)
    get_settings.cache_clear()
    status = await vc.verify_scheduler_token()
    assert status.configured is False
    assert status.ok is True
    assert status.detail == "not_configured"


async def test_verify_ok_reports_lifetime(fake_kv_any_token: _FakeKvV2) -> None:
    fake_kv_any_token.token_api.lookup_result = {
        "data": {"ttl": 2764800, "expire_time": "2026-08-11T00:00:00Z"}
    }
    status = await vc.verify_scheduler_token(reason="startup")
    assert status.configured is True
    assert status.ok is True
    assert status.ttl_seconds == 2764800
    assert status.expire_time == "2026-08-11T00:00:00Z"


async def test_verify_dead_token(fake_kv_any_token: _FakeKvV2) -> None:
    fake_kv_any_token.token_api.lookup_raises = hvac.exceptions.Forbidden("403")
    status = await vc.verify_scheduler_token()
    assert status.configured is True
    assert status.ok is False
    assert status.detail.startswith("denied:")


async def test_verify_unreachable(fake_kv_any_token: _FakeKvV2) -> None:
    fake_kv_any_token.token_api.lookup_raises = requests.exceptions.ConnectionError("down")
    status = await vc.verify_scheduler_token()
    assert status.configured is True
    assert status.ok is False
    assert status.detail.startswith("unreachable:")
