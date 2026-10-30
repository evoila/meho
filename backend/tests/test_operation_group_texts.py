# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No hand-written group text claims one safety level for the whole group (#4120).

A group text (``when_to_use``) tells the model when to use a group. A small
model trusts it. Each operation has its own safety level, and
``search_operations`` shows it. So a text that says "Every op is dangerous"
or "All ops are safe" is wrong as soon as one operation is at another level,
and it can make a model skip the group or think a plain read needs approval.

Two checks:

* Every group text that MEHO's code writes at start-up, of every connector,
  has no "every op is", "every other op is" or "all ops are" claim.
* The ``vmware-rest`` group texts also have no bare "Read-only." sentence and
  no "Reads (safe):" or "Writes (dangerous):" label. These groups also hold
  the operations ingested from the vSphere API files, so such a claim cannot
  be true there. The texts name a safety level only next to a named
  operation. (Other connectors' groups that hold only hand-written reads may
  say "Read-only.", and that is true.)
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.vmware_rest.composites._register import _WHEN_TO_USE_BY_GROUP
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_OP = r"(?:op|operation)s?"
_LEVEL = r"(?:safe|caution|dangerous|destructive|approval)"

#: A claim about every operation of the group.
WHOLE_GROUP_CLAIM: Final = re.compile(
    rf"\bevery (?:other )?{_OP} (?:is|are)\b|\ball (?:other )?{_OP} (?:is|are)\b",
    re.IGNORECASE,
)

#: A bare "Read-only." sentence, or a safety label for all reads or all writes.
CLASS_CLAIM: Final = re.compile(
    rf"(?:^|[.!?]\s+)read[- ]only\s*[.!?]|\b(?:reads?|writes?)\s*\(\s*{_LEVEL}",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "text",
    [
        "Every other op is dangerous / approval-required.",
        "Every op is dangerous.",
        "All operations are safe.",
        "Use for events. Read-only. Pair with 'vm'.",
        "Read-only.",
        "Write (dangerous / approval-required): evacuate a host.",
        "Reads (safe): list guest processes.",
    ],
)
def test_the_checks_catch_a_whole_group_claim(text: str) -> None:
    assert WHOLE_GROUP_CLAIM.search(text) or CLASS_CLAIM.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Reads: list VMs (GET:/api/vcenter/vm).",
        "Changes: take a snapshot (vm.snapshot.create, caution).",
        "Update every host of a cluster in turn (cluster.patch, dangerous).",
        "Delete a read-only file (datastore.file.delete, destructive).",
    ],
)
def test_the_checks_let_a_claim_about_one_operation_pass(text: str) -> None:
    assert not WHOLE_GROUP_CLAIM.search(text)
    assert not CLASS_CLAIM.search(text)


@pytest.mark.parametrize("group_key", sorted(_WHEN_TO_USE_BY_GROUP))
def test_vmware_rest_group_text_names_a_level_only_next_to_an_operation(group_key: str) -> None:
    text = _WHEN_TO_USE_BY_GROUP[group_key]
    for check in (WHOLE_GROUP_CLAIM, CLASS_CLAIM):
        match = check.search(text)
        assert match is None, f"group {group_key!r} claims one level for many ops: {match!r}"


@pytest.fixture
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.mark.asyncio
@pytest.mark.usefixtures("_required_settings_env")
async def test_no_code_group_text_claims_one_level_for_every_op() -> None:
    await run_typed_op_registrars(embedding_service=_stub_embedding_service())
    async with get_sessionmaker()() as session:
        groups = (await session.execute(select(OperationGroup))).scalars().all()
    assert groups, "no group was registered"

    found = [
        f"{group.impl_id}-{group.version} {group.group_key}: {match.group(0)!r}"
        for group in groups
        if (match := WHOLE_GROUP_CLAIM.search(group.when_to_use)) is not None
    ]
    assert not found, "group texts claim one safety level for every op:\n" + "\n".join(found)
