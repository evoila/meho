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
  whole install, every operation keeps its risk level, its "always wait",
  who may approve it, and the answer of the real policy check
  (``policy_gate``: run, wait or refuse, and the reason) for seven kinds of
  caller, with grants and a run approval, also with ``AGENT_GRANT_REQUIRED=on``.
  A file with one more entry or routing line fails this check. A binding in the file fails the
  ``validate`` check: an install's file may not hold bindings.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    PermissionVerdict,
    PolicyRevision,
    RunApproval,
    ServicePrincipalGrant,
    TenantPolicy,
)
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.policy import service
from meho_backplane.policy.classification import DEFAULT_TAXONOMY, taxonomy_from_documents
from meho_backplane.policy.document import document_of, parse_policy_file
from meho_backplane.policy.evaluate import Asker, AskerKind, Lookups, Via, evaluate
from meho_backplane.policy.service import (
    NO_CHANGE,
    POLICY_CHANGED,
    PolicyApplyError,
    apply_policy,
    build_plan,
    builtin_install_document,
    export_policy,
)
from meho_backplane.policy.shadow import asked_of
from meho_backplane.policy.store import connector_id_of
from meho_backplane.policy.who_approves import approvers_for
from meho_backplane.settings import get_settings

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY_COPY = _REPO / "deploy" / "policy-profiles" / "small-team.yaml"
_PACKAGED_COPY = (
    _REPO / "backend" / "src" / "meho_backplane" / "policy" / "profiles" / "small-team.yaml"
)
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006040")
_WORK_REF = "run-small-team"


@dataclass(frozen=True)
class _Caller:
    """A caller of every operation, and what it holds."""

    name: str
    operator: Operator
    asker: Asker
    run_approval: bool = False


def _operator(sub: str, kind: PrincipalKind, role: TenantRole, **extra: Any) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        **extra,
    )


def _asker(kind: AskerKind, sub: str, *roles: str, via: Via = "token") -> Asker:
    return Asker(kind=kind, sub=sub, tenant_id=_TENANT, roles=frozenset(roles), via=via)


_USER, _AGENT, _SERVICE = PrincipalKind.USER, PrincipalKind.AGENT, PrincipalKind.SERVICE
_OPERATOR, _ADMIN = TenantRole.OPERATOR, TenantRole.TENANT_ADMIN

#: One caller of each kind, so a changed answer shows up for any of them.
#: ``svc-grant`` holds a standing grant for every operation, ``agent-grant``
#: an agent grant for every operation, and ``svc-run`` a run approval for
#: every operation (:func:`_seed_grants`).
_CALLERS: tuple[_Caller, ...] = (
    _Caller(
        "person in the console",
        _operator("p-web", _USER, _OPERATOR, web_session=True),
        _asker("person", "p-web", "operator", via="web"),
    ),
    _Caller(
        "person with a token",
        _operator("p-token", _USER, _OPERATOR),
        _asker("person", "p-token", "operator"),
    ),
    _Caller(
        "admin",
        _operator("p-admin", _USER, _ADMIN, approver=True),
        _asker("person", "p-admin", "tenant_admin", "approver", "sa_operator"),
    ),
    _Caller(
        "service with a standing grant",
        _operator("svc-grant", _SERVICE, _OPERATOR),
        _asker("service", "svc-grant", "operator"),
    ),
    _Caller(
        "agent with a grant",
        _operator("agent-grant", _AGENT, _OPERATOR),
        _asker("agent", "agent-grant", "operator"),
    ),
    _Caller(
        "agent without a grant",
        _operator("agent-none", _AGENT, _OPERATOR),
        _asker("agent", "agent-none", "operator"),
    ),
    _Caller(
        "service with a run approval",
        _operator("svc-run", _SERVICE, _OPERATOR),
        _asker("service", "svc-run", "operator"),
        run_approval=True,
    ),
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool, *, routing: bool | None = None) -> None:
    """The switches that make MEHO read the policy file: classification and approver routing."""
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if on else "false")
    monkeypatch.setenv(
        "POLICY_APPROVER_ROUTING", "true" if (on if routing is None else routing) else "false"
    )
    get_settings.cache_clear()


def _small_team() -> bytes:
    return _PACKAGED_COPY.read_bytes()


async def _register_all() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await run_typed_op_registrars(embedding_service=embedding)


async def _store_install_policy(content: bytes) -> None:
    """Store *content* as the install's policy, in place of the one stored before."""
    parsed = parse_policy_file(content)
    assert parsed.model is not None, parsed.mistakes
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        session.add(
            TenantPolicy(tenant_id=None, document=document_of(parsed.model), set_by_sub="test")
        )


async def _all_rows() -> list[EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        session.expunge_all()
    return rows


async def _seed_grants(rows: list[EndpointDescriptor]) -> None:
    """A standing grant, an agent grant and a run approval for every operation."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        for row in rows:
            session.add(
                ServicePrincipalGrant(
                    tenant_id=_TENANT,
                    principal_sub="svc-grant",
                    op_id=row.op_id,
                    connector_id=connector_id_of(row),
                    target_id=None,
                    reason="test: every operation",
                    created_by_sub="test-admin",
                )
            )
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub="agent-grant",
                op_pattern="*",
                verdict=PermissionVerdict.AUTO_EXECUTE,
                created_by_sub="test-admin",
            )
        )
        session.add(
            RunApproval(
                tenant_id=_TENANT,
                principal_sub="svc-run",
                work_ref=_WORK_REF,
                entries=[
                    {"connector_id": connector_id_of(row), "op_id": row.op_id} for row in rows
                ],
                reason="test: every operation",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="test-approver",
            )
        )


async def _gate(caller: _Caller, descriptor: EndpointDescriptor) -> tuple[str, str | None]:
    """The answer of the real policy check (run, wait or refuse) and its reason."""
    token = work_ref_var.set(_WORK_REF if caller.run_approval else None)
    try:
        verdict, reason = await policy_gate(
            operator=caller.operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return verdict.value, reason


async def _answers(rows: list[EndpointDescriptor]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per operation, everything the policy decides, as a real call loads it."""
    answers: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        descriptor = await lookup_descriptor(
            tenant_id=_TENANT,
            product=row.product,
            version=row.version,
            impl_id=row.impl_id,
            op_id=row.op_id,
        )
        assert descriptor is not None, row.op_id
        connector_id = connector_id_of(descriptor)
        approvers = await approvers_for(
            tenant_id=_TENANT, connector_id=connector_id, descriptor=descriptor
        )
        asked = asked_of(descriptor, connector_id=connector_id, target=None)
        answers[(connector_id, row.op_id)] = {
            "risk": descriptor.safety_level,
            "requires_approval": descriptor.requires_approval,
            "always_wait": descriptor.policy_always_wait,
            "category": descriptor.policy_category,
            "labels": dict(descriptor.policy_labels or {}),
            "approvers": approvers,
            "gate": {c.name: await _gate(c, descriptor) for c in _CALLERS},
            "evaluator": {c.name: evaluate(c.asker, asked, Lookups()).verdict for c in _CALLERS},
        }
    return answers


def _without_classification(answers: dict[Any, dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Everything but the category and the labels, which only exist with the switch on."""
    return {
        key: {k: v for k, v in value.items() if k not in ("category", "labels")}
        for key, value in answers.items()
    }


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


def test_the_file_has_no_classification_entries() -> None:
    assert builtin_install_document()["classifications"] == []


def test_a_packaged_file_that_is_not_valid_names_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "small-team.yaml").write_text("policy: v9\n")
    monkeypatch.setattr(service, "files", lambda _package: tmp_path)
    service._small_team_document.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=r"The shipped small-team\.yaml is not valid"):
            builtin_install_document()
    finally:
        service._small_team_document.cache_clear()


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
async def test_an_install_file_without_taxonomy_waits_and_keeps_the_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    await _register_all()
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="install", tenant_id=None, content=b"policy: v1\nclassifications: []\n"
        )
    assert plan.mistakes == []
    assert plan.no_change is False
    assert plan.diff is not None
    assert plan.diff["taxonomy"]["removed"] == list(DEFAULT_TAXONOMY.categories)
    assert plan.diff["operations_changed"] == 0
    assert taxonomy_from_documents(plan.document, None) == DEFAULT_TAXONOMY


@pytest.mark.asyncio
async def test_a_fresh_tenant_still_exports_an_empty_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant does not get the install's file (with routing on it gets the shipped bindings)."""
    _switch(monkeypatch, True, routing=False)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
    assert text == "policy: v1\nclassifications: []\n"


# ---------------------------------------------------------------------------
# No change over every registered operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_importing_the_file_changes_no_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real policy check gives every caller the same answer, with and without the file.

    The last part shows that the comparison sees a change: one routing line
    and one "always wait" entry added to the file change the answers.
    """
    await _register_all()
    rows = await _all_rows()
    assert len(rows) > 100, "the registrars registered too few operations"
    await _seed_grants(rows)

    _switch(monkeypatch, False)
    today = await _answers(rows)
    _switch(monkeypatch, True)
    without_file = await _answers(rows)

    await _store_install_policy(_small_team())
    with_file = await _answers(rows)

    assert with_file == without_file
    assert _without_classification(with_file) == _without_classification(today)

    gates = [answer["gate"] for answer in with_file.values()]
    assert any("standing grant" in (g["service with a standing grant"][1] or "") for g in gates)
    assert any("run approval" in (g["service with a run approval"][1] or "") for g in gates)
    assert any(g["agent with a grant"] != g["agent without a grant"] for g in gates)

    changed_file = _small_team().replace(
        b"classifications: []\n",
        b"classifications:\n"
        b"  - match: {op: '*.list'}\n"
        b"    always_wait: true\n"
        b"routing:\n"
        b"  - match: {category: other}\n"
        b"    approvers: [Admins]\n",
    )
    await _store_install_policy(changed_file)
    changed = await _answers(rows)
    assert any(changed[k]["approvers"] != with_file[k]["approvers"] for k in with_file)
    assert any(changed[k]["always_wait"] != with_file[k]["always_wait"] for k in with_file)
    assert any(changed[k]["gate"] != with_file[k]["gate"] for k in with_file)


@pytest.mark.asyncio
async def test_importing_the_file_changes_no_operation_when_agents_need_a_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same comparison with ``AGENT_GRANT_REQUIRED=on``: the file still changes no answer."""
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "on")
    await _register_all()
    rows = await _all_rows()
    assert len(rows) > 100, "the registrars registered too few operations"
    await _seed_grants(rows)

    _switch(monkeypatch, False)
    today = await _answers(rows)
    _switch(monkeypatch, True)
    without_file = await _answers(rows)

    await _store_install_policy(_small_team())
    with_file = await _answers(rows)

    assert with_file == without_file
    assert _without_classification(with_file) == _without_classification(today)
    gates = [answer["gate"] for answer in with_file.values()]
    assert any(g["agent with a grant"] != g["agent without a grant"] for g in gates)


@pytest.mark.asyncio
async def test_an_import_planned_before_the_policy_changed_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan is made, then the stored policy moves: the approved import writes nothing."""
    _switch(monkeypatch, True)
    await _register_all()
    content = _small_team().replace(
        b"classifications: []\n",
        b"classifications:\n  - match: {op: '*.list'}\n    always_wait: true\n",
    )
    other = _small_team().replace(
        b"classifications: []\n",
        b"classifications:\n  - match: {op: '*.delete'}\n    always_wait: true\n",
    )
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=content)
    assert plan.mistakes == []
    assert plan.no_change is False

    await _store_install_policy(other)

    with pytest.raises(PolicyApplyError) as stopped:
        async with get_sessionmaker()() as session, session.begin():
            await apply_policy(
                session,
                scope="install",
                tenant_id=None,
                content=content,
                expected_sha256=plan.file_sha256,
                based_on=plan.based_on,
                imported_by="admin-a",
                approved_by="admin-b",
            )
    assert str(stopped.value) == POLICY_CHANGED
    assert "The policy changed after this import was planned." in POLICY_CHANGED
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(PolicyRevision))).scalars().all() == []
