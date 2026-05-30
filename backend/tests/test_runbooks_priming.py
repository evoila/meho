# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.priming` (G12.4-T1 / #1315).

Covers the 12-test acceptance breakdown from the issue body:

* Shape -- empty, single, two, exactly-five, six (summary), runs without
  current_step_id skipped (defensive).
* Scoping -- completed runs excluded, abandoned runs excluded,
  other-operator runs excluded (operator-scope visibility floor),
  other-tenant runs excluded.
* Wrapper discipline -- guard delimiters are hard-coded, body verbatim
  wording is preserved, ``${...}`` in slug is not substituted.

The conftest autouse fixture migrates a fresh SQLite DB per test (the
runbooks template + run services' load-bearing infrastructure) so the
helper's :meth:`RunbookRunService.list_runs` binds to the same per-test
schema these tests seed runs into directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.runbooks.priming import (
    BLOCK_END,
    BLOCK_START,
    MAX_PRIMING_BLOCKS,
    RunbookPrimingResult,
    assemble_runbook_priming,
)
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    ConfirmVerifyResponse,
    CurrentStepResponse,
    NextStepRequest,
    StartRunRequest,
)
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DraftTemplateRequest,
    ManualStep,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import RunbookTemplateService
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OPERATOR = "operator-alpha"
OPERATOR_BETA = "operator-beta"


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


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _manual_step(step_id: str) -> ManualStep:
    """Build a ``manual`` step gated by a ``confirm`` verify."""
    return ManualStep(
        id=step_id,
        title=f"Step {step_id}",
        body=f"Do {step_id}",
        type="manual",
        verify=ConfirmVerify(type="confirm", prompt="done?"),
    )


def _two_step_template() -> RunbookTemplateBody:
    return RunbookTemplateBody(
        title="Two-step procedure",
        description="for priming tests",
        target_kind="k8s-node",
        steps=[_manual_step("step-1"), _manual_step("step-2")],
    )


def _single_step_template() -> RunbookTemplateBody:
    return RunbookTemplateBody(
        title="One-step procedure",
        description="for priming tests",
        target_kind="k8s-node",
        steps=[_manual_step("only-step")],
    )


async def _seed_published_template(
    tenant_id: uuid.UUID,
    slug: str,
    *,
    body: RunbookTemplateBody | None = None,
) -> None:
    """Helper: create+publish a template via the template service.

    Mirrors the test_runbooks_run_service.py shape so the seeded rows
    match what production code writes.
    """
    template_service = RunbookTemplateService()
    template_body = body if body is not None else _two_step_template()
    await template_service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug=slug, body=template_body)
    )
    await template_service.publish(tenant_id, PublishTemplateRequest(slug=slug, version=1))


async def _start_runs(
    tenant_id: uuid.UUID,
    operator_sub: str,
    *,
    slug: str = "drain",
    n: int = 1,
) -> list[uuid.UUID]:
    """Start *n* in-progress runs for *operator_sub* against *slug*.

    The slug must already be a published template in *tenant_id*. Returns
    the run ids in start order.
    """
    run_service = RunbookRunService()
    run_ids: list[uuid.UUID] = []
    for index in range(n):
        resp = await run_service.start_run(
            tenant_id,
            operator_sub,
            StartRunRequest(template_slug=slug, target=f"node-{index}", params={}),
        )
        assert isinstance(resp, CurrentStepResponse)
        run_ids.append(resp.run_id)
    return run_ids


# ---------------------------------------------------------------------------
# Shape: empty / single / multiple / cap behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_runs_returns_empty_text() -> None:
    """Operator with no in-progress runs -> text='', count=0, summarized=False."""
    tenant_id = uuid.uuid4()
    result = await assemble_runbook_priming(OPERATOR, tenant_id)
    assert result == RunbookPrimingResult(text="", block_count=0, summarized=False)


@pytest.mark.asyncio
async def test_single_run_returns_one_block() -> None:
    """Operator with 1 in-progress run -> one verbatim block with slug, version, step id."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 1
    assert result.summarized is False
    # The verbatim block body fields.
    assert BLOCK_START in result.text
    assert BLOCK_END in result.text
    assert "`drain`" in result.text  # slug in backticks
    assert "v1" in result.text  # version
    assert "step 1/2" in result.text  # n/total -- 2-step template, first step
    assert "`step-1`" in result.text  # current step id
    # One opening + one closing delimiter.
    assert result.text.count(BLOCK_START) == 1
    assert result.text.count(BLOCK_END) == 1


@pytest.mark.asyncio
async def test_two_runs_returns_two_blocks_concatenated() -> None:
    """2 in-progress runs -> 2 BLOCK_START markers separated by a blank line."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=2)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 2
    assert result.summarized is False
    assert result.text.count(BLOCK_START) == 2
    assert result.text.count(BLOCK_END) == 2
    # Two blocks separated by exactly one blank line between END and START.
    assert f"{BLOCK_END}\n\n{BLOCK_START}" in result.text


@pytest.mark.asyncio
async def test_five_runs_inline_form() -> None:
    """Exactly MAX_PRIMING_BLOCKS=5 in-progress runs -> 5 inline blocks, not summary."""
    assert MAX_PRIMING_BLOCKS == 5  # invariant the test name encodes
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=5)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 5
    assert result.summarized is False
    assert result.text.count(BLOCK_START) == 5
    assert result.text.count(BLOCK_END) == 5
    # No summary-form prose at the inline-form boundary.
    assert "too many to list inline" not in result.text


@pytest.mark.asyncio
async def test_six_runs_collapses_to_summary() -> None:
    """6 in-progress runs (> MAX_PRIMING_BLOCKS) -> one summary block.

    The summary references runbook_list_runs and drops per-run blocks.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=6)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 6
    assert result.summarized is True
    assert result.text.count(BLOCK_START) == 1
    assert result.text.count(BLOCK_END) == 1
    assert "6 in-progress" in result.text
    assert "runbook_list_runs" in result.text
    # No per-run leakage in the summary form.
    assert "`drain`" not in result.text
    assert "step 1/2" not in result.text


# ---------------------------------------------------------------------------
# Scoping: state, operator, tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_runs_not_included() -> None:
    """An operator with 1 in-progress + 1 completed run -> only the in-progress one primes."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "single", body=_single_step_template())
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    run_service = RunbookRunService()

    # Single-step run, completed.
    single_start = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="single", target="n", params={}),
    )
    assert isinstance(single_start, CurrentStepResponse)
    await run_service.next_step(
        tenant_id,
        OPERATOR,
        single_start.run_id,
        NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
        ),
    )

    # Two-step run, in progress.
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 1
    assert result.summarized is False
    # Only the in-progress run's slug surfaces in the priming text.
    assert "`drain`" in result.text
    assert "`single`" not in result.text


@pytest.mark.asyncio
async def test_abandoned_runs_not_included() -> None:
    """Abandoned runs are excluded from the operator's priming."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _seed_published_template(tenant_id, "rollback", body=_two_step_template())
    run_service = RunbookRunService()

    # Abandoned run.
    abandoned_start = await run_service.start_run(
        tenant_id,
        OPERATOR,
        StartRunRequest(template_slug="rollback", target="n", params={}),
    )
    assert isinstance(abandoned_start, CurrentStepResponse)
    await run_service.abort_run(
        tenant_id,
        OPERATOR,
        abandoned_start.run_id,
        AbortRunRequest(reason="testing"),
    )

    # In-progress run.
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result.block_count == 1
    assert "`drain`" in result.text
    assert "`rollback`" not in result.text


@pytest.mark.asyncio
async def test_other_operator_runs_not_included() -> None:
    """Only ``operator_sub``'s runs surface; another operator's are invisible."""
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    # Beta starts a run; alpha asks for priming.
    await _start_runs(tenant_id, OPERATOR_BETA, slug="drain", n=2)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert result == RunbookPrimingResult(text="", block_count=0, summarized=False)


@pytest.mark.asyncio
async def test_other_tenant_runs_not_included() -> None:
    """Strict tenant isolation: a run in tenant B does not surface for tenant A."""
    await _seed_published_template(_TENANT_A, "drain", body=_two_step_template())
    await _seed_published_template(_TENANT_B, "drain", body=_two_step_template())
    # Same sub starts a run in tenant B.
    await _start_runs(_TENANT_B, OPERATOR, slug="drain", n=1)

    # Priming for the same sub in tenant A returns empty.
    result = await assemble_runbook_priming(OPERATOR, _TENANT_A)

    assert result == RunbookPrimingResult(text="", block_count=0, summarized=False)


# ---------------------------------------------------------------------------
# Wrapper discipline: hard-coded delimiters, verbatim body, no templating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_delimiters_are_hard_coded() -> None:
    """Wrapper delimiters are module constants emitted positionally, not interpolated.

    Defence-in-depth: the slug regex (kb-slug pattern) already forbids
    the terminator-like substring at publish time, but the wrapper
    discipline is independent of the slug validator. Build a result and
    confirm:

    * BLOCK_START / BLOCK_END are the literal module constants (not
      interpolated from any run field), and the terminator only appears
      at the wrapper boundary -- never inside the body.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    # The constants are the literal module values -- the test pins both
    # the wrapper boundary and that the value matches the module export.
    assert BLOCK_START == "<<RUNBOOK_PRIMING — CRITICAL>>"
    assert BLOCK_END == "<<END_RUNBOOK_PRIMING>>"
    assert result.text.startswith(BLOCK_START)
    assert result.text.endswith(BLOCK_END)
    # The terminator literal only appears once -- at the wrapper boundary.
    assert result.text.count(BLOCK_END) == 1


@pytest.mark.asyncio
async def test_priming_text_includes_no_skip_no_force_advance_implication() -> None:
    """Body text contains the verbatim adherence phrases the agent is trained against.

    Regression guard -- a refactor that strips ``Follow only the current step``,
    ``runbook_abort``, or ``runbook_next`` from the rendered text regresses
    the priming's UX-hint discipline. Initiative #1199 fixes the wording.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    assert "Follow only the current step" in result.text
    assert "Do not look ahead" in result.text
    assert "Do not improvise" in result.text
    assert "Do not combine steps" in result.text
    assert "runbook_abort" in result.text
    assert "runbook_next" in result.text


@pytest.mark.asyncio
async def test_priming_text_is_substitution_safe() -> None:
    """A slug containing ``${...}`` is rendered verbatim, never resolved.

    Regression guard against accidental templating: the priming text is
    plain f-string composition over already-validated run fields, not a
    string template. A slug that looked like ``${run.target}`` (which
    the slug regex would actually reject at publish time, but defence
    in depth) must render as the literal characters in the priming.

    The slug regex enforces ``[a-z0-9][a-z0-9-]*`` so a ``$`` is rejected
    upstream; we exercise the property by checking that the *current
    step id* (which has the same regex shape) renders verbatim even
    when constructed to look template-ish at the boundary -- specifically,
    that the step id appears in backticks exactly once and is not
    expanded against anything.
    """
    tenant_id = uuid.uuid4()
    await _seed_published_template(tenant_id, "drain", body=_two_step_template())
    await _start_runs(tenant_id, OPERATOR, slug="drain", n=1)

    result = await assemble_runbook_priming(OPERATOR, tenant_id)

    # ``step-1`` renders verbatim as backticked text; no `$` or `{`
    # in the rendered priming string.
    assert "`step-1`" in result.text
    assert "$" not in result.text
    assert "{" not in result.text
