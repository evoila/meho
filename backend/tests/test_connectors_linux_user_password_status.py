# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``linux.user.password_status`` (#6239)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops import LINUX_OPS
from meho_backplane.connectors.linux.ops_user import (
    LinuxPasswordStatusError,
    build_password_status_command,
    parse_password_status_output,
    validate_user_name,
)
from meho_backplane.settings import get_settings

_SUDO_CANARY = "linux-user-sudo-canary-xyz-6239"  # gitleaks:allow -- synthetic
_HASH_CANARY = "$6$saltsalt$hashhashhashcanary"  # gitleaks:allow -- synthetic
_TODAY = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="linux-user-test",
    host="linux-host.test.invalid",
    port=22,
    secret_ref="meho/testing/linux/user-test",
)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _connector(secret: dict[str, Any]) -> Any:
    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(return_value=secret)  # type: ignore[method-assign]
    return connector


def _chage(last: str, expires: str, inactive: str = "never", account: str = "never") -> str:
    return (
        f"Last password change\t\t\t\t\t: {last}\n"
        f"Password expires\t\t\t\t\t: {expires}\n"
        f"Password inactive\t\t\t\t\t: {inactive}\n"
        f"Account expires\t\t\t\t\t\t: {account}\n"
        "Minimum number of days between password change\t\t: 0\n"
        "Maximum number of days between password change\t\t: 90\n"
        "Number of days of warning before password expires\t: 7\n"
    )


# -- user-name check ---------------------------------------------------------


@pytest.mark.parametrize("user", ["root", "svc-deploy", "_apt", "User_1", "a" * 32])
def test_valid_user_names_pass(user: str) -> None:
    assert validate_user_name(user) == user
    assert build_password_status_command(user) == f"LC_ALL=C chage -l -- {user}"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a b",
        "a;b",
        "$(id)",
        "`id`",
        "-root",
        "--help",
        "a\nb",
        "1user",
        "a" * 33,
        "a|b",
        "a'b",
        "root\n",
        None,
        5,
    ],
)
def test_bad_user_names_are_refused_before_a_command_is_built(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_user_name(bad)
    with pytest.raises(ValueError):
        build_password_status_command(bad)


# -- parser ------------------------------------------------------------------


def test_parse_a_date() -> None:
    result = parse_password_status_output(
        "ops", _chage("Oct 01, 2026", "Dec 30, 2026", "Jan 05, 2027"), today=_TODAY
    )
    assert result == {
        "user": "ops",
        "last_change": "2026-10-01",
        "expires": "2026-12-30",
        "inactive": "2027-01-05",
        "account_expires": "never",
        "min_days": 0,
        "max_days": 90,
        "warn_days": 7,
        "must_change": False,
        "days_until_expiry": 48,
        "expired": False,
    }


def test_parse_never_gives_null_days() -> None:
    result = parse_password_status_output("ops", _chage("Oct 01, 2026", "never"), today=_TODAY)
    assert result["expires"] == "never"
    assert result["days_until_expiry"] is None
    assert result["expired"] is False


def test_parse_expires_today_is_not_yet_expired_and_yesterday_is() -> None:
    today = parse_password_status_output(
        "ops", _chage("Aug 14, 2026", "Nov 12, 2026"), today=_TODAY
    )
    assert (today["days_until_expiry"], today["expired"]) == (0, False)
    past = parse_password_status_output("ops", _chage("Aug 13, 2026", "Nov 11, 2026"), today=_TODAY)
    assert (past["days_until_expiry"], past["expired"]) == (-1, True)


def test_parse_password_must_be_changed() -> None:
    result = parse_password_status_output(
        "ops", _chage("password must be changed", "password must be changed"), today=_TODAY
    )
    assert result["must_change"] is True
    assert result["expired"] is True
    assert result["days_until_expiry"] == 0


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "Last password change : Oct 01, 2026\n",
        _chage("Oct 99, 2026", "never"),
        _chage("Oct 01, 2026", "sometime"),
        _chage("Oct 01, 2026", "never").replace(": 90", ": many"),
    ],
)
def test_parse_partial_or_odd_output_is_an_error_not_an_answer(stdout: str) -> None:
    with pytest.raises(LinuxPasswordStatusError):
        parse_password_status_output("ops", stdout, today=_TODAY)


# -- handler -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_user_reads_own_account_without_sudo() -> None:
    connector = _connector({"username": "ops", "password": _SUDO_CANARY})
    sudo = AsyncMock()
    with (
        patch("meho_backplane.connectors.linux.ops_user.run_remote_bash_with_sudo", sudo),
        patch.object(connector, "_run_command", new_callable=AsyncMock) as plain,
    ):
        plain.return_value = _proc(stdout=_chage("Oct 01, 2026", "never"))
        result = await connector.user_password_status(_TARGET, {"user": "ops"})
    assert result["user"] == "ops"
    assert plain.await_args.args[1] == "LC_ALL=C chage -l -- ops"
    sudo.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_user_reads_through_sudo_and_no_secret_is_returned() -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    sudo = AsyncMock(return_value=_proc(stdout=_chage("Oct 01, 2026", "Dec 30, 2026")))
    with (
        patch("meho_backplane.connectors.linux.ops_user.run_remote_bash_with_sudo", sudo),
        patch.object(connector, "_run_command", new_callable=AsyncMock) as plain,
    ):
        result = await connector.user_password_status(_TARGET, {"user": "svc-deploy"})
    plain.assert_not_awaited()
    assert sudo.await_args.args[2] == "LC_ALL=C chage -l -- svc-deploy"
    assert sudo.await_args.kwargs["sudo_password"] == _SUDO_CANARY
    assert _SUDO_CANARY not in repr(result)


@pytest.mark.asyncio
async def test_unknown_user_is_a_clear_error() -> None:
    connector = _connector({"username": "ops"})
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as plain:
        plain.return_value = _proc(
            stderr="chage: user 'ops' does not exist in /etc/passwd\n", exit_status=1
        )
        with pytest.raises(LinuxPasswordStatusError, match="unknown_user"):
            await connector.user_password_status(_TARGET, {"user": "ops"})


@pytest.mark.asyncio
async def test_failed_reads_raise_and_never_repeat_host_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    stderr = f"sudo: a password is required {_HASH_CANARY}\n"
    sudo = AsyncMock(return_value=_proc(stderr=stderr, exit_status=1))
    with (
        caplog.at_level(logging.DEBUG),
        patch("meho_backplane.connectors.linux.ops_user.run_remote_bash_with_sudo", sudo),
        pytest.raises(LinuxPasswordStatusError, match="permission_denied") as excinfo,
    ):
        await connector.user_password_status(_TARGET, {"user": "svc-deploy"})
    assert _HASH_CANARY not in str(excinfo.value)
    assert _SUDO_CANARY not in str(excinfo.value)
    assert _HASH_CANARY not in caplog.text
    assert _SUDO_CANARY not in caplog.text


@pytest.mark.asyncio
async def test_other_failure_is_read_failed() -> None:
    connector = _connector({"username": "ops"})
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as plain:
        plain.return_value = _proc(stderr="boom", exit_status=127)
        with pytest.raises(LinuxPasswordStatusError, match="read_failed"):
            await connector.user_password_status(_TARGET, {"user": "ops"})


@pytest.mark.asyncio
async def test_bad_user_is_refused_before_any_ssh() -> None:
    connector = _connector({"username": "ops"})
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as plain,
        patch("meho_backplane.connectors.linux.ops_user.run_remote_bash_with_sudo") as sudo,
        pytest.raises(ValueError),
    ):
        await connector.user_password_status(_TARGET, {"user": "x; reboot"})
    plain.assert_not_awaited()
    sudo.assert_not_called()


# -- registration ------------------------------------------------------------


def test_op_is_a_safe_read_without_approval() -> None:
    op = next(op for op in LINUX_OPS if op.op_id == "linux.user.password_status")
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags
    assert op.parameter_schema["required"] == ["user"]
    assert "days_until_expiry" in op.response_schema["properties"]
    assert hasattr(LinuxSshConnector, op.handler_attr)
    for key in ("when_to_call", "output_shape"):
        assert op.llm_instructions[key].strip(), key


@pytest.mark.asyncio
async def test_sensor_can_use_days_until_expiry_with_a_threshold() -> None:
    import uuid

    from sqlalchemy import select

    from meho_backplane.checks.schemas import SensorCreate
    from meho_backplane.checks.service import SensorAdminService
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Tenant

    tenant_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    async with get_sessionmaker()() as session:
        existing = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug="tenant-a", name="Tenant A"))
            await session.commit()
    await LinuxSshConnector.register_operations()

    entry = await SensorAdminService().create(
        tenant_id=tenant_id,
        created_by_sub="op-admin",
        payload=SensorCreate(
            name="svc-password-expiry",
            connector_id="linux-ssh-1.x",
            op_id="linux.user.password_status",
            params={"user": "svc-deploy"},
            assertion={
                "select": {"path": "$.days_until_expiry"},
                "compare": {"type": "threshold", "op": "lt", "degraded": 14, "critical": 3},
            },
            cadence_kind="interval",
            interval_seconds=3600,
        ),
    )
    assert entry.op_id == "linux.user.password_status"
    assert entry.status.value == "active"
