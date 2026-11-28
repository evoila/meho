# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved ``secret.move`` acts on the Vault targets its approval card showed.

* The card shows, for each ``vault-target`` side, the name as typed, the
  target it points to (name and id), that target's Vault role and its
  Vault login mount.
* The approved run compares the target ids, roles and mounts with the
  card. Another target, role or mount, or a card without them, refuses the
  run before any Vault login. So does a target that is gone by then.
* The card says whether the sink secret exists and, with ``merge``,
  whether its field exists: "yes", "no" or "not known".
* A planted value stays out of every answer, log line and stored row of
  the whole flow (wait for approval, approve, run): the approval row, the
  audit rows, the trace, every other table and the broadcast stream.

The Vault client is faked at the single ``_build_client`` seam; no real HTTP.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import hvac
import pytest
import structlog
from sqlalchemy import delete, select, update

import meho_backplane.broadcast.client as broadcast_client
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.move_preview import build_secret_move_preview
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Base, DispatchTrace, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from tests.conftest import MehoLogs

from ._vault_fakes import install_fake_client
from .test_secret_broker_vault_target import (  # noqa: F401  (fixtures)
    _OPERATOR,
    _SECRET_VALUE,
    _TENANT_ID,
    _env,
    _roles,
    _seed_target,
    _settings_env,
    stub_embedding_service,
)

pytestmark = pytest.mark.usefixtures("_env")

#: The alias the move names for its sink. It points to ``vault-dst`` first.
_ALIAS = "vault-prod"
_SRC = "vault-target:vault-src/secret/a#password"
_DST = f"vault-target:{_ALIAS}/secret/b#password"


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


async def _set_aliases(name: str, aliases: list[str]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(TargetORM).where(TargetORM.name == name).values(aliases=aliases)
        )
        await session.commit()


async def _target_id(name: str) -> str:
    async with get_sessionmaker()() as session:
        found = await session.execute(select(TargetORM.id).where(TargetORM.name == name))
        return str(found.scalar_one())


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


@pytest.fixture
async def _alias(_env: None) -> None:  # noqa: F811  (the shared fixture)
    """Point the alias to ``vault-dst``, after the shared targets exist."""
    await _seed_target("vault-other", role="role-other")
    await _set_aliases("vault-dst", [_ALIAS])


# -- what the card shows -------------------------------------------------


@pytest.mark.usefixtures("_alias")
async def test_the_card_shows_the_real_target_and_its_role_for_each_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    card = _card(await _ask({"from": _SRC, "to": _DST}))

    assert card["source"]["target"] == "vault-src"
    assert card["source"]["target_name"] == "vault-src"
    assert card["source"]["target_id"] == await _target_id("vault-src")
    assert card["source"]["vault_role"] == "role-src"
    assert card["sink"]["target"] == _ALIAS
    assert card["sink"]["target_name"] == "vault-dst"
    assert card["sink"]["target_id"] == await _target_id("vault-dst")
    assert card["sink"]["vault_role"] == "role-dst"
    assert card["source"]["vault_mount"] == card["sink"]["vault_mount"] == "jwt"
    assert _SECRET_VALUE not in json.dumps(card)


# -- the approved run checks the targets ---------------------------------


@pytest.mark.usefixtures("_alias")
async def test_the_approved_run_uses_the_targets_on_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.auth.jwt.login_calls.clear()  # the sink lookup for the card

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert _roles(fake) == ["role-src", "role-dst"]


@pytest.mark.usefixtures("_alias")
async def test_a_name_that_points_to_another_target_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.auth.jwt.login_calls.clear()
    fake.secrets.kv.v2.read_calls.clear()
    await _set_aliases("vault-dst", [])
    await _set_aliases("vault-other", [_ALIAS])

    result = await _approve_and_run(row.id)

    assert result.status == "error", result
    assert result.error == "connector_error: VaultTargetChangedError"
    message = _message(result)
    assert "now points to Vault target 'vault-other'" in message
    assert await _target_id("vault-dst") in message
    assert "Nothing was read or written" in message
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def _set_extras(name: str, extras: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(update(TargetORM).where(TargetORM.name == name).values(extras=extras))
        await session.commit()


@pytest.mark.parametrize(
    ("extras", "says"),
    [
        ({"vault_role": "role-new"}, "the Vault role of the sink target 'vault-dst' changed"),
        (
            {"vault_role": "role-dst", "vault_mount": "jwt-other"},
            "the Vault login mount of the sink target 'vault-dst' changed",
        ),
    ],
    ids=["role", "mount"],
)
@pytest.mark.usefixtures("_alias")
async def test_a_changed_vault_role_or_mount_refuses_the_run(
    extras: dict[str, Any], says: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.auth.jwt.login_calls.clear()
    fake.secrets.kv.v2.read_calls.clear()
    await _set_extras("vault-dst", extras)

    result = await _approve_and_run(row.id)

    assert result.status == "error", result
    assert result.error == "connector_error: VaultTargetChangedError"
    message = _message(result)
    assert says in message
    assert "Nothing was read or written. Send a new request" in message
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def test_a_target_gone_at_the_run_is_named_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": "vault-target:vault-dst/secret/b#password"})
    fake.auth.jwt.login_calls.clear()
    fake.secrets.kv.v2.read_calls.clear()
    async with get_sessionmaker()() as session:
        await session.execute(delete(TargetORM).where(TargetORM.name == "vault-dst"))
        await session.commit()

    result = await _approve_and_run(row.id)

    assert result.status == "error", result
    assert result.error == "connector_error: VaultTargetNotFoundError"
    assert _message(result) == (
        "no Vault target named 'vault-dst' was found in your tenant. Nothing was read or written"
    )
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


@pytest.mark.parametrize(
    ("side", "key"),
    [
        ("source", "target_id"),
        ("sink", "target_id"),
        ("sink", "vault_role"),
        ("sink", "vault_mount"),
    ],
)
@pytest.mark.usefixtures("_alias")
async def test_a_card_without_the_target_id_refuses_the_run(
    side: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card made before MEHO showed target ids cannot be checked, so it is refused."""
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _SRC, "to": _DST})
    fake.auth.jwt.login_calls.clear()
    effect = dict(row.proposed_effect)
    card = _card(row)
    card[side] = {k: v for k, v in card[side].items() if k != key}
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id)
            .values(proposed_effect={**effect, "preview": card})
        )
        await session.commit()

    result = await _approve_and_run(row.id)

    assert result.status == "error", result
    assert result.error == "connector_error: VaultTargetChangedError"
    assert "cannot tell what was approved" in _message(result)
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.put_calls == []


# -- whether the sink secret exists --------------------------------------


def _read_answer(fake: Any, monkeypatch: pytest.MonkeyPatch, sink: Any) -> None:
    """Make the sink path answer *sink*: an exception to raise or a payload."""
    kv = fake.secrets.kv.v2
    real_read = kv.read_secret_version

    def read(path: str, **kwargs: Any) -> dict[str, Any]:
        if path != "secret/b":
            return real_read(path, **kwargs)
        if isinstance(sink, Exception):
            raise sink
        return dict(sink)

    monkeypatch.setattr(kv, "read_secret_version", read)


_DELETED = {"data": {"data": None, "metadata": {"version": 3, "deletion_time": "2026-11-01"}}}


@pytest.mark.parametrize(
    ("sink", "merge", "expected"),
    [
        (None, False, {"secret_exists": "yes"}),
        (None, True, {"secret_exists": "yes", "field_exists": "yes"}),
        (
            {"data": {"data": {"username": "demo"}, "metadata": {"version": 2}}},
            True,
            {"secret_exists": "yes", "field_exists": "no"},
        ),
        (
            hvac.exceptions.InvalidPath("missing"),
            True,
            {"secret_exists": "no", "field_exists": "no"},
        ),
        (_DELETED, True, {"secret_exists": "yes", "field_exists": "not known"}),
        (hvac.exceptions.Forbidden("denied"), False, {"secret_exists": "not known"}),
        (
            hvac.exceptions.Forbidden("denied"),
            True,
            {"secret_exists": "not known", "field_exists": "not known"},
        ),
    ],
    ids=[
        "exists",
        "merge-field-exists",
        "merge-field-missing",
        "missing",
        "latest-version-deleted",
        "vault-refuses",
        "merge-vault-refuses",
    ],
)
async def test_the_card_says_whether_the_sink_secret_exists(
    sink: Any, merge: bool, expected: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    if sink is not None:
        _read_answer(fake, monkeypatch, sink)

    card = _card(
        await _ask({"from": _SRC, "to": "vault-target:vault-dst/secret/b#password", "merge": merge})
    )

    assert {k: v for k, v in card["sink"].items() if k.endswith("_exists")} == expected
    assert _SECRET_VALUE not in json.dumps(card)
    assert fake.secrets.kv.v2.put_calls == []


async def test_a_sink_kind_that_cannot_tell_says_not_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    card = _card(await _ask({"from": _SRC, "to": "keycloak:kc/meho/operator-a#password"}))

    assert card["sink"]["secret_exists"] == "not known"


async def test_the_agents_own_preview_shows_only_the_refs_and_makes_no_vault_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    answer = await preview_dispatch(
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params={"from": _SRC, "to": "vault-target:vault-dst/secret/b#password", "merge": True},
    )

    text = json.dumps(answer, default=str)
    assert '"target": "vault-dst"' in text, answer  # the name as typed
    for added in ("secret_exists", "field_exists", "target_id", "target_name", "vault_role"):
        assert added not in text, answer
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


@pytest.mark.parametrize("agent_preview", [True, False])
async def test_only_the_approval_request_looks_up_the_sink(
    agent_preview: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's own preview shows the refs only; the approval request looks."""
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    ctx = PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # the builder does not read it
        connector_instance=None,
        operator=_OPERATOR,
        target=None,
        params={"from": "vault:secret/a#password", "to": "vault:secret/b#password"},
        agent_preview=agent_preview,
    )

    preview = await build_secret_move_preview(ctx)

    assert preview is not None
    if agent_preview:
        assert preview == {
            "action": "secret.move",
            "source": {"kind": "vault", "ref": "secret/a#password"},
            "sink": {"kind": "vault", "ref": "secret/b#password"},
            "merge": False,
        }
        assert fake.auth.jwt.login_calls == []
        assert fake.secrets.kv.v2.read_calls == []
    else:
        assert preview["sink"]["secret_exists"] == "yes"
        assert [c["path"] for c in fake.secrets.kv.v2.read_calls] == ["secret/b"]


# -- a planted value stays out of everything -----------------------------

#: A made-up value that is easy to find; it is in the source and in a
#: second field of the sink secret, which the card lookup reads.
_MARKER = "planted-move-marker-7f3a9c"


class _BroadcastRecorder:
    """Stand-in for the broadcast Valkey client: keeps every stream entry and every call."""

    def __init__(self) -> None:
        self.streams: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any, Any]] = []

    async def xadd(self, key: str, fields: dict[str, Any], *args: Any, **kwargs: Any) -> str:
        self.streams.setdefault(key, []).append(dict(fields))
        return f"1700000000000-{len(self.streams[key])}"

    async def xrange(self, key: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(f"1700000000000-{i}", e) for i, e in enumerate(self.streams.get(key, []), 1)]

    def register_script(self, _script: str) -> Any:
        async def run(keys: Any = (), args: Any = (), **_kwargs: Any) -> list[Any]:
            self.calls.append(("script", keys, args))
            stream_key, payload = keys[0], args[0]
            return [1, await self.xadd(stream_key, {"event": payload})]

        return run

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return call

    async def read_everything(self) -> str:
        """Every entry of every stream (read with ``XRANGE``) and every other call."""
        entries = [await self.xrange(key, "-", "+") for key in self.streams]
        return json.dumps({"entries": entries, "calls": self.calls}, default=str)


async def _all_stored_rows() -> str:
    """Every row of every table, as one text."""
    parts: list[str] = []
    async with get_sessionmaker()() as session:
        for table in Base.metadata.sorted_tables:
            rows = (await session.execute(select(table))).mappings().all()
            parts.extend(json.dumps(dict(r), default=str) for r in rows)
    return "\n".join(parts)


@pytest.mark.parametrize("write_refused", [False, True], ids=["moved", "write-refused"])
@pytest.mark.usefixtures("_alias")
async def test_a_planted_value_is_in_no_answer_log_or_stored_row(
    write_refused: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    meho_logs: MehoLogs,
) -> None:
    """On the success path, and when Vault refuses the write after the source was read."""
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT_ID,
                slug="secret-move-planted",
                name="secret.move planted value",
                flight_recorder_enabled=True,
            )
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()
    fake = install_fake_client(monkeypatch, secret={"password": _MARKER, "note": _MARKER})
    if write_refused:
        fake.secrets.kv.v2.put_exc = hvac.exceptions.Forbidden("permission denied")
    stream = _BroadcastRecorder()
    monkeypatch.setattr(broadcast_client, "_CLIENT", stream)
    monkeypatch.setattr(broadcast_client, "_BLOCKING_CLIENT", stream)
    assert broadcast_client.get_broadcast_client() is stream
    caplog.set_level(logging.DEBUG)

    with structlog.testing.capture_logs() as logs:
        row = await _ask({"from": _SRC, "to": _DST, "merge": True, "reason": "planted check"})
        result = await _approve_and_run(row.id)

    if write_refused:
        assert result.error == "connector_error: SecretMergeRefusedError", result
    else:
        assert result.status == "ok", result
    put = fake.secrets.kv.v2.put_calls[0]
    assert put["secret"]["password"] == _MARKER  # the move did carry the value
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(DispatchTrace))).scalars().first() is not None
    for place, text in (
        ("answer", result.model_dump_json()),
        ("log", json.dumps(logs, default=str) + meho_logs.text()),
        ("stored rows", await _all_stored_rows()),
        ("broadcast stream", await stream.read_everything()),
    ):
        assert _MARKER not in text, place
