# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of a call names whose login a secret store accepted.

* The scope helpers keep one id per call, and a step inside a multi-step call
  does not leave its value on the parent.
* The Vault login notes the ``sub`` of the token MEHO sent (the caller's, the
  approver's, an agent's, the check runner's own).
* A real dispatch writes the id on the audit row, and nothing else (no store
  path, no address, no token). A call that reads no secret leaves it empty.
* An audit answer carries ``secret_read_as`` only while ``AUDIT_SECRET_READ_AS``
  is on. With it off the answer is the same as without the field.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.audit_query.query import _build_audit_entry
from meho_backplane.audit_query.schemas import AuditQueryResult, ReplayNode
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.secret_read_as import (
    current_secret_read_as,
    record_secret_read_as,
    token_subject,
    track_secret_read_as,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vault.ops_sys import register_vault_sys_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault

_MARKER_MOUNT = "markermount0zzz"
_MARKER_PATH = "markerenv0zzz/markersvc0zzz"
_MARKER_HOST = "https://vault.test"
_TOKEN_TAIL = "tokentail0zzz"
_TENANT = UUID("00000000-0000-0000-0000-0000000000d4")


def _jwt(sub: str) -> str:
    """A compact token with a made-up ``sub`` claim and a marker signature."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"e30.{payload}.{_TOKEN_TAIL}"


def _operator(sub: str, token_sub: str | None = None, **kwargs: bool) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=_jwt(token_sub or sub),
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        **kwargs,
    )


class _Target:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = type("F", (), {"version": "1.18.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "vault-prod"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", _MARKER_HOST)
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    monkeypatch.delenv("AUDIT_SECRET_READ_AS", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


# ---------------------------------------------------------------------------
# The scope helpers
# ---------------------------------------------------------------------------


def test_empty_outside_a_scope_and_without_a_read() -> None:
    record_secret_read_as("ignored")
    assert current_secret_read_as() is None
    with track_secret_read_as():
        assert current_secret_read_as() is None


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_empty_ids_are_not_recorded(bad: str | None) -> None:
    with track_secret_read_as():
        record_secret_read_as(bad)
        assert current_secret_read_as() is None


def test_id_is_cut_to_printable_text_and_a_fixed_length() -> None:
    with track_secret_read_as():
        record_secret_read_as("ab\x00c\nd" + "x" * 400)
        value = current_secret_read_as()
    assert value is not None
    assert value.startswith("abcdx")
    assert len(value) == 255


def test_a_step_does_not_leave_its_value_on_the_parent() -> None:
    with track_secret_read_as():
        record_secret_read_as("parent-login")
        with track_secret_read_as():
            assert current_secret_read_as() is None
            record_secret_read_as("step-login")
            assert current_secret_read_as() == "step-login"
        assert current_secret_read_as() == "parent-login"
    with track_secret_read_as():
        with track_secret_read_as():
            record_secret_read_as("step-login")
        assert current_secret_read_as() is None


@pytest.mark.asyncio
async def test_a_value_set_in_a_copied_context_reaches_the_call() -> None:
    import asyncio

    with track_secret_read_as():
        await asyncio.create_task(_set("task-login"))
        assert current_secret_read_as() == "task-login"


async def _set(value: str) -> None:
    record_secret_read_as(value)


def test_token_subject_reads_sub_and_falls_back() -> None:
    assert token_subject(_jwt("abc"), "fallback") == "abc"
    for odd in (
        "",
        "not-a-token",
        "a.b.c",
        "a..c",
        f"a.{base64.urlsafe_b64encode(b'[]').decode()}.c",
    ):
        assert token_subject(odd, "fallback") == "fallback"


# ---------------------------------------------------------------------------
# A real dispatch against the in-process Vault fake
# ---------------------------------------------------------------------------


async def _row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    assert len(rows) == 1
    return rows[0]


async def _read(
    operator: Operator, monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> AuditLog:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"password": "made-up-value-0zzz"}
    await register_vault_typed_operations(embedding_service=embedding)
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=_Target(),
        params={"mount": _MARKER_MOUNT, "path": _MARKER_PATH},
    )
    assert result.status == "ok", result.error
    return await _row("vault.kv.read")


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        pytest.param(_operator("person-sub"), "person-sub", id="direct-call"),
        pytest.param(
            _operator("approver-sub"), "approver-sub", id="approved-call-runs-as-approver"
        ),
        pytest.param(_operator("agent:reader"), "agent:reader", id="agent-resumes-itself"),
        pytest.param(_operator("person-sub"), "person-sub", id="hosted-run"),
        pytest.param(
            _operator("sensor-owner-sub", "runner-sub", check_runner_dispatch=True),
            "runner-sub",
            id="check-runner-own-login",
        ),
    ],
)
@pytest.mark.asyncio
async def test_row_names_the_login_the_store_accepted(
    operator: Operator,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    embedding: AsyncMock,
) -> None:
    row = await _read(operator, monkeypatch, embedding)
    assert row.secret_read_as == expected
    if expected == "runner-sub":
        assert row.operator_sub == "sensor-owner-sub"


@pytest.mark.asyncio
async def test_row_holds_only_the_id(monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock) -> None:
    row = await _read(_operator("person-sub"), monkeypatch, embedding)
    for marker in (_MARKER_MOUNT, "markerenv0zzz", _MARKER_HOST, "vault.test", _TOKEN_TAIL):
        assert marker not in (row.secret_read_as or "")


@pytest.mark.asyncio
async def test_call_without_a_secret_read_leaves_it_empty(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    install_fake_vault(monkeypatch)
    await register_vault_sys_typed_operations(embedding_service=embedding)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=_Target(),
        params={},
    )
    assert result.status == "ok", result.error
    assert (await _row("vault.sys.health")).secret_read_as is None


# ---------------------------------------------------------------------------
# What an audit answer shows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_has_the_field_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    row = await _read(_operator("person-sub"), monkeypatch, embedding)
    entry = _build_audit_entry(row, None)
    assert entry.secret_read_as == "person-sub"

    off = entry.model_dump(mode="json")
    assert "secret_read_as" not in off
    assert "secret_read_as" not in entry.model_dump_json()

    monkeypatch.setenv("AUDIT_SECRET_READ_AS", "true")
    get_settings.cache_clear()
    assert entry.model_dump(mode="json")["secret_read_as"] == "person-sub"
    on_json = json.loads(AuditQueryResult(rows=[entry], next_cursor=None).model_dump_json())
    assert on_json["rows"][0]["secret_read_as"] == "person-sub"


@pytest.mark.asyncio
async def test_off_answer_equals_the_answer_without_the_field(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    row = await _read(_operator("person-sub"), monkeypatch, embedding)
    with_value = _build_audit_entry(row, None)
    row.secret_read_as = None
    without = _build_audit_entry(row, None)
    assert with_value.model_dump_json() == without.model_dump_json()
    node = ReplayNode(**with_value.model_dump(), depth=0)
    assert "secret_read_as" not in node.model_dump(mode="json")
