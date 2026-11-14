# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shipped "small team" policy file says what MEHO does without a file.

* The two copies of the file (``deploy/policy-profiles/`` and the one
  packaged in ``meho_backplane/policy/profiles/``) are the same, and the
  file passes the checks of ``meho policy validate``.
* Round trip: the export of a fresh install parses equal to the file, and
  the diff and the import of the file on a fresh install answer
  "No change.".
* No change over every registered operation: with the file stored for the
  whole install, every operation keeps its risk level, its wait and the
  answer (run, wait or deny) for each kind of caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PolicyRevision, TenantPolicy
from meho_backplane.operations.op_policy import OpKey, effective_policy_map
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.policy.classification import DEFAULT_TAXONOMY, taxonomy_from_documents
from meho_backplane.policy.document import document_of, parse_policy_file
from meho_backplane.policy.evaluate import Asker, Lookups, evaluate
from meho_backplane.policy.service import (
    NO_CHANGE,
    apply_policy,
    build_plan,
    builtin_install_document,
    export_policy,
)
from meho_backplane.policy.shadow import asked_of
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY_COPY = _REPO / "deploy" / "policy-profiles" / "small-team.yaml"
_PACKAGED_COPY = (
    _REPO / "backend" / "src" / "meho_backplane" / "policy" / "profiles" / "small-team.yaml"
)
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006040")

#: One caller of each kind, so a changed answer shows up for any of them.
_ASKERS: tuple[Asker, ...] = (
    Asker(kind="person", sub="p-web", tenant_id=_TENANT, roles=frozenset({"operator"}), via="web"),
    Asker(kind="person", sub="p-token", tenant_id=_TENANT, roles=frozenset({"operator"})),
    Asker(
        kind="person",
        sub="p-admin",
        tenant_id=_TENANT,
        roles=frozenset({"tenant_admin", "approver", "sa_operator"}),
    ),
    Asker(kind="agent", sub="agent", tenant_id=_TENANT, roles=frozenset({"operator"})),
    Asker(kind="service", sub="service", tenant_id=_TENANT, roles=frozenset({"operator"})),
    Asker(kind="runner", sub="runner", tenant_id=_TENANT, roles=frozenset({"operator"})),
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if on else "false")
    get_settings.cache_clear()


def _small_team() -> bytes:
    return _PACKAGED_COPY.read_bytes()


async def _register_all() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await run_typed_op_registrars(embedding_service=embedding)


async def _store_small_team() -> None:
    parsed = parse_policy_file(_small_team())
    assert parsed.model is not None, parsed.mistakes
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TenantPolicy(tenant_id=None, document=document_of(parsed.model), set_by_sub="test")
        )


async def _answers() -> dict[tuple[str, str], tuple[Any, ...]]:
    """Per operation: risk level, wait, "always wait", category, labels and each caller's answer."""
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        policies = await effective_policy_map(session, tenant_id=_TENANT, rows=rows)
        session.expunge_all()
    answers: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in rows:
        policy = policies[OpKey.of(row)]
        row.safety_level = policy.safety_level
        row.requires_approval = policy.requires_approval
        row.policy_always_wait = policy.always_wait
        connector_id = connector_id_of(row)
        asked = asked_of(row, connector_id=connector_id, target=None)
        verdicts = tuple(evaluate(asker, asked, Lookups()).verdict for asker in _ASKERS)
        answers[(connector_id, row.op_id)] = (
            policy.safety_level,
            policy.requires_approval,
            policy.always_wait,
            policy.category,
            dict(policy.labels),
            verdicts,
        )
    return answers


def _without_classification(answers: dict[Any, tuple[Any, ...]]) -> dict[Any, tuple[Any, ...]]:
    """Risk level, wait, "always wait" and the answers; no category or labels."""
    return {key: (*value[:3], value[5]) for key, value in answers.items()}


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_the_two_copies_are_the_same() -> None:
    assert _DEPLOY_COPY.read_bytes() == _PACKAGED_COPY.read_bytes()


def test_the_builtin_default_is_the_packaged_file() -> None:
    parsed = parse_policy_file(_small_team())
    assert parsed.model is not None, parsed.mistakes
    assert builtin_install_document() == document_of(parsed.model)


def test_the_file_has_the_builtin_taxonomy() -> None:
    assert taxonomy_from_documents(builtin_install_document(), None) == DEFAULT_TAXONOMY


@pytest.mark.asyncio
async def test_the_file_passes_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, True)
    await _register_all()
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=_small_team())
    assert plan.mistakes == []
    assert plan.warnings == []


# ---------------------------------------------------------------------------
# Round trip on a fresh install
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_of_a_fresh_install_is_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, True)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="install", tenant_id=None)
    exported = parse_policy_file(text.encode())
    expected = parse_policy_file(_small_team())
    assert exported.mistakes == []
    assert exported.model == expected.model


@pytest.mark.asyncio
async def test_diff_and_import_of_the_file_on_a_fresh_install_change_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    await _register_all()
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=_small_team())
    assert plan.no_change is True
    assert plan.diff is not None and plan.diff["operations_changed"] == 0

    async with get_sessionmaker()() as session, session.begin():
        result = await apply_policy(
            session,
            scope="install",
            tenant_id=None,
            content=_small_team(),
            expected_sha256=plan.file_sha256,
            based_on=plan.based_on,
            imported_by="admin-a",
            approved_by="admin-b",
        )
    assert result["applied"] is False and result["message"] == NO_CHANGE
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(TenantPolicy))).scalars().all() == []
        assert (await session.execute(select(PolicyRevision))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_fresh_tenant_still_exports_an_empty_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, True)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
    assert text == "policy: v1\nclassifications: []\n"


# ---------------------------------------------------------------------------
# No change over every registered operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_importing_the_file_changes_no_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    await _register_all()

    _switch(monkeypatch, False)
    today = await _answers()
    _switch(monkeypatch, True)
    without_file = await _answers()

    await _store_small_team()
    with_file = await _answers()

    assert len(with_file) > 100, "the registrars registered too few operations"
    assert with_file == without_file
    assert _without_classification(with_file) == _without_classification(today)
