# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step that is sent again after its target session ended checks the block list again.

A multi-step operation runs one step through ``dispatch_child``. The target
says that the login session ended, and the person is blocked (or the block
list becomes unreadable) while that first request runs. The step is not sent
again, and the answer is the refusal. Covered: both ways the dispatcher
repeats a call, after the status 401 and after the connector's own
``is_session_expired`` method.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import block_principal, reset_block_cache
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import DispatchChild, dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests.test_operations_connector_auth_failed import _FakeTarget, _make_operator
from tests.test_operations_session_expired_hook import _OP_ID, _insert_descriptor

pytestmark = pytest.mark.asyncio

_COMPOSITE_OP_ID = "vcfops.composite.one_read"
_COMPOSITE_REF = f"{__name__}._one_read_composite"
_EMBEDDING = [0.1] * 384

#: Every request that reached the target.
sent: list[str] = []
#: What happens while the first request runs (set by each test).
during_first_request: list[Callable[[], Awaitable[None]]] = []


class _Status401Connector(HttpConnector):
    """The first request is answered with "session ended", every later one with ``{}``."""

    product = "vcfops"
    version = "9"
    impl_id = "vcfops-block-401"
    accepts_ingested_operations = True
    first_status = 401

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def invalidate_session(self, target: Any, *, rejected: Any = None) -> None:
        return None

    async def _http_client(self, target: Any) -> httpx.AsyncClient:
        async def _answer(request: httpx.Request) -> httpx.Response:
            sent.append(f"{request.method} {request.url.path}")
            if len(sent) > 1:
                return httpx.Response(200, json={"ok": True})
            for hook in during_first_request:
                await hook()
            return httpx.Response(self.first_status, json={"code": "SESSION_GONE"})

        return httpx.AsyncClient(
            base_url="https://target.test", transport=httpx.MockTransport(_answer)
        )

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


class _HookConnector(_Status401Connector):
    impl_id = "vcfops-block-hook"
    first_status = 403

    def is_session_expired(self, response: httpx.Response) -> bool:
        return response.status_code == 403


async def _one_read_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    return await dispatch_child(connector_id=params["connector_id"], op_id=_OP_ID, params={})


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    during_first_request.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def _block_person() -> None:
    admin = _make_operator().model_copy(
        update={"sub": "admin-person", "tenant_role": TenantRole.TENANT_ADMIN}
    )
    await block_principal(admin, _make_operator().sub, "left the team")


def _make_unreadable(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[None]]:
    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    async def _break_now() -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", _broken)
        reset_block_cache()

    return _break_now


async def _run(cls: type[_Status401Connector], session: AsyncSession) -> Any:
    register_connector_v2(product="vcfops", version="9", impl_id=cls.impl_id, cls=cls)
    await _insert_descriptor(session, cls.impl_id, _EMBEDDING)
    await _insert_descriptor(
        session,
        cls.impl_id,
        _EMBEDDING,
        source_kind="composite",
        handler_ref=_COMPOSITE_REF,
        op_id=_COMPOSITE_OP_ID,
    )
    connector_id = f"{cls.impl_id}-9"
    return await dispatch(
        operator=_make_operator(),
        connector_id=connector_id,
        op_id=_COMPOSITE_OP_ID,
        target=_FakeTarget(name="target-01", host="target.test"),
        params={"connector_id": connector_id},
    )


async def _audit_status(path: str) -> tuple[str, int]:
    async with get_sessionmaker()() as fresh:
        row = (await fresh.execute(select(AuditLog).where(AuditLog.path == path))).scalar_one()
    return row.payload["result_status"], row.status_code


_CONNECTORS = [
    pytest.param(_Status401Connector, id="status-401"),
    pytest.param(_HookConnector, id="is_session_expired"),
]


@pytest.mark.parametrize("cls", _CONNECTORS)
async def test_step_is_sent_again_for_a_person_who_is_not_blocked(
    session: AsyncSession, cls: type[_Status401Connector]
) -> None:
    result = await _run(cls, session)

    assert result.status == "ok", result
    assert len(sent) == 2


@pytest.mark.parametrize("cls", _CONNECTORS)
async def test_step_of_a_blocked_person_is_not_sent_again(
    session: AsyncSession, cls: type[_Status401Connector]
) -> None:
    during_first_request.append(_block_person)

    result = await _run(cls, session)

    assert len(sent) == 1
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "uncertain"
    assert await _audit_status(_OP_ID) == ("denied", 403)
    assert await _audit_status(_COMPOSITE_OP_ID) == ("denied", 403)


@pytest.mark.parametrize("cls", _CONNECTORS)
async def test_step_is_not_sent_again_when_the_block_list_cannot_be_read(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, cls: type[_Status401Connector]
) -> None:
    during_first_request.append(_make_unreadable(monkeypatch))

    result = await _run(cls, session)

    assert len(sent) == 1
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"
