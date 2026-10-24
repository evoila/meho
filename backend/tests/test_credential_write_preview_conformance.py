# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry sweep: every credential write that can wait for approval has a preview (#4850).

An op pinned ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`) gets no
generic echo of its params when it waits for approval, because that echo
cannot be trusted to hide every secret. Only a bespoke preview builder shows
the approver anything. Without one, the approver sees the op id, the target
and "credential write, redacted", and approves without knowing which user,
secret or host changes.

This sweep runs every typed-op registrar, takes each registered op that is
pinned ``credential_write`` and can wait for approval, and checks that it has
a registered preview builder. An op can wait for approval when it has
``requires_approval=True`` (every call waits) or a ``safety_level`` above
``safe`` (an agent's call waits). A new credential write without a builder
now fails CI.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import or_, select

from meho_backplane.broadcast.events import _CREDENTIAL_WRITE_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Credential writes that may wait for approval with no preview, each with
#: its reason. Empty today: every such op has a builder. Add an op here only
#: with a comment that says why the approver cannot be shown what changes.
_NO_PREVIEW_CREDENTIAL_WRITES: frozenset[str] = frozenset()

#: Ops this sweep must see, so a broken seed cannot make it pass with an
#: empty set. One per connector family that #4850 gave a builder.
_KNOWN_PARKABLE_CREDENTIAL_WRITES: frozenset[str] = frozenset(
    {
        "keycloak.user.reset_password",
        "vault.auth.userpass.write",
        "k8s.secret.create",
        "sddc.host.commission",
        "holodeck.router.patch",
        "vmware.composite.vm.guest.file.read",
    }
)


def _credential_writes_missing_preview(
    op_ids: Iterable[str],
    *,
    registered_builders: Mapping[str, Any],
    exemptions: frozenset[str],
) -> list[str]:
    """Return the credential-write op ids with no builder and no exemption, sorted."""
    return sorted(
        op_id
        for op_id in set(op_ids)
        if op_id in _CREDENTIAL_WRITE_OPS
        and op_id not in registered_builders
        and op_id not in exemptions
    )


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Fixed embedding stub so the registrar pass does not load a model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _seed_and_collect_parkable_credential_writes(stub: AsyncMock) -> list[str]:
    """Run every typed-op registrar; return credential writes that can wait for approval."""
    await run_typed_op_registrars(embedding_service=stub)
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.op_id.in_(_CREDENTIAL_WRITE_OPS),
                or_(
                    EndpointDescriptor.requires_approval.is_(True),
                    EndpointDescriptor.safety_level != "safe",
                ),
            )
        )
    return sorted(set(rows.scalars().all()))


@pytest.mark.asyncio
async def test_every_parkable_credential_write_has_a_preview_builder(
    stub_embedding_service: AsyncMock,
) -> None:
    op_ids = await _seed_and_collect_parkable_credential_writes(stub_embedding_service)

    seeded = set(op_ids)
    assert seeded >= _KNOWN_PARKABLE_CREDENTIAL_WRITES, (
        "expected these credential writes to be registered and able to wait for "
        f"approval; missing {sorted(_KNOWN_PARKABLE_CREDENTIAL_WRITES - seeded)}"
    )

    missing = _credential_writes_missing_preview(
        op_ids,
        registered_builders=_PREVIEW_BUILDERS,
        exemptions=_NO_PREVIEW_CREDENTIAL_WRITES,
    )
    assert not missing, (
        "credential_write ops that can wait for approval but have no preview builder "
        f"(the approver would see no account, secret or host): {missing}. Register one "
        "with register_preview_builder(op_id, ...) that shows what changes and never a "
        "secret value, or add the op to _NO_PREVIEW_CREDENTIAL_WRITES with the reason."
    )


@pytest.mark.asyncio
async def test_sweep_flags_a_removed_builder(stub_embedding_service: AsyncMock) -> None:
    """Removing a real builder makes the sweep flag that op (the sweep is not vacuous)."""
    op_ids = await _seed_and_collect_parkable_credential_writes(stub_embedding_service)
    victim = "keycloak.user.reset_password"
    assert victim in op_ids
    builders = {k: v for k, v in _PREVIEW_BUILDERS.items() if k != victim}
    missing = _credential_writes_missing_preview(
        op_ids, registered_builders=builders, exemptions=_NO_PREVIEW_CREDENTIAL_WRITES
    )
    assert missing == [victim]


def test_an_exemption_suppresses_the_flag() -> None:
    op_id = "vault.auth.userpass.write"
    missing = _credential_writes_missing_preview(
        [op_id], registered_builders={}, exemptions=frozenset({op_id})
    )
    assert missing == []


def test_a_non_credential_op_is_not_flagged() -> None:
    missing = _credential_writes_missing_preview(
        ["vault.auth.approle.write"], registered_builders={}, exemptions=frozenset()
    )
    assert missing == []
