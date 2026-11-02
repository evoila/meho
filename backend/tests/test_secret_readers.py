# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`SecretReaders`, the per-caller read check of the connector caches."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultRoleDeniedError
from meho_backplane.connectors._shared.secret_readers import SecretReaders, caller_id
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator

_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


@dataclass(frozen=True)
class _Target:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT_ID


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"{sub}.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def test_a_caller_has_read_only_after_record() -> None:
    readers = SecretReaders()
    target = _Target()
    alice = _person("alice")

    assert not readers.has_read(target, alice)
    readers.record(target, alice)
    assert readers.has_read(target, alice)
    assert readers.callers(target) == frozenset({("operator", "alice")})


def test_another_caller_and_another_target_are_not_covered() -> None:
    readers = SecretReaders()
    target, other = _Target(), _Target()
    readers.record(target, _person("alice"))

    assert not readers.has_read(target, _person("bob"))
    assert not readers.has_read(other, _person("alice"))


def test_the_same_sub_in_another_tenant_is_another_target() -> None:
    readers = SecretReaders()
    shared_id = uuid.uuid4()
    target = _Target(id=shared_id)
    same_id_other_tenant = _Target(id=shared_id, tenant_id=uuid.uuid4())
    readers.record(target, _person("alice"))

    assert not readers.has_read(same_id_other_tenant, _person("alice"))


def test_the_system_operator_is_never_recorded() -> None:
    readers = SecretReaders()
    target = _Target()
    system = synthesise_system_operator()

    readers.record(target, system)

    assert not readers.has_read(target, system)
    assert readers.callers(target) == frozenset()


def test_the_check_runner_under_a_persons_sub_is_another_caller() -> None:
    """A Sensor may run under its creator's ``sub``; the runner's read is still its own."""
    readers = SecretReaders()
    target = _Target()
    alice = _person("alice")
    runner_as_alice = Operator(
        sub="alice",
        name=None,
        email=None,
        raw_jwt="runner.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        check_runner_dispatch=True,
    )
    readers.record(target, alice)

    assert caller_id(runner_as_alice) == ("check-runner", "alice")
    assert not readers.has_read(target, runner_as_alice)

    readers.record(target, runner_as_alice)

    assert readers.has_read(target, runner_as_alice)
    assert readers.callers(target) == frozenset({("operator", "alice"), ("check-runner", "alice")})


@pytest.mark.asyncio
async def test_ensure_read_runs_the_read_once_per_caller() -> None:
    readers = SecretReaders()
    target = _Target()
    alice = _person("alice")
    reads: list[str] = []

    async def _read() -> dict[str, str]:
        reads.append("alice")
        return {"username": "svc"}

    await readers.ensure_read(target, alice, _read)
    await readers.ensure_read(target, alice, _read)

    assert reads == ["alice"]
    assert readers.has_read(target, alice)


@pytest.mark.asyncio
async def test_a_refused_read_raises_and_records_nothing() -> None:
    readers = SecretReaders()
    target = _Target()
    bob = _person("bob")

    async def _refused() -> None:
        raise VaultRoleDeniedError("read refused for bob")

    with pytest.raises(VaultRoleDeniedError):
        await readers.ensure_read(target, bob, _refused)

    assert not readers.has_read(target, bob)


@pytest.mark.asyncio
async def test_the_system_operator_reads_every_time() -> None:
    readers = SecretReaders()
    target = _Target()
    system = synthesise_system_operator()
    reads = 0

    async def _read() -> None:
        nonlocal reads
        reads += 1

    await readers.ensure_read(target, system, _read)
    await readers.ensure_read(target, system, _read)

    assert reads == 2


def test_forget_drops_one_target_and_clear_drops_all() -> None:
    readers = SecretReaders()
    target, other = _Target(), _Target()
    alice, bob = _person("alice"), _person("bob")
    readers.record(target, alice)
    readers.record(target, bob)
    readers.record(other, alice)

    readers.forget(target)

    assert readers.callers(target) == frozenset()
    assert readers.has_read(other, alice)

    readers.clear()

    assert not readers.has_read(other, alice)
