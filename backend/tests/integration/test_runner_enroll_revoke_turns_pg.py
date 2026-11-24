# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: enrollment and revocation of one runner take turns.

Both read the runner's Keycloak client and write it back. They take one
advisory lock per runner, so the second one starts after the first one is
done, also on two replicas. SQLite (the unit-suite driver) has no advisory
lock, so only a real Postgres can show this.

Keycloak is a small stateful fake behind the real admin client. Its ``PUT``
works like the Keycloak Admin API: a field that the body leaves out keeps its
value, and an attribute with an empty value is removed. The first ``PUT``
waits until the test lets it go.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient, KeycloakAdminError
from meho_backplane.auth.runner_enrollment import (
    EnrollmentCodeInvalidError,
    EnrollmentRunnerMismatchError,
    enroll_runner_key,
    new_enrollment_code,
)
from meho_backplane.auth.runner_principals import (
    RunnerPrincipalNotFoundError,
    RunnerPrincipalService,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.runner.login import public_jwk

from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_INTERNAL_ID = "cc000000-0000-0000-0000-000000000042"
_ADMIN_URL = "https://keycloak.test/admin/realms/meho"
_TOKEN_URL = "https://keycloak.test/realms/meho/protocol/openid-connect/token"
_NAME = "edge-turns"
#: Long enough for the second request to reach Keycloak if nothing held it back.
_OVERLAP_SECONDS = 0.5

_skip_no_docker = pytest.mark.skipif(
    not (Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST")),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


class _Keycloak:
    """One client; the first ``PUT`` waits for :attr:`go`."""

    def __init__(self) -> None:
        self.client: dict[str, Any] = {"id": _INTERNAL_ID, "enabled": True, "attributes": {}}
        self.calls: list[str] = []
        self.first_put = asyncio.Event()
        self.go = asyncio.Event()
        self.lose_answers = False
        self.time_out_next_put = False
        self.late_puts: list[dict[str, Any]] = []

    def _get(self, _request: httpx.Request) -> httpx.Response:
        self.calls.append("GET")
        return httpx.Response(200, json=self.client)

    async def _put(self, request: httpx.Request) -> httpx.Response:
        self.calls.append("PUT")
        if not self.first_put.is_set():
            self.first_put.set()
            await self.go.wait()
        sent = json.loads(request.content)
        if self.time_out_next_put:
            # MEHO stops waiting; Keycloak applies the write later (apply_late_puts).
            self.time_out_next_put = False
            self.late_puts.append(sent)
            raise httpx.ReadTimeout("no answer in time", request=request)
        self.apply(sent)
        return httpx.Response(502 if self.lose_answers else 204)

    def apply(self, sent: dict[str, Any]) -> None:
        attributes = {**self.client.get("attributes", {}), **sent.get("attributes", {})}
        self.client = {
            **self.client,
            **sent,
            "attributes": {k: v for k, v in attributes.items() if v != ""},
        }

    def apply_late_puts(self) -> None:
        for sent in self.late_puts:
            self.apply(sent)
        self.late_puts.clear()

    def mount(self, router: respx.MockRouter) -> None:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-token"})
        )
        url = f"{_ADMIN_URL}/clients/{_INTERNAL_ID}"
        router.get(url).mock(side_effect=self._get)
        router.put(url).mock(side_effect=self._put)

    @property
    def has_key(self) -> bool:
        return "jwks.string" in self.client["attributes"]


@pytest.fixture
async def keycloak(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Keycloak]:
    def _from_settings(*_args: object) -> KeycloakAdminClient:
        return KeycloakAdminClient(
            admin_url=_ADMIN_URL,
            token_url=_TOKEN_URL,
            client_id="meho-admin",
            client_secret="made-up",
        )

    monkeypatch.setattr(KeycloakAdminClient, "from_settings", _from_settings)
    fake = _Keycloak()
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        yield fake


@pytest.fixture(autouse=True)
def _issuer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_runner() -> str:
    runner_id = uuid.uuid4()
    code, code_hash, expires_at = new_enrollment_code(runner_id)
    async with get_sessionmaker()() as session:
        session.add(
            RunnerPrincipal(
                id=runner_id,
                tenant_id=_TENANT,
                name=_NAME,
                keycloak_client_id=f"runner:{_NAME}",
                keycloak_internal_id=_INTERNAL_ID,
                owner_sub="owner",
                created_by_sub="owner",
                revoked=False,
                enrollment_code_hash=code_hash,
                enrollment_code_expires_at=expires_at,
            )
        )
        await session.commit()
    return code


async def _row() -> RunnerPrincipal:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == _NAME))
        ).scalar_one()


def _jwk() -> dict[str, str]:
    return public_jwk(ec.generate_private_key(ec.SECP256R1()))


async def _overlap(keycloak: _Keycloak, first: asyncio.Task[Any], second: Any) -> tuple[Any, Any]:
    """Start *second* while *first* is inside Keycloak, then let both finish."""
    await asyncio.wait_for(keycloak.first_put.wait(), timeout=10)
    calls_before = list(keycloak.calls)
    other = asyncio.create_task(second)
    await asyncio.sleep(_OVERLAP_SECONDS)
    assert not other.done()
    assert keycloak.calls == calls_before  # the second one waits for its turn
    keycloak.go.set()
    one, two = await asyncio.wait_for(
        asyncio.gather(first, other, return_exceptions=True), timeout=20
    )
    return one, two


def _revoke() -> Any:
    return RunnerPrincipalService().revoke(_TENANT, _NAME, operator_sub="op-admin")


@_skip_no_docker
async def test_a_revoke_during_an_enrollment_leaves_the_client_disabled_and_without_a_key(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    code = await _seed_runner()
    enroll = asyncio.create_task(enroll_runner_key(code, _jwk(), _NAME))
    enrolled, revoked = await _overlap(keycloak, enroll, _revoke())
    assert enrolled.client_id == f"runner:{_NAME}"
    assert revoked.revoked is True
    assert keycloak.client["enabled"] is False
    assert not keycloak.has_key
    assert keycloak.client["clientAuthenticatorType"] == "client-secret"
    row = await _row()
    assert row.revoked is True
    assert row.enrollment_code_hash is None


@_skip_no_docker
async def test_an_enrollment_during_a_revoke_is_refused(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    code = await _seed_runner()
    revoke = asyncio.create_task(_revoke())
    revoked, enrolled = await _overlap(keycloak, revoke, enroll_runner_key(code, _jwk(), _NAME))
    assert revoked.revoked is True
    assert isinstance(enrolled, EnrollmentCodeInvalidError)
    assert keycloak.client["enabled"] is False
    assert not keycloak.has_key
    assert (await _row()).enrolled_key_id is None


@_skip_no_docker
async def test_two_keys_with_one_code_at_once_enroll_one_key(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    code = await _seed_runner()
    first_key = _jwk()
    enroll = asyncio.create_task(enroll_runner_key(code, first_key, _NAME))
    first, second = await _overlap(keycloak, enroll, enroll_runner_key(code, _jwk(), _NAME))
    assert first.key_id == ec_key_id(first_key)
    assert isinstance(second, EnrollmentCodeInvalidError)
    (jwk,) = json.loads(keycloak.client["attributes"]["jwks.string"])["keys"]
    assert jwk["kid"] == first.key_id == (await _row()).enrolled_key_id


@_skip_no_docker
async def test_two_revokes_at_once_revoke_one_time(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    await _seed_runner()
    revoke = asyncio.create_task(_revoke())
    first, second = await _overlap(keycloak, revoke, _revoke())
    assert first.revoked is True
    assert isinstance(second, RunnerPrincipalNotFoundError)
    assert keycloak.client["enabled"] is False


async def _expire_code() -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == _NAME))
        ).scalar_one()
        row.enrollment_code_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


@_skip_no_docker
async def test_the_code_keeps_its_key_when_keycloak_stored_it_and_the_answer_was_lost(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    """Keycloak has the key and MEHO has no answer. Only that key can finish."""
    code = await _seed_runner()
    first_key = _jwk()
    keycloak.go.set()
    keycloak.lose_answers = True
    with pytest.raises(KeycloakAdminError, match="PUT failed"):
        await enroll_runner_key(code, first_key, _NAME)
    keycloak.lose_answers = False
    assert keycloak.has_key
    row = await _row()
    assert row.enrolled_key_id == ec_key_id(first_key)
    assert row.key_enrolled_at is None
    with pytest.raises(EnrollmentCodeInvalidError):
        await enroll_runner_key(code, _jwk(), _NAME)
    (jwk,) = json.loads(keycloak.client["attributes"]["jwks.string"])["keys"]
    assert jwk["kid"] == ec_key_id(first_key)
    await _expire_code()
    finished = await enroll_runner_key(code, first_key, _NAME)
    assert finished.key_id == ec_key_id(first_key)
    assert (await _row()).key_enrolled_at is not None
    await _revoke()
    assert keycloak.client["enabled"] is False
    assert not keycloak.has_key


@_skip_no_docker
async def test_a_key_write_that_keycloak_applies_after_a_revoke_leaves_the_client_disabled(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    """MEHO stops waiting for Keycloak, a revoke runs, then Keycloak applies the old write."""
    code = await _seed_runner()
    first_key = _jwk()
    keycloak.go.set()
    keycloak.time_out_next_put = True
    with pytest.raises(KeycloakAdminError, match="network error"):
        await enroll_runner_key(code, first_key, _NAME)
    revoked = await _revoke()
    assert revoked.revoked is True
    assert keycloak.client["enabled"] is False
    keycloak.apply_late_puts()
    assert keycloak.client["enabled"] is False
    with pytest.raises(EnrollmentCodeInvalidError):
        await enroll_runner_key(code, first_key, _NAME)
    assert keycloak.client["enabled"] is False
    assert (await _row()).revoked is True


@_skip_no_docker
async def test_the_code_of_another_runner_changes_nothing(
    pg_engine: None,  # noqa: F811
    keycloak: _Keycloak,
) -> None:
    code = await _seed_runner()
    keycloak.go.set()
    with pytest.raises(EnrollmentRunnerMismatchError):
        await enroll_runner_key(code, _jwk(), "another-site")
    assert keycloak.calls == []
    assert (await _row()).enrolled_key_id is None
    enrolled = await enroll_runner_key(code, _jwk(), _NAME)
    assert (await _row()).enrolled_key_id == enrolled.key_id
