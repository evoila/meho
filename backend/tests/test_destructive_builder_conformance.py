# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry-driven conformance sweep: every destructive op has a builder (#3312).

The runtime enforcement is fail-closed but runtime-only: the dispatcher's
``_destructive_binding_refusal`` (dispatcher.py) plus the mandatory
blast-radius gate (``blast_radius_missing_reason``, ``operations/_preview.py``)
refuse to *park* a ``safety_level="destructive"`` op unless its
``proposed_effect`` carries a well-formed blast-radius block — which requires a
registered preview builder. So a posture sweep that promotes an op to
``destructive`` **without** registering its builder (the #3247 / #3288 / #3305
promotions show these are recurring) ships an **un-parkable** operation: fail-
safe, but silently broken until first use, and green in CI.

This sweep closes that gap. It enumerates every registered ``destructive``
descriptor — across every connector, both source kinds (typed + composite;
harbor / bind9 precedent shows a bespoke builder is required regardless of
source kind) — from the real typed-op registrars, and asserts each resolves a
registered builder in :data:`_PREVIEW_BUILDERS`. A promotion without a builder
now fails **CI**, not first use.

Idiom: registry-driven (drives off the live registered descriptor set, so it
fails on an unacknowledged addition), mirroring the flight-recorder registry
sweep and ``test_mcp_surface_conformance``. The shared predicate
(:func:`_destructive_ops_missing_builder`) lets the negative guards prove the
sweep bites without a DB.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires (``run_typed_op_registrars``
    reads :func:`get_settings`)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Destructive op ids deliberately exempt from the builder requirement.
#:
#: **Empty by design.** The runtime gate is fail-closed, so a destructive op
#: with no builder is un-parkable; this sweep only promotes that from a
#: first-use runtime failure to a CI failure. Add an op here ONLY with a
#: comment proving its family genuinely cannot compute a blast radius (no
#: family does today — every destructive op ships a bespoke builder).
_BUILDER_EXEMPT_DESTRUCTIVE_OPS: frozenset[str] = frozenset()

#: Real destructive ops (one per non-vmware family) asserted to be swept: they
#: must be both registered as ``destructive`` and carry a builder. A guard
#: against the sweep silently seeing an empty destructive set (a broken seed
#: would make the main assertion vacuously pass).
_KNOWN_DESTRUCTIVE_OPS_WITH_BUILDERS: frozenset[str] = frozenset(
    {
        "bind9.record.delete",
        "bind9.record.remove",
        "harbor.robot.delete",
        "windns.record.remove",
    }
)


def _destructive_ops_missing_builder(
    op_ids: object,
    *,
    registered_builders: Mapping[str, Any],
    exemptions: frozenset[str],
) -> list[str]:
    """Return the destructive op ids resolving no proposed-effect builder.

    The registry-driven predicate the sweep and its negative guards share:
    a destructive op id that is neither in *registered_builders* nor exempt is
    un-parkable and flagged. Sorted for a deterministic assertion message.
    """
    return sorted(
        op_id
        for op_id in set(op_ids)  # type: ignore[arg-type]
        if op_id not in registered_builders and op_id not in exemptions
    )


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the registrar pass doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _seed_and_collect_destructive_op_ids(stub: AsyncMock) -> list[str]:
    """Run every typed-op registrar, return the registered destructive op ids."""
    await run_typed_op_registrars(embedding_service=stub)
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(EndpointDescriptor.safety_level == "destructive")
        )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_every_destructive_op_registers_a_blast_radius_builder(
    stub_embedding_service: AsyncMock,
) -> None:
    """Registry-driven sweep: no registered destructive op lacks a builder (#3312)."""
    op_ids = await _seed_and_collect_destructive_op_ids(stub_embedding_service)

    # Guard against a vacuous pass: the destructive set must be non-empty and
    # cover the known non-vmware families (else a broken seed hides gaps).
    assert op_ids, "no destructive descriptors registered — the sweep would pass vacuously"
    seeded = set(op_ids)
    assert seeded >= _KNOWN_DESTRUCTIVE_OPS_WITH_BUILDERS, (
        "expected the known destructive family ops to be registered; missing "
        f"{sorted(_KNOWN_DESTRUCTIVE_OPS_WITH_BUILDERS - seeded)}"
    )

    missing = _destructive_ops_missing_builder(
        op_ids,
        registered_builders=_PREVIEW_BUILDERS,
        exemptions=_BUILDER_EXEMPT_DESTRUCTIVE_OPS,
    )
    assert not missing, (
        "destructive ops with no registered proposed-effect / blast-radius builder "
        f"(un-parkable — the runtime park gate is fail-closed): {missing}. Register a "
        "builder via register_preview_builder(op_id, ...), or add a commented "
        "exemption to _BUILDER_EXEMPT_DESTRUCTIVE_OPS with a reason."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "victim",
    ["bind9.record.delete", "windns.record.remove", "harbor.robot.delete"],
)
async def test_sweep_flags_a_missing_destructive_builder(
    stub_embedding_service: AsyncMock,
    victim: str,
) -> None:
    """Removing any existing destructive builder makes the sweep flag it (#3312 AC).

    Exercised on a filtered copy of the builder registry so the process-wide
    :data:`_PREVIEW_BUILDERS` is never mutated.
    """
    op_ids = await _seed_and_collect_destructive_op_ids(stub_embedding_service)
    assert victim in set(op_ids), f"{victim} should be a registered destructive op"
    assert victim in _PREVIEW_BUILDERS, f"{victim} should have a builder to remove"

    builders_without_victim = {k: v for k, v in _PREVIEW_BUILDERS.items() if k != victim}
    missing = _destructive_ops_missing_builder(
        op_ids,
        registered_builders=builders_without_victim,
        exemptions=_BUILDER_EXEMPT_DESTRUCTIVE_OPS,
    )
    assert victim in missing


def test_sweep_flags_a_hypothetical_destructive_op_without_a_builder() -> None:
    """A promoted-but-builderless destructive descriptor fails the sweep (#3312 AC).

    The recurring hazard the sweep guards: a posture pass adds a ``destructive``
    op and forgets its builder. Pure — no DB — the predicate is the same one the
    live sweep uses.
    """
    missing = _destructive_ops_missing_builder(
        ["some.brand_new.destroy"],
        registered_builders=_PREVIEW_BUILDERS,
        exemptions=_BUILDER_EXEMPT_DESTRUCTIVE_OPS,
    )
    assert missing == ["some.brand_new.destroy"]


def test_an_exemption_suppresses_the_flag() -> None:
    """An explicit exemption removes an op from the flagged set (escape valve)."""
    op_id = "some.family.that_cannot_preview"
    missing = _destructive_ops_missing_builder(
        [op_id],
        registered_builders={},
        exemptions=frozenset({op_id}),
    )
    assert missing == []
