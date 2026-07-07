# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.run_service` (G12.3-T3 / #1308).

Coverage matrix tracks the 27-test acceptance breakdown from the issue body:

* ``start_run`` (5 tests) -- resolves latest published, refuses
  deprecated-only / draft-only, creates step states with first
  ``in_progress`` + rest ``pending``, refuses missing params.
* ``next_step`` happy paths (4 tests) -- confirm yes advances,
  operation_call match advances (with run/step audit correlation),
  completes-at-last produces :class:`RunCompletedResponse`, response
  carries exactly one step body.
* ``next_step`` refusals (5 tests) -- non-assignee 403,
  TENANT_ADMIN-non-assignee 403 (same reason -- single-assignee
  discipline), confirm-no transitions to failed (subsequent next_step
  refuses), terminal run refuses, unverified previous step refuses.
* Audit correlation (2 tests) -- ``next_step`` operation_call writes
  audit row with run_id + step_id populated; ``abort_run`` writes its
  own audit row with ``runbook.abort`` path + reason + run/step
  correlation.
* ``abort_run`` (3 tests) -- assignee can abort, admin can abort
  someone else's run, third-party operator can't.
* ``reassign_run`` (2 tests) -- updates assigned_to + returns response,
  service is role-agnostic (the TENANT_ADMIN gate lives at the route).
* ``list_runs`` (3 tests) -- operator pinned to own runs, admin sees
  all tenant runs, tenant isolation.
* ``can_show_template_post_completion`` (3 tests) -- completed run
  unlocks, abandoned run unlocks, in-progress run does not.

The conftest autouse fixture migrates a fresh SQLite DB per test (the
template service tests' load-bearing infrastructure) so the service's
``get_sessionmaker()`` binds to the same per-test schema these tests
seed rows into directly.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, RunbookRun, RunbookRunStepState, Target
from meho_backplane.operations import (
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks.run_service import (
    DeprecatedTemplateError,
    MissingParamsError,
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunAlreadyTerminalError,
    RunbookRunService,
    _build_operator_for_dispatch,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    ConfirmVerifyResponse,
    CurrentStepResponse,
    ListRunsFilter,
    NextStepRequest,
    ReassignRunRequest,
    RunCompletedResponse,
    StartRunRequest,
)
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DeprecateTemplateRequest,
    DraftTemplateRequest,
    ManualStep,
    OperationCallStep,
    OperationCallVerify,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import (
    RunbookTemplateService,
    TemplateNotFoundError,
)
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
OPERATOR = "operator-alpha"
OPERATOR_BETA = "operator-beta"
ADMIN = "tenant-admin-1"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars the :class:`Settings` model requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_dispatcher_state() -> Iterator[None]:
    """Clear connector + dispatcher caches around each test.

    The audit-correlation tests register a stub connector so the verify
    op_id resolves; the reset keeps that state from leaking into other
    tests in the module.
    """
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test for direct row inspection."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Embedding stub for typed-op descriptor's embedding column."""
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _manual_step(
    step_id: str,
    *,
    body: str = "do the thing",
    prompt: str = "done?",
) -> ManualStep:
    """Build a ``manual`` step gated by a ``confirm`` verify."""
    return ManualStep(
        id=step_id,
        title=f"Step {step_id}",
        body=body,
        type="manual",
        verify=ConfirmVerify(type="confirm", prompt=prompt),
    )


def _op_call_step(
    step_id: str,
    *,
    op_id: str = "stub.op_call",
    expect: dict[str, Any] | None = None,
) -> OperationCallStep:
    """Build an ``operation_call`` step gated by an ``operation_call`` verify."""
    return OperationCallStep(
        id=step_id,
        title=f"Step {step_id}",
        body=f"Body for {step_id}",
        type="operation_call",
        op_id=op_id,
        params={},
        verify=OperationCallVerify(
            type="operation_call",
            op_id=op_id,
            params={},
            expect=expect if expect is not None else {"ok": True},
        ),
    )


def _two_step_template(*, body: str = "drain") -> RunbookTemplateBody:
    return RunbookTemplateBody(
        title="Two-step procedure",
        description="for service tests",
        target_kind="k8s-node",
        steps=[_manual_step("step-1", body=body), _manual_step("step-2")],
    )


def _params_template() -> RunbookTemplateBody:
    """Template that references ``${run.params.threshold}`` in step body."""
    return RunbookTemplateBody(
        title="Params template",
        description="exercises start_run param validation",
        target_kind="k8s-node",
        steps=[
            _manual_step("only-step", body="Drain at ${run.params.threshold}"),
        ],
    )


def _op_call_template(op_id: str = "stub.op_call") -> RunbookTemplateBody:
    """Template with an operation_call verify to exercise audit correlation."""
    return RunbookTemplateBody(
        title="Op-call procedure",
        description="exercises operation_call verify dispatch",
        target_kind="k8s-node",
        steps=[_op_call_step("call-it", op_id=op_id)],
    )


async def _seed_published_template(
    tenant_id: uuid.UUID,
    slug: str,
    *,
    body: RunbookTemplateBody,
    version: int = 1,
) -> None:
    """Helper: create+publish a template via the template service.

    Going through the service keeps the test's seed shape identical to
    what production code writes; later versions can use the same path
    (and the service handles the in-place-edit vs fork branching).
    """
    template_service = RunbookTemplateService()
    if version == 1:
        await template_service.create_draft(
            tenant_id, OPERATOR, DraftTemplateRequest(slug=slug, body=body)
        )
        await template_service.publish(tenant_id, PublishTemplateRequest(slug=slug, version=1))
    else:  # pragma: no cover - not exercised in current tests
        raise NotImplementedError("seeding non-v1 versions requires fork path")


# Stub connector + typed handler used by operation_call verify dispatch tests.


class _StubConnector(Connector):
    """Minimal connector so dispatcher resolver finds a class for the triple."""

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _ok_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: return ``{"ok": True}`` -- matches the test's ``expect``."""
    return {"ok": True}


async def _vault_backed_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler standing in for a Vault-backed connector's credential read.

    Module-level (the typed-op registry rejects closures). Performs the
    operator-context Vault read every Vault-backed connector loader
    performs; under the synthetic runbook-dispatch operator the read
    must refuse locally before Vault is contacted.
    """
    await load_basic_credentials(target, operator)
    return {"ok": True}  # pragma: no cover - the read above must refuse


async def _seed_stub_op(
    stub_embedding_service: AsyncMock,
    *,
    op_id: str = "stub.op_call",
) -> None:
    """Register the stub connector + typed op the dispatcher can find."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=op_id,
        handler=_ok_handler,
        summary="Stub op for runbook run-service tests.",
        description="Echo OK; used for operation_call verify dispatch.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def _seed_target(tenant_id: uuid.UUID, name: str, *, secret_ref: str | None = None) -> None:
    """Seed a minimal Target row so the resolver in ``call_operation()`` succeeds.

    The dispatcher's target resolver (``targets.resolver.resolve_target``)
    looks up tenant-scoped targets by name; the runbook service passes the
    run's ``target`` string through to ``call_operation()`` which routes
    it via the resolver. Without a seed row, the call returns a 404 and
    the verify dispatch can't validate. Real production runbooks pin to
    targets the operator has provisioned out of band. ``secret_ref``
    configures the target as Vault-backed for the fail-closed dispatch
    tests (a populated ``secret_ref`` makes the empty-``raw_jwt`` guard
    the *only* precondition that can refuse the credential read).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Target(
                tenant_id=tenant_id,
                name=name,
                product="stub",
                host="stub-host.example",
                auth_model="shared_service_account",
                secret_ref=secret_ref,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_resolves_latest_published() -> None:
    """A slug with v1 (deprecated) + v2 (published) pins v2.

    Walks the template service: publish v1, fork-edit to draft v2,
    publish v2, deprecate v1 (so v1 is non-startable). ``start_run``
    must return ``template_version=2``.
    """
    tenant_id = uuid.uuid4()
    template_service = RunbookTemplateService()
    await template_service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain", body=_two_step_template())
    )
    await template_service.publish(tenant_id, PublishTemplateRequest(slug="drain", version=1))
    # Edit forks v2 from published v1.
    from meho_backplane.runbooks.schemas import EditTemplateRequest

    await template_service.update_or_fork(
        tenant_id,
        OPERATOR,
        EditTemplateRequest(slug="drain", body=_two_step_template(body="drain-v2")),
    )
    await template_service.publish(tenant_id, PublishTemplateRequest(slug="drain", version=2))
    await template_service.deprecate(tenant_id, DeprecateTemplateRequest(slug="drain", version=1))

    run_service = RunbookRunService()
    resp = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="drain", target="node-1", params={})
    )
    assert isinstance(resp, CurrentStepResponse)
    assert resp.template_version == 2


@pytest.mark.asyncio
async def test_start_run_refuses_when_only_deprecated() -> None:
    """Every version is deprecated -> :class:`DeprecatedTemplateError`."""
    tenant_id = uuid.uuid4()
    template_service = RunbookTemplateService()
    await template_service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="d", body=_two_step_template())
    )
    await template_service.publish(tenant_id, PublishTemplateRequest(slug="d", version=1))
    await template_service.deprecate(tenant_id, DeprecateTemplateRequest(slug="d", version=1))

    run_service = RunbookRunService()
    with pytest.raises(DeprecatedTemplateError):
        await run_service.start_run(
            tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
        )


@pytest.mark.asyncio
async def test_start_run_refuses_when_no_published() -> None:
    """Only-drafts -> :class:`TemplateNotFoundError`."""
    tenant_id = uuid.uuid4()
    template_service = RunbookTemplateService()
    await template_service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="d", body=_two_step_template())
    )

    run_service = RunbookRunService()
    with pytest.raises(TemplateNotFoundError):
        await run_service.start_run(
            tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
        )


@pytest.mark.asyncio
async def test_start_run_creates_step_states(db_session: AsyncSession) -> None:
    """N steps -> N step-state rows; first ``in_progress``, rest ``pending``."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())

    run_service = RunbookRunService()
    resp = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(resp, CurrentStepResponse)

    rows = (
        await db_session.scalars(
            select(RunbookRunStepState).where(RunbookRunStepState.run_id == resp.run_id)
        )
    ).all()
    assert len(rows) == 2
    by_id = {row.step_id: row for row in rows}
    assert by_id["step-1"].state == "in_progress"
    assert by_id["step-1"].started_at is not None
    assert by_id["step-2"].state == "pending"
    assert by_id["step-2"].started_at is None


@pytest.mark.asyncio
async def test_start_run_missing_params_raises() -> None:
    """Template references ``${run.params.threshold}`` but request omits it -> raises."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "p", body=_params_template())

    run_service = RunbookRunService()
    with pytest.raises(MissingParamsError) as ex:
        await run_service.start_run(
            tenant_id, OPERATOR, StartRunRequest(template_slug="p", target="n", params={})
        )
    assert "threshold" in str(ex.value)


# ---------------------------------------------------------------------------
# next_step happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_step_confirm_yes_advances(db_session: AsyncSession) -> None:
    """Confirm with answer=yes advances; previous step verified; next step in_progress."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    resp = await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )
    assert isinstance(resp, CurrentStepResponse)
    assert resp.current_step.id == "step-2"

    rows = (
        await db_session.scalars(
            select(RunbookRunStepState).where(RunbookRunStepState.run_id == start.run_id)
        )
    ).all()
    by_id = {row.step_id: row for row in rows}
    assert by_id["step-1"].state == "verified"
    assert by_id["step-1"].verified_at is not None
    assert by_id["step-2"].state == "in_progress"


@pytest.mark.asyncio
async def test_next_step_operation_call_match_advances(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """operation_call verify dispatches the call + matches expect + advances.

    Crucially: the dispatched call's audit row carries ``run_id`` and
    ``step_id`` populated -- that's the run/step correlation contract
    G12.1-T2 wired and this Initiative depends on.
    """
    await _seed_stub_op(stub_embedding_service)
    tenant_id = _TENANT_A
    await _seed_target(tenant_id, "n")
    await _seed_published_template(tenant_id, "d", body=_op_call_template())

    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    resp = await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=None,  # engine populates it from the dispatched call
        ),
    )
    assert isinstance(resp, RunCompletedResponse)


@pytest.mark.asyncio
async def test_next_step_completes_at_last_step(
    db_session: AsyncSession,
) -> None:
    """Verify on the last step -> ``RunCompletedResponse`` + ``state='completed'``."""
    tenant_id = uuid.uuid4()
    # One-step template -- the only step is the last.
    single_step = RunbookTemplateBody(
        title="one step",
        description="for completion",
        steps=[_manual_step("only")],
    )
    await _seed_published_template(tenant_id, "d", body=single_step)
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    resp = await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )
    assert isinstance(resp, RunCompletedResponse)

    run = (
        await db_session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
    ).one()
    assert run.state == "completed"
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_next_step_response_carries_exactly_one_step_body() -> None:
    """Property test (LOAD-BEARING): ``next_step`` response has one step body.

    Serialise the response, walk its JSON, assert no other step ids
    leak into the payload. Opacity is what makes Initiative #1198's
    adherence floor real -- the structural test guards refactors.
    """
    tenant_id = uuid.uuid4()
    # 5-step template -- many candidates for leakage.
    five = RunbookTemplateBody(
        title="five",
        description="opacity",
        steps=[_manual_step(f"step-{i}") for i in range(1, 6)],
    )
    await _seed_published_template(tenant_id, "d", body=five)
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    resp = await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )
    assert isinstance(resp, CurrentStepResponse)
    serialised = resp.model_dump_json()
    # The response is on step 2 now -- step 1's id (the just-verified)
    # and steps 3 / 4 / 5 (future) must not appear in the wire shape.
    assert '"id":"step-2"' in serialised
    for leak in ("step-1", "step-3", "step-4", "step-5"):
        assert leak not in serialised, f"step id leaked into response: {leak}"


# ---------------------------------------------------------------------------
# next_step refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_step_non_assignee_403() -> None:
    """A different operator calling ``next_step`` -> :class:`NotRunAssigneeError`."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    with pytest.raises(NotRunAssigneeError):
        await run_service.next_step(
            tenant_id,
            OPERATOR_BETA,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )


@pytest.mark.asyncio
async def test_next_step_admin_non_assignee_still_403() -> None:
    """TENANT_ADMIN who isn't the assignee -> still ``NotRunAssigneeError``.

    The right way for a senior to take over is :meth:`reassign_run`; the
    service refuses role-based bypasses on the advance path.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    # next_step has no caller_is_admin knob -- single-assignee discipline
    # is unconditional. Same call, different caller_sub, still raises.
    with pytest.raises(NotRunAssigneeError):
        await run_service.next_step(
            tenant_id,
            ADMIN,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )


@pytest.mark.asyncio
async def test_next_step_confirm_no_transitions_to_failed(
    db_session: AsyncSession,
) -> None:
    """answer=no -> step ``failed``; subsequent next_step raises ``PreviousStepFailedError``."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    # First call: answer=no -> service raises PreviousStepFailedError
    # AND commits step state to failed.
    with pytest.raises(PreviousStepFailedError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=False,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="no"),
            ),
        )

    # Confirm the step row is failed.
    rows = (
        await db_session.scalars(
            select(RunbookRunStepState).where(
                RunbookRunStepState.run_id == start.run_id,
                RunbookRunStepState.step_id == "step-1",
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].state == "failed"

    # Second call should still refuse because the step is failed.
    with pytest.raises(PreviousStepFailedError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )


@pytest.mark.asyncio
async def test_next_step_run_already_completed_refused() -> None:
    """Calling next_step on a run already in terminal state -> ``RunAlreadyTerminalError``."""
    tenant_id = uuid.uuid4()
    single_step = RunbookTemplateBody(
        title="one",
        description="terminal",
        steps=[_manual_step("only")],
    )
    await _seed_published_template(tenant_id, "d", body=single_step)
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    # Complete the run.
    await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )

    # Second call must refuse.
    with pytest.raises(RunAlreadyTerminalError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )


@pytest.mark.asyncio
async def test_next_step_unverified_previous_step_refuses(
    db_session: AsyncSession,
) -> None:
    """The substrate is the verify oracle: ``last_verified=False`` does *not* skip the gate.

    The caller's claim is informational; the service reads the persisted
    step state and refuses to advance when the current step isn't
    ``in_progress``. Forcing the step to ``pending`` (which the engine
    refuses to advance from) makes the substrate's authority explicit.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    # Force step-1 back to 'pending' to simulate a substrate state the
    # caller's ``last_verified=True`` cannot bypass.
    async with get_sessionmaker()() as session:
        row = (
            await session.scalars(
                select(RunbookRunStepState).where(
                    RunbookRunStepState.run_id == start.run_id,
                    RunbookRunStepState.step_id == "step-1",
                )
            )
        ).one()
        row.state = "pending"
        row.started_at = None
        await session.commit()

    # last_verified=True is the caller's optimistic claim; the substrate
    # refuses regardless because the step is 'pending'. The engine's
    # PreviousStepNotVerifiedError propagates through the service.
    from meho_backplane.runbooks.engine import PreviousStepNotVerifiedError

    with pytest.raises((PreviousStepNotVerifiedError, ValueError)):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )


# ---------------------------------------------------------------------------
# Audit correlation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_step_operation_call_audit_row_has_run_and_step_id(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The dispatched verify call's audit row carries run_id + step_id.

    The contextvar binding is the load-bearing piece: G12.3 binds
    ``run_id_var`` + ``step_id_var`` around ``call_operation()``; the
    dispatcher's audit writer reads them into the dedicated columns
    (or, on schemas predating migration 0034, into the JSON payload).
    Either column or payload landing the values is acceptable evidence;
    we check both shapes.
    """
    await _seed_stub_op(stub_embedding_service)
    tenant_id = _TENANT_A
    await _seed_target(tenant_id, "n")
    await _seed_published_template(tenant_id, "d", body=_op_call_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(last_verified=True, verify_response=None),
    )

    rows = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH")
            .where(AuditLog.path == "stub.op_call")
        )
    ).all()
    assert len(rows) == 1
    row = rows[0]
    # Either the dedicated column carries the value (post-0034) or the
    # payload mirror does; one of them must.
    column_run_id = getattr(row, "run_id", None)
    column_step_id = getattr(row, "step_id", None)
    payload = row.payload
    assert isinstance(payload, dict)
    assert (column_run_id == start.run_id) or (payload.get("run_id") == str(start.run_id))
    assert (column_step_id == "call-it") or (payload.get("step_id") == "call-it")


@pytest.mark.asyncio
async def test_abort_run_writes_dispatch_audit_row(
    db_session: AsyncSession,
) -> None:
    """``abort_run`` writes an audit row directly: path='runbook.abort', reason in payload."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    await run_service.abort_run(
        tenant_id,
        OPERATOR,
        start.run_id,
        AbortRunRequest(reason="customer cancelled"),
    )

    rows = (
        await db_session.scalars(select(AuditLog).where(AuditLog.path == "runbook.abort"))
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "DISPATCH"
    assert row.status_code == 200
    assert row.operator_sub == OPERATOR
    payload = row.payload
    assert isinstance(payload, dict)
    assert payload["reason"] == "customer cancelled"
    # Run/step correlation: either dedicated column or payload mirror.
    column_run_id = getattr(row, "run_id", None)
    column_step_id = getattr(row, "step_id", None)
    assert (column_run_id == start.run_id) or (payload.get("run_id") == str(start.run_id))
    assert (column_step_id == "step-1") or (payload.get("step_id") == "step-1")


# ---------------------------------------------------------------------------
# Synthetic dispatch operator fails closed on Vault-backed reads
# ---------------------------------------------------------------------------


def test_build_operator_for_dispatch_carries_empty_raw_jwt() -> None:
    """The synthetic runbook-dispatch operator honours the fail-closed shape.

    ``raw_jwt`` must be the empty string (the synthetic-operator
    convention every Vault-touching layer short-circuits on) — never a
    non-empty placeholder that sails past ``_resolve_secret_ref``'s
    empty-``raw_jwt`` guard and reaches Vault's JWT/OIDC login. The
    audit identity (``sub`` / ``tenant_id``) is preserved unchanged.
    """
    operator = _build_operator_for_dispatch(sub=OPERATOR, tenant_id=_TENANT_A)

    assert operator.raw_jwt == ""
    assert operator.sub == OPERATOR
    assert operator.name == OPERATOR
    assert operator.tenant_id == _TENANT_A


@pytest.mark.asyncio
async def test_dispatch_operator_vault_read_refused_before_vault_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault-backed credential read under the synthetic operator fails closed locally.

    ``load_basic_credentials`` must raise ``VaultCredentialsReadError``
    from the ``_resolve_secret_ref`` precondition guard (no
    authenticated operator) **before** any Vault contact: the login
    entry point (``vault_client_for_operator``) is never invoked. The
    target carries a populated ``secret_ref`` so the empty-``raw_jwt``
    guard is the only precondition that can refuse the read.
    """
    vault_login = MagicMock(name="vault_client_for_operator")
    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.vault_client_for_operator",
        vault_login,
    )

    class _VaultBackedTarget:
        name = "n"
        host = "stub-host.example"
        secret_ref = "targets/n"

    operator = _build_operator_for_dispatch(sub=OPERATOR, tenant_id=_TENANT_A)

    with pytest.raises(VaultCredentialsReadError, match="authenticated operator"):
        await load_basic_credentials(_VaultBackedTarget(), operator)

    vault_login.assert_not_called()


@pytest.mark.asyncio
async def test_next_step_vault_backed_verify_fails_closed_without_vault_contact(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verify op that needs a Vault credential read refuses with a failed step.

    End-to-end through ``next_step``: the verify ``op_id`` resolves to a
    handler that performs the operator-context Vault read
    (``load_basic_credentials``). Under the synthetic runbook-dispatch
    operator the read must fail closed at the local precondition guard —
    the dispatch surfaces a structured refusal (step ``failed``,
    subsequent calls raise :class:`PreviousStepFailedError`) and the
    Vault login entry points are **never** invoked. Complements
    :func:`test_next_step_operation_call_match_advances`, which proves
    the non-Vault typed verify path dispatches unchanged with the same
    synthetic operator.
    """
    vault_login = MagicMock(name="vault_client_for_operator")
    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.vault_client_for_operator",
        vault_login,
    )
    jwt_login = MagicMock(name="_to_thread_jwt_login")
    monkeypatch.setattr("meho_backplane.auth.vault._to_thread_jwt_login", jwt_login)

    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id="stub.vault_backed",
        handler=_vault_backed_handler,
        summary="Vault-backed stub op for fail-closed dispatch tests.",
        description="Performs an operator-context Vault credential read.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    tenant_id = _TENANT_A
    await _seed_target(tenant_id, "n", secret_ref="targets/n")
    await _seed_published_template(
        tenant_id, "d", body=_op_call_template(op_id="stub.vault_backed")
    )

    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    with pytest.raises(PreviousStepFailedError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(last_verified=True, verify_response=None),
        )

    vault_login.assert_not_called()
    jwt_login.assert_not_called()

    rows = (
        await db_session.scalars(
            select(RunbookRunStepState).where(RunbookRunStepState.run_id == start.run_id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].state == "failed"


# ---------------------------------------------------------------------------
# work_ref correlation (I3-T1 #1661)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_persists_work_ref_and_step_audit_inherits(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """start_run with work_ref pins it on the run row; each operation_call
    step's audit row inherits the same work_ref.

    This is the load-bearing acceptance criterion: the engine binds the
    run's ``work_ref`` onto ``work_ref_var`` around each step's dispatch,
    so the dispatcher's audit writer stamps it onto the step's
    ``audit_log.work_ref`` (or, on schemas predating migration 0039, the
    column simply does not exist -- guarded by ``hasattr``).
    """
    await _seed_stub_op(stub_embedding_service)
    tenant_id = _TENANT_A
    await _seed_target(tenant_id, "n")
    await _seed_published_template(tenant_id, "d", body=_op_call_template())
    work_ref = "gh:evoila/meho#9"

    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="d", target="n", params={}, work_ref=work_ref),
    )
    assert isinstance(start, CurrentStepResponse)

    # The run row carries the work_ref.
    run_row = (
        await db_session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
    ).one()
    assert run_row.work_ref == work_ref

    # Advancing dispatches the operation_call verify; its audit row
    # inherits the run's work_ref.
    await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(last_verified=True, verify_response=None),
    )
    rows = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH")
            .where(AuditLog.path == "stub.op_call")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].work_ref == work_ref


@pytest.mark.asyncio
async def test_start_run_without_work_ref_leaves_audit_null(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A run started without a work_ref leaves the step audit row's work_ref NULL."""
    await _seed_stub_op(stub_embedding_service)
    tenant_id = _TENANT_A
    await _seed_target(tenant_id, "n")
    await _seed_published_template(tenant_id, "d", body=_op_call_template())

    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    run_row = (
        await db_session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
    ).one()
    assert run_row.work_ref is None

    await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(last_verified=True, verify_response=None),
    )
    rows = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH")
            .where(AuditLog.path == "stub.op_call")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].work_ref is None


@pytest.mark.asyncio
async def test_abort_run_audit_row_inherits_work_ref(db_session: AsyncSession) -> None:
    """The direct abort audit row carries the run's work_ref."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    work_ref = "gh:evoila/meho#9"
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="d", target="n", params={}, work_ref=work_ref),
    )
    assert isinstance(start, CurrentStepResponse)

    await run_service.abort_run(
        tenant_id,
        OPERATOR,
        start.run_id,
        AbortRunRequest(reason="customer cancelled"),
    )

    rows = (
        await db_session.scalars(select(AuditLog).where(AuditLog.path == "runbook.abort"))
    ).all()
    assert len(rows) == 1
    assert rows[0].work_ref == work_ref


@pytest.mark.asyncio
async def test_list_runs_filters_by_work_ref() -> None:
    """list_runs with a work_ref filter returns only runs under that ticket;
    RunSummary surfaces the work_ref."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    matching = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="d", target="n", params={}, work_ref="gh:evoila/meho#9"),
    )
    assert isinstance(matching, CurrentStepResponse)
    other = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="d", target="n", params={}, work_ref="gh:evoila/meho#10"),
    )
    assert isinstance(other, CurrentStepResponse)
    await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )

    rows = await run_service.list_runs(
        tenant_id,
        OPERATOR,
        caller_is_admin=False,
        filter_=ListRunsFilter(work_ref="gh:evoila/meho#9"),
    )
    assert len(rows) == 1
    assert rows[0].run_id == matching.run_id
    assert rows[0].work_ref == "gh:evoila/meho#9"


# ---------------------------------------------------------------------------
# abort_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_run_by_assignee(db_session: AsyncSession) -> None:
    """Assignee can abort their own run."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    resp = await run_service.abort_run(
        tenant_id, OPERATOR, start.run_id, AbortRunRequest(reason="stop")
    )
    assert resp.state == "abandoned"
    assert resp.abandoned_at is not None

    run = (
        await db_session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
    ).one()
    assert run.state == "abandoned"


@pytest.mark.asyncio
async def test_abort_run_by_admin() -> None:
    """TENANT_ADMIN can abort someone else's run when caller_is_admin=True."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    resp = await run_service.abort_run(
        tenant_id,
        ADMIN,
        start.run_id,
        AbortRunRequest(reason="taking over"),
        caller_is_admin=True,
    )
    assert resp.state == "abandoned"


@pytest.mark.asyncio
async def test_abort_run_by_other_operator_refused() -> None:
    """Operator who isn't the assignee or admin -> ``NotRunAssigneeError``."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    with pytest.raises(NotRunAssigneeError):
        await run_service.abort_run(
            tenant_id,
            OPERATOR_BETA,
            start.run_id,
            AbortRunRequest(reason="nope"),
            caller_is_admin=False,
        )


# ---------------------------------------------------------------------------
# reassign_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_run_changes_assigned_to(db_session: AsyncSession) -> None:
    """Service updates assigned_to; returns response carrying the new owner."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    resp = await run_service.reassign_run(
        tenant_id,
        ADMIN,
        start.run_id,
        ReassignRunRequest(new_assignee=OPERATOR_BETA),
    )
    assert resp.assigned_to == OPERATOR_BETA

    run = (
        await db_session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
    ).one()
    assert run.assigned_to == OPERATOR_BETA


@pytest.mark.asyncio
async def test_reassign_run_does_not_check_caller_role() -> None:
    """Service is role-agnostic on reassign (the TENANT_ADMIN gate is the route's job).

    Reassigning from a non-admin caller still flips ``assigned_to`` --
    the service does not look up roles; the route layer T5 / MCP layer
    T6 are where the role gate fires.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    # Reassigner role doesn't matter -- the service just writes the column.
    resp = await run_service.reassign_run(
        tenant_id,
        OPERATOR_BETA,
        start.run_id,
        ReassignRunRequest(new_assignee="operator-gamma"),
    )
    assert resp.assigned_to == "operator-gamma"


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_operator_sees_only_own() -> None:
    """caller_is_admin=False forces assignee=caller_sub regardless of filter."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start_a = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start_a, CurrentStepResponse)
    start_b = await run_service.start_run(
        tenant_id, OPERATOR_BETA, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start_b, CurrentStepResponse)

    # OPERATOR alpha asks for OPERATOR_BETA's runs explicitly via filter;
    # the service overrides to alpha's own.
    rows = await run_service.list_runs(
        tenant_id,
        OPERATOR,
        caller_is_admin=False,
        filter_=ListRunsFilter(assignee=OPERATOR_BETA),
    )
    assert len(rows) == 1
    assert rows[0].assigned_to == OPERATOR
    assert rows[0].run_id == start_a.run_id


@pytest.mark.asyncio
async def test_list_runs_admin_sees_all_tenant() -> None:
    """caller_is_admin=True honours the filter; admin can see other operators' runs."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    start_b = await run_service.start_run(
        tenant_id, OPERATOR_BETA, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start_b, CurrentStepResponse)

    # Admin filters explicitly for beta.
    rows = await run_service.list_runs(
        tenant_id,
        ADMIN,
        caller_is_admin=True,
        filter_=ListRunsFilter(assignee=OPERATOR_BETA),
    )
    assert len(rows) == 1
    assert rows[0].assigned_to == OPERATOR_BETA

    # Admin with no filter sees both.
    all_rows = await run_service.list_runs(
        tenant_id, ADMIN, caller_is_admin=True, filter_=ListRunsFilter()
    )
    assert len(all_rows) == 2


@pytest.mark.asyncio
async def test_list_runs_tenant_isolation() -> None:
    """A run in tenant B is not visible to a tenant A admin querying tenant A."""
    await _seed_published_template(_TENANT_A, "d", body=_two_step_template())
    await _seed_published_template(_TENANT_B, "d", body=_two_step_template())
    run_service = RunbookRunService()
    await run_service.start_run(
        _TENANT_A, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    await run_service.start_run(
        _TENANT_B, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )

    rows_a = await run_service.list_runs(
        _TENANT_A, ADMIN, caller_is_admin=True, filter_=ListRunsFilter()
    )
    assert len(rows_a) == 1
    rows_b = await run_service.list_runs(
        _TENANT_B, ADMIN, caller_is_admin=True, filter_=ListRunsFilter()
    )
    assert len(rows_b) == 1
    assert rows_a[0].run_id != rows_b[0].run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["no", "escalate"])
async def test_list_runs_surfaces_failed_step_state_without_mutation(answer: str) -> None:
    """#2119 AC: a ``no`` / ``escalate`` verify answer is readable via ``list_runs``.

    Start a run, answer the first step's confirm verify with *answer*
    (``no`` and the escalated-to-``failed`` ``escalate`` path both
    transition the step to ``failed``), then read the run through the
    list projection with **no mutation in between**. The failed step
    state must be visible — previously the only way to discover it was
    to fire another ``next_step`` and parse the resulting 400.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    with pytest.raises(PreviousStepFailedError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=False,
                verify_response=ConfirmVerifyResponse(type="confirm", answer=answer),  # type: ignore[arg-type]
            ),
        )

    # Read surface only — no further next/abort call before this.
    rows = await run_service.list_runs(
        tenant_id, OPERATOR, caller_is_admin=False, filter_=ListRunsFilter()
    )
    assert len(rows) == 1
    summary = rows[0]
    assert summary.run_id == start.run_id
    assert summary.state == "in_progress"
    assert summary.current_step_id == "step-1"
    assert summary.current_step_state == "failed"


@pytest.mark.asyncio
async def test_list_runs_current_step_state_in_progress_for_healthy_run() -> None:
    """A freshly started run projects ``current_step_state='in_progress'``."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    rows = await run_service.list_runs(
        tenant_id, OPERATOR, caller_is_admin=False, filter_=ListRunsFilter()
    )
    assert len(rows) == 1
    assert rows[0].current_step_id == "step-1"
    assert rows[0].current_step_state == "in_progress"


@pytest.mark.asyncio
async def test_list_runs_current_step_state_none_for_terminal_run() -> None:
    """Terminal runs carry no current step, so ``current_step_state`` is ``None``."""
    tenant_id = uuid.uuid4()
    single_step = RunbookTemplateBody(
        title="one",
        description="terminal step-state projection",
        steps=[_manual_step("only")],
    )
    await _seed_published_template(tenant_id, "d", body=single_step)
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    completed = await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )
    assert isinstance(completed, RunCompletedResponse)

    rows = await run_service.list_runs(
        tenant_id, OPERATOR, caller_is_admin=False, filter_=ListRunsFilter()
    )
    assert len(rows) == 1
    assert rows[0].state == "completed"
    assert rows[0].current_step_id is None
    assert rows[0].current_step_state is None


# ---------------------------------------------------------------------------
# can_show_template_post_completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_completion_check_completed_run_returns_true() -> None:
    """A completed run by the operator unlocks the read."""
    tenant_id = uuid.uuid4()
    single_step = RunbookTemplateBody(
        title="one",
        description="post-completion check",
        steps=[_manual_step("only")],
    )
    await _seed_published_template(tenant_id, "d", body=single_step)
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    await run_service.next_step(
        tenant_id,
        OPERATOR,
        start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )

    allowed = await run_service.can_show_template_post_completion(tenant_id, OPERATOR, "d", 1)
    assert allowed is True


@pytest.mark.asyncio
async def test_post_completion_check_abandoned_run_returns_true() -> None:
    """An abandoned run by the operator also unlocks the read."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)
    await run_service.abort_run(tenant_id, OPERATOR, start.run_id, AbortRunRequest(reason="stop"))

    allowed = await run_service.can_show_template_post_completion(tenant_id, OPERATOR, "d", 1)
    assert allowed is True


@pytest.mark.asyncio
async def test_post_completion_check_in_progress_returns_false() -> None:
    """An in-progress run does not unlock the read (opacity floor stays in place)."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    allowed = await run_service.can_show_template_post_completion(tenant_id, OPERATOR, "d", 1)
    assert allowed is False


# ---------------------------------------------------------------------------
# next_step session lifetime (#1352) -- the verify dispatch must not hold an
# AsyncSession across the external ``call_operation`` await, and the outcome
# write must re-validate run state in case a TENANT_ADMIN raced the gap with
# ``abort_run`` / ``reassign_run``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_step_aborted_during_dispatch_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``abort_run`` landing between the read phase and the write phase wins.

    Simulates a TENANT_ADMIN aborting the run while ``next_step`` is in
    its (now sessionless) verify-dispatch phase. The post-dispatch
    re-validation must observe the terminal flip and raise
    :class:`RunAlreadyTerminalError` rather than overwriting the
    abandoned state with a ``verified`` / ``in_progress`` outcome.
    """
    from meho_backplane.runbooks import run_service as run_service_mod

    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    original_resolve = run_service._resolve_verify_response

    async def _abort_mid_dispatch(**kwargs: Any) -> Any:
        # Fire the race inside the dispatch window: session A has closed,
        # session B has not opened. An admin aborts the run here.
        await run_service.abort_run(
            tenant_id, ADMIN, start.run_id, AbortRunRequest(reason="raced"), caller_is_admin=True
        )
        return await original_resolve(**kwargs)

    monkeypatch.setattr(run_service, "_resolve_verify_response", _abort_mid_dispatch)

    with pytest.raises(RunAlreadyTerminalError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )

    # The aborted state survived; no step was flipped to verified.
    sessionmaker = run_service_mod.get_sessionmaker()
    async with sessionmaker() as session:
        run = (
            await session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
        ).one()
        assert run.state == "abandoned"
        states = (
            await session.scalars(
                select(RunbookRunStepState).where(RunbookRunStepState.run_id == start.run_id)
            )
        ).all()
        by_id = {s.step_id: s for s in states}
        assert by_id["step-1"].state == "in_progress"
        assert by_id["step-2"].state == "pending"


@pytest.mark.asyncio
async def test_next_step_reassigned_during_dispatch_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reassign_run`` landing in the dispatch gap revokes the caller's right.

    A TENANT_ADMIN reassigns the run to another operator while the
    original assignee's ``next_step`` is dispatching. The post-dispatch
    re-validation must raise :class:`NotRunAssigneeError` rather than
    advancing on behalf of the now-former assignee.
    """
    from meho_backplane.runbooks import run_service as run_service_mod

    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    original_resolve = run_service._resolve_verify_response

    async def _reassign_mid_dispatch(**kwargs: Any) -> Any:
        await run_service.reassign_run(
            tenant_id, ADMIN, start.run_id, ReassignRunRequest(new_assignee=OPERATOR_BETA)
        )
        return await original_resolve(**kwargs)

    monkeypatch.setattr(run_service, "_resolve_verify_response", _reassign_mid_dispatch)

    with pytest.raises(NotRunAssigneeError):
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )

    # The run still belongs to OPERATOR_BETA and step-1 was not advanced.
    sessionmaker = run_service_mod.get_sessionmaker()
    async with sessionmaker() as session:
        run = (
            await session.scalars(select(RunbookRun).where(RunbookRun.run_id == start.run_id))
        ).one()
        assert run.assigned_to == OPERATOR_BETA
        states = (
            await session.scalars(
                select(RunbookRunStepState).where(RunbookRunStepState.run_id == start.run_id)
            )
        ).all()
        assert {s.step_id: s.state for s in states}["step-1"] == "in_progress"


@pytest.mark.asyncio
async def test_next_step_does_not_pin_connection_across_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow verify dispatch must not pin a pooled connection.

    With a ``pool_size=1, max_overflow=0`` pool, the old single-session
    shape held the lone connection for the full ``call_operation`` await,
    so a concurrent query queued behind it for the entire (here 500ms)
    dispatch. With the read/dispatch/write split, the connection is back
    in the pool during the dispatch, so the concurrent probe returns
    promptly. The assertion is a generous margin below the dispatch
    duration -- a regression to the pinned shape would blow past it.
    """
    from meho_backplane.runbooks import run_service as run_service_mod

    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "d", body=_two_step_template())
    run_service = RunbookRunService()
    start = await run_service.start_run(
        tenant_id, OPERATOR, StartRunRequest(template_slug="d", target="n", params={})
    )
    assert isinstance(start, CurrentStepResponse)

    # A single-connection pool over the same per-test SQLite file. The
    # production engine factory prunes pool_size for SQLite URLs, so we
    # build the constrained engine directly to make the pinning failure
    # mode observable. aiosqlite uses AsyncAdaptedQueuePool for a
    # file-backed URL, which honours pool_size / max_overflow.
    db_url = get_settings().database_url
    pinned_engine = create_async_engine(db_url, pool_size=1, max_overflow=0, pool_timeout=30)
    pinned_sessionmaker = async_sessionmaker(pinned_engine, expire_on_commit=False)
    monkeypatch.setattr(run_service_mod, "get_sessionmaker", lambda: pinned_sessionmaker)

    dispatch_secs = 0.5

    async def _slow_resolve(**kwargs: Any) -> Any:
        # Stand in for a slow external connector dispatch. The real
        # _resolve_verify_response would await call_operation here; the
        # point is purely that no session is checked out during this await.
        await asyncio.sleep(dispatch_secs)
        return kwargs["request_verify"]

    monkeypatch.setattr(run_service, "_resolve_verify_response", _slow_resolve)

    dispatch_started = asyncio.Event()

    async def _drive_next_step() -> None:
        dispatch_started.set()
        await run_service.next_step(
            tenant_id,
            OPERATOR,
            start.run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )

    async def _probe() -> float:
        await dispatch_started.wait()
        # Let next_step reach its (sessionless) dispatch phase before we probe.
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        async with pinned_sessionmaker() as probe_session:
            await probe_session.execute(text("SELECT 1"))
        return time.perf_counter() - t0

    try:
        driver = asyncio.create_task(_drive_next_step())
        probe_elapsed = await _probe()
        await driver
    finally:
        await pinned_engine.dispose()

    # The probe must not have queued behind the full dispatch. Half the
    # dispatch window is a wide margin: the pinned shape would force the
    # probe to wait the entire dispatch_secs.
    assert probe_elapsed < dispatch_secs / 2, (
        f"probe waited {probe_elapsed * 1000:.0f}ms -- connection was pinned "
        f"across the {dispatch_secs * 1000:.0f}ms dispatch"
    )


@pytest.mark.asyncio
async def test_next_step_falsy_operation_result_preserved_in_forensics(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A falsy ``result`` payload (``[]`` / ``0``) must survive into ``actual``.

    The verify-dispatch path used ``call_result.get("result") or {}``,
    which collapsed any falsy payload to ``{}`` and erased the forensics
    record. The fix uses ``.get("result", {})`` so only an absent key
    defaults; falsy-but-present values flow into the non-dict wrap and
    round-trip into the persisted ``verify_response.actual``.
    """
    await _seed_stub_op(stub_embedding_service)

    for slug, falsy_result, expected_actual in (
        ("empty-list", [], {"result": []}),
        ("zero", 0, {"result": 0}),
    ):
        tenant_id = uuid.uuid4()
        await _seed_target(tenant_id, "n")
        await _seed_published_template(tenant_id, slug, body=_op_call_template())
        run_service = RunbookRunService()
        start = await run_service.start_run(
            tenant_id, OPERATOR, StartRunRequest(template_slug=slug, target="n", params={})
        )
        assert isinstance(start, CurrentStepResponse)

        # Stub only the dispatch so _resolve_verify_response builds
        # ``actual`` from a falsy ``result``; the connector-id resolution
        # (a DB lookup before the dispatch) still runs against the seeded
        # stub op.
        async def _falsy_call(operator: Any, arguments: Any, _r: Any = falsy_result) -> Any:
            return {"status": "success", "result": _r}

        monkeypatch.setattr("meho_backplane.runbooks.run_service.call_operation", _falsy_call)

        # expect={"ok": True} won't match a list/scalar actual, so the
        # step fails -- but the verify_response is persisted before the
        # PreviousStepFailedError is raised.
        with pytest.raises(PreviousStepFailedError):
            await run_service.next_step(
                tenant_id,
                OPERATOR,
                start.run_id,
                NextStepRequest(last_verified=True, verify_response=None),
            )

        row = (
            await db_session.scalars(
                select(RunbookRunStepState)
                .where(RunbookRunStepState.run_id == start.run_id)
                .where(RunbookRunStepState.step_id == "call-it")
            )
        ).one()
        assert row.state == "failed"
        assert isinstance(row.verify_response, dict)
        assert row.verify_response["actual"] == expected_actual
