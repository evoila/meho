# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``pfsense.openvpn.user.create`` while an admin blocks the caller, through ``dispatch``.

The caller is blocked after the firewall made the new VPN user and before
MEHO stores its password in Vault. The store is not sent. MEHO removes the
new user again: the undo is a clean-up, so its SSH commands run with no block
check. The answer is the block refusal. When the undo fails, the answer and
the audit row name the user that stays.

The same holds when the caller is blocked while the firewall runs the create
command: the removal of the staged script is a clean-up, so it is sent, and
MEHO keeps the firewall's reply to the create, so it can remove the new user.
When that removal fails because the SSH connection is lost, MEHO still keeps
the reply: it stores the password, or removes the new user when the caller
is blocked.

The firewall is a scripted SSH connection under the real ``_run_command``
and ``run_capped``; it keeps its users. Vault is the stand-in HTTP server
with the real ``hvac`` client. All names and values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.connectors.adapters.ssh import ConnectorUnreachableError
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.pfsense import ops_openvpn_user as user_module
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings

from ._principal_block_send_helpers import add_tenant, block_the_caller
from ._ssh_fakes import FakeSshProcess
from ._vault_http_standin import VaultStandIn, vault_standin

pytestmark = pytest.mark.asyncio

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000b1c04")
_VAULT_ITEM = f"tenants/{_TENANT_ID}/vpn/guest-1"
_CREATE = {"name": "guest-1", "vault_path": _VAULT_ITEM}
_PASSWORD = "QRSTVWXZbcdfghjkmnpqrstv"


def _user(name: str, uid: str, *, scope: str = "user") -> str:
    return f"<user><scope>{scope}</scope><name>{name}</name><uid>{uid}</uid></user>"


class _Firewall:
    """A scripted pfSense that keeps its local users and records each command."""

    def __init__(self, *, undo_fails: bool = False) -> None:
        self.users: dict[str, str] = {}
        self.commands: list[str] = []
        self.undo_fails = undo_fails
        self.closed = False

    def _config(self) -> str:
        users = "".join(_user(name, uid) for name, uid in self.users.items())
        return (
            "<pfsense><system><group><name>all</name><member>0</member></group>"
            f"{_user('admin', '0', scope='system')}{users}</system><openvpn></openvpn></pfsense>"
        )

    def _answer(self, cmd: str) -> tuple[str, int]:
        if cmd == "cat /etc/version":
            return "2.7.2-RELEASE (amd64)\n", 0
        if cmd == "cat /cf/conf/config.xml":
            return self._config(), 0
        if cmd.startswith("pfSsh.php playback meho_openvpn_user_create_"):
            self.users["guest-1"] = "2000"
            return (
                "MEHO_VPN_CREATED_UID=2000\nMEHO_VPN_CREATED_NAME=guest-1\n"
                f"MEHO_VPN_PASSWORD={_PASSWORD}\n",
                0,
            )
        if cmd.startswith("pfSsh.php playback meho_openvpn_user_undo_"):
            if self.undo_fails:
                return "", 1
            self.users.pop("guest-1", None)
            return "MEHO_VPN_REMOVED\n", 0
        return "", 0

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def create_process(self, cmd: str, **_kwargs: object) -> FakeSshProcess:
        self.commands.append(cmd)
        stdout, status = self._answer(cmd)
        return FakeSshProcess(command=cmd, stdout=stdout, exit_status=status)

    def played(self, kind: str) -> int:
        return sum(1 for c in self.commands if c.startswith(f"pfSsh.php playback {kind}"))


class _ScriptedPfSense(PfSenseConnector):
    def __init__(
        self,
        firewall: _Firewall,
        *,
        block_after_create: bool = False,
        lose_script_removal: bool = False,
    ) -> None:
        super().__init__()
        self.firewall = firewall
        self.block_after_create = block_after_create
        self.lose_script_removal = lose_script_removal

    async def _connect(self, target: Any, operator: Operator | None = None) -> Any:
        return self.firewall

    async def _run_command(self, target: Any, cmd: str, **kwargs: Any) -> Any:
        if (
            self.lose_script_removal
            and cmd.startswith("rm -f ")
            and "meho_openvpn_user_create_" in cmd
        ):
            self.firewall.commands.append(cmd)
            raise ConnectorUnreachableError("made-up lost SSH connection")
        answer = await super()._run_command(target, cmd, **kwargs)
        if self.block_after_create and cmd.startswith(
            "pfSsh.php playback meho_openvpn_user_create_"
        ):
            # The create ran on the firewall; the next SSH command is the
            # removal of the staged script.
            await block_the_caller(_caller())
        return answer


class _Target:
    def __init__(self) -> None:
        self.product = "pfsense"
        self.fingerprint = type("_Fp", (), {"version": "2.7"})()
        self.preferred_impl_id: str | None = "pfsense-ssh"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "pf-block"
        self.host = "pf.test.invalid"
        self.port = 22
        self.auth_model = "shared_service_account"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    reset_block_cache()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VaultStandIn]:
    await add_tenant(_TENANT_ID)
    with vault_standin(monkeypatch) as standin:
        yield standin


def _caller() -> Operator:
    return Operator(
        sub="blocked-vpn-maker",
        name=None,
        email=None,
        raw_jwt="made.up.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _connector(
    firewall: _Firewall, *, block_after_create: bool = False, lose_script_removal: bool = False
) -> _ScriptedPfSense:
    if ("pfsense", "2.7", "pfsense-ssh") not in all_connectors_v2():
        register_connector_v2(
            product="pfsense", version="2.7", impl_id="pfsense-ssh", cls=PfSenseConnector
        )
    await PfSenseConnector.register_operations()
    connector = _ScriptedPfSense(
        firewall, block_after_create=block_after_create, lose_script_removal=lose_script_removal
    )
    _CONNECTOR_INSTANCE_CACHE[PfSenseConnector] = connector  # type: ignore[assignment]
    return connector


async def _run(op_id: str, params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_caller(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=dict(params),
        _approved=True,
    )


def _block_before_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    real = user_module._store_password

    async def _block_then_store(operator: Operator, *args: Any) -> None:
        await block_the_caller(operator)
        await real(operator, *args)

    monkeypatch.setattr(user_module, "_store_password", _block_then_store)


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


async def _dispatch_row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars()
        (row,) = [r for r in rows if r.payload.get("result_status") == "denied"]
    return row


async def test_new_user_is_removed_when_the_caller_is_blocked_before_the_store(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    firewall = _Firewall()
    await _connector(firewall)
    _block_before_the_store(monkeypatch)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    assert _is_block_refusal(result), (result.status, result.error)
    assert "not_removed" not in result.extras
    assert firewall.played("meho_openvpn_user_create_") == 1
    assert firewall.played("meho_openvpn_user_undo_") == 1
    assert firewall.users == {}
    # The read of the Vault item ran before the create; the store was not sent.
    assert vault.sent("POST", "/v1/secret/data/") == 0
    assert vault.sent("POST", "/v1/auth/jwt/login") == 1

    # The blocked caller's next calls stop before any command reaches the firewall.
    sent = len(firewall.commands)
    again = await _run("pfsense.openvpn.user.create", _CREATE)
    delete = await _run("pfsense.openvpn.user.delete", {"name": "guest-1"})
    assert _is_block_refusal(again), (again.status, again.error)
    assert _is_block_refusal(delete), (delete.status, delete.error)
    assert len(firewall.commands) == sent


async def test_a_failed_undo_names_the_user_that_stays_in_the_answer_and_the_audit_row(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    firewall = _Firewall(undo_fails=True)
    await _connector(firewall)
    _block_before_the_store(monkeypatch)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    stays = "the new VPN user guest-1 (uid 2000) was not removed"
    assert _is_block_refusal(result), (result.status, result.error)
    assert stays in (result.error or "")
    assert result.extras["not_removed"] == [stays]
    assert firewall.played("meho_openvpn_user_undo_") == 1
    assert firewall.users == {"guest-1": "2000"}
    assert vault.sent("POST", "/v1/secret/data/") == 0
    row = await _dispatch_row("pfsense.openvpn.user.create")
    assert row.payload["error"]["not_removed"] == [stays]
    assert _PASSWORD not in str(row.payload) and _PASSWORD not in (result.error or "")


def _script_removals(firewall: _Firewall, kind: str) -> int:
    return sum(1 for c in firewall.commands if c.startswith("rm -f ") and kind in c)


async def test_new_user_is_removed_when_the_caller_is_blocked_while_the_create_runs(
    vault: VaultStandIn,
) -> None:
    firewall = _Firewall()
    await _connector(firewall, block_after_create=True)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    assert _is_block_refusal(result), (result.status, result.error)
    assert "not_removed" not in result.extras
    assert firewall.played("meho_openvpn_user_create_") == 1
    # The removal of the staged create script is a clean-up: it is sent.
    assert _script_removals(firewall, "meho_openvpn_user_create_") == 1
    assert firewall.played("meho_openvpn_user_undo_") == 1
    assert _script_removals(firewall, "meho_openvpn_user_undo_") == 1
    assert firewall.users == {}
    assert vault.sent("POST", "/v1/secret/data/") == 0
    assert _PASSWORD not in (result.error or "")


async def test_a_failed_undo_after_a_block_during_the_create_names_the_user(
    vault: VaultStandIn,
) -> None:
    firewall = _Firewall(undo_fails=True)
    await _connector(firewall, block_after_create=True)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    stays = "the new VPN user guest-1 (uid 2000) was not removed"
    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["not_removed"] == [stays]
    assert stays in (result.error or "")
    assert firewall.played("meho_openvpn_user_undo_") == 1
    assert firewall.users == {"guest-1": "2000"}
    assert vault.sent("POST", "/v1/secret/data/") == 0
    row = await _dispatch_row("pfsense.openvpn.user.create")
    assert row.payload["error"]["not_removed"] == [stays]
    assert _PASSWORD not in str(row.payload) and _PASSWORD not in (result.error or "")


async def test_the_create_reply_is_kept_when_the_script_removal_loses_the_connection(
    vault: VaultStandIn,
) -> None:
    firewall = _Firewall()
    await _connector(firewall, lose_script_removal=True)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    assert result.status == "ok", (result.status, result.error)
    assert _script_removals(firewall, "meho_openvpn_user_create_") == 1
    assert firewall.users == {"guest-1": "2000"}
    assert vault.items[f"secret/{_VAULT_ITEM}"][1] == {"password": _PASSWORD}
    assert firewall.played("meho_openvpn_user_undo_") == 0
    assert _PASSWORD not in str(result.model_dump(mode="json"))


async def test_new_user_is_removed_when_the_script_removal_loses_the_connection_of_a_blocked_run(
    vault: VaultStandIn,
) -> None:
    firewall = _Firewall()
    await _connector(firewall, block_after_create=True, lose_script_removal=True)

    result = await _run("pfsense.openvpn.user.create", _CREATE)

    assert _is_block_refusal(result), (result.status, result.error)
    assert "not_removed" not in result.extras
    assert _script_removals(firewall, "meho_openvpn_user_create_") == 1
    assert firewall.played("meho_openvpn_user_undo_") == 1
    assert firewall.users == {}
    assert vault.sent("POST", "/v1/secret/data/") == 0
