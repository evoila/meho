# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved ``secret.move`` writes to a ``linux:`` place only on the target its card showed.

* The card shows, for a ``linux:`` sink, the name as typed, the Linux
  target it points to (name and id), its host and its SSH port.
* The approved run compares the target id, host and port with the target
  it finds. Another target, host or port, or a card without one of them,
  refuses the run before the sudo password lookup, before the source read
  and before any SSH call.
* When nothing changed, the approved run writes to the target on the card.

The Vault source is faked at the ``_build_client`` seam and the SSH channel
at ``run_capped``; the targets and the approval rows are real rows.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)

from ._vault_fakes import install_fake_client
from .test_secret_broker_linux_sink import _SUDO_CANARY, _Wire
from .test_secret_broker_vault_target import (  # noqa: F401  (fixtures)
    _OPERATOR,
    _SECRET_VALUE,
    _TENANT_ID,
    _env,
    _settings_env,
    stub_embedding_service,
)

pytestmark = pytest.mark.usefixtures("_env", "_linux_targets")

#: The alias the move names for its sink. It points to ``linux-a`` first.
_ALIAS = "app-host"
_SRC = "vault-target:vault-src/secret/a#password"
_DST = f"linux:{_ALIAS}/home/app/.config/app/key#app"


def _reviewer() -> Operator:
    return Operator(
        sub="op-reviewer",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _seed_linux(name: str, host: str, port: int | None, aliases: list[str]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=name,
                aliases=aliases,
                product="linux",
                host=host,
                port=port,
                fqdn=None,
                secret_ref="tenants/x/ssh",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={},
                notes="seeded by test_secret_move_approved_linux_target",
            )
        )
        await session.commit()


@pytest.fixture
async def _linux_targets(_env: None) -> None:  # noqa: F811  (the shared fixture)
    await _seed_linux("linux-a", "host-a.test.invalid", None, [_ALIAS])
    await _seed_linux("linux-b", "host-b.test.invalid", 2222, [])


async def _update(name: str, **values: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(update(TargetORM).where(TargetORM.name == name).values(**values))
        await session.commit()


async def _target_id(name: str) -> str:
    async with get_sessionmaker()() as session:
        found = await session.execute(select(TargetORM.id).where(TargetORM.name == name))
        return str(found.scalar_one())


class _Ssh:
    """A Linux connector whose login and SSH channel are fakes."""

    def __init__(self) -> None:
        self.wire = _Wire(stdout="===MEHO_SINK_OK===\n", home="/home/app")
        self.connector = LinuxSshConnector()
        self.connector._connect = AsyncMock(return_value=object())  # type: ignore[method-assign]
        self.connector._resolve_secret = AsyncMock(  # type: ignore[method-assign]
            return_value={"username": "ops", "sudo_password": _SUDO_CANARY}
        )

    def assert_untouched(self) -> None:
        """No sudo password lookup, no SSH connection, no script."""
        self.connector._resolve_secret.assert_not_awaited()
        self.connector._connect.assert_not_awaited()
        assert self.wire.commands == []


@pytest.fixture
def ssh() -> Iterator[_Ssh]:
    fake = _Ssh()
    with (
        patch("meho_backplane.connectors.linux._sudo.run_capped", fake.wire.run_capped),
        patch(
            "meho_backplane.connectors.linux.secret_endpoint.get_or_create_connector_instance",
            return_value=fake.connector,
        ),
    ):
        yield fake
    _CONNECTOR_INSTANCE_CACHE.pop(LinuxSshConnector, None)


async def _ask(params: dict[str, Any]) -> ApprovalRequest:
    """Send the move without approval; return the approval row it waits on."""
    result = await dispatch(
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
    )
    assert result.status == "awaiting_approval", result
    async with get_sessionmaker()() as session:
        (row,) = (await session.execute(select(ApprovalRequest))).scalars().all()
    return row


async def _approve_and_run(request_id: UUID) -> OperationResult:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=_reviewer())
        await session.commit()
    result: OperationResult = await resume_dispatch_after_approval(
        operator=_reviewer(), request=request, params=None
    )
    return result


def _card(row: ApprovalRequest) -> dict[str, Any]:
    return dict(dict(row.proposed_effect)["preview"])


def _message(result: OperationResult) -> str:
    return str((result.extras or {}).get("exception_message"))


def _assert_refused(result: OperationResult, says: str, fake: Any, ssh: _Ssh) -> None:
    assert result.status == "error", result
    assert result.error == "connector_error: LinuxTargetChangedError"
    message = _message(result)
    assert says in message
    assert "Nothing was read or written. Send a new request" in message
    assert _SECRET_VALUE not in message
    assert fake.secrets.kv.v2.read_calls == []  # the source was not read
    ssh.assert_untouched()


# -- what the card shows -------------------------------------------------


async def test_the_card_shows_the_linux_target_its_host_and_its_port(
    monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    card = _card(await _ask({"from": _SRC, "to": _DST}))

    assert card["sink"] == {
        "kind": "linux",
        "ref": f"{_ALIAS}/home/app/.config/app/key#app",
        "target": _ALIAS,
        "target_id": await _target_id("linux-a"),
        "target_name": "linux-a",
        "host": "host-a.test.invalid",
        "port": "22",  # the target names no port, so MEHO dials 22
        "secret_exists": "not known",
    }
    assert _SECRET_VALUE not in json.dumps(card)
    ssh.assert_untouched()


async def test_a_name_that_finds_no_linux_target_gets_no_target_on_the_card(
    monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    card = _card(await _ask({"from": _SRC, "to": "linux:no-such-host/home/app/key#app"}))

    assert "target_id" not in card["sink"]
    assert card["sink"]["target"] == "no-such-host"


# -- the approved run checks the target ----------------------------------


async def test_the_approved_run_writes_to_the_target_on_the_card(
    monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert fake.secrets.kv.v2.read_calls  # the source was read
    assert len(ssh.wire.commands) == 2  # the home lookup, then the write
    assert base64.b64encode(_SECRET_VALUE.encode()).decode() in (ssh.wire.stdin[1] or "")
    ssh.connector._resolve_secret.assert_awaited()
    (dialed,) = {call.args[0].host for call in ssh.connector._connect.await_args_list}
    assert dialed == "host-a.test.invalid"


async def test_an_alias_moved_to_another_linux_target_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.secrets.kv.v2.read_calls.clear()
    await _update("linux-a", aliases=[])
    await _update("linux-b", aliases=[_ALIAS])

    result = await _approve_and_run(row.id)

    _assert_refused(result, "now points to Linux target 'linux-b'", fake, ssh)
    assert await _target_id("linux-a") in _message(result)


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"host": "host-c.test.invalid"}, "the host of the sink target 'linux-a' changed"),
        ({"port": 2200}, "the port of the sink target 'linux-a' changed"),
    ],
    ids=["host", "port"],
)
async def test_a_new_host_or_port_on_the_same_target_refuses_the_run(
    values: dict[str, Any], says: str, monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.secrets.kv.v2.read_calls.clear()
    await _update("linux-a", **values)

    result = await _approve_and_run(row.id)

    _assert_refused(result, says, fake, ssh)


@pytest.mark.parametrize("key", ["target_id", "host", "port"])
async def test_a_card_without_the_target_host_or_port_refuses_the_run(
    key: str, monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.secrets.kv.v2.read_calls.clear()
    effect = dict(row.proposed_effect)
    card = _card(row)
    card["sink"] = {k: v for k, v in card["sink"].items() if k != key}
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id)
            .values(proposed_effect={**effect, "preview": card})
        )
        await session.commit()

    result = await _approve_and_run(row.id)

    _assert_refused(result, "cannot tell what was approved", fake, ssh)


async def test_a_name_with_no_target_at_park_refuses_the_run_once_it_finds_one(
    monkeypatch: pytest.MonkeyPatch, ssh: _Ssh
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": "linux:new-host/home/app/key#app"})
    fake.secrets.kv.v2.read_calls.clear()
    await _update("linux-b", aliases=["new-host"])

    result = await _approve_and_run(row.id)

    _assert_refused(result, "cannot tell what was approved", fake, ssh)
