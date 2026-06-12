# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shape contract for the G12.3 runbook *run* lifecycle (#1300).

Companion to :mod:`meho_backplane.runbooks.schemas` (the template-side
shapes from #1295). Split deliberately: template-side schemas are
authoring shapes (mutable drafts, full step lists, fork visibility);
run-side schemas are *execution* shapes (run state, opaque current step,
verify responses, ownership transitions). Splitting keeps each module
readable and lets the G12.3-T2 / T3 reviewers focus on run logic without
scrolling past authoring types.

The load-bearing data structure here is :class:`StepBody` -- the
*opaque-by-construction* single-step shape returned by the
``runbook_next`` tool. The structural property that makes the entire
Initiative #1198 adherence floor real is that this shape carries
**exactly one step body** (the one the operator is currently on, with
``${run.target}`` / ``${run.params.X}`` substitutions resolved) and no
adjacent or future steps. The agent literally cannot see step 3 if
``step_state[2].state != 'verified'`` because the response shape has no
field for it. The regression test
:func:`test_step_body_omits_future_step_fields` keeps that property
honest at refactor time.

Two discriminated unions are defined here, both following the same
``Annotated[..., Field(discriminator=...)]`` pattern the predecessor
module established:

* :data:`VerifyResponse` -- discriminated on ``type``
  (``"confirm"`` / ``"operation_call"``); the operator/engine's answer to
  the current step's verify gate.
* :data:`NextStepResponse` -- discriminated on ``kind``
  (``"current_step"`` / ``"completed"``); the ``runbook_next`` tool's
  reply shape. The explicit ``kind`` tag is chosen deliberately over a
  field-presence-based callable discriminator because (a) it is more
  debuggable at the wire boundary, and (b) it matches the existing
  step / verify discriminator pattern from #1295.

Frozen IO models, alphabetised ``__all__``, model validators that raise
:class:`ValueError` so Pydantic surfaces them as a clean 422 at the HTTP
boundary -- the same posture as the template-side module. No engine
logic, no service plumbing, no routes; this is the validation layer
between operator input / engine output and the JSONB columns on
``runbook_runs`` / ``runbook_run_step_states`` (G12.1-T1, #1292).

The closed vocabularies (``state in {'in_progress', 'completed',
'abandoned'}`` for runs; per-step shapes via :class:`StepBody`) mirror
the storage-level ``CheckConstraint`` on the SQLAlchemy models, so a
shape that surfaces here is one that the database also accepts -- one
contract, two layers, no drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AbortRunRequest",
    "AbortRunResponse",
    "ConfirmVerifyResponse",
    "CurrentStepResponse",
    "ListRunsFilter",
    "NextStepRequest",
    "NextStepResponse",
    "OperationCallVerifyResponse",
    "ReassignRunRequest",
    "ReassignRunResponse",
    "RunCompletedResponse",
    "RunSummary",
    "StartRunRequest",
    "StepBody",
    "StepBodyVerify",
    "StepPosition",
    "VerifyResponse",
]


class ConfirmVerifyResponse(BaseModel):
    """Operator's answer to a ``confirm``-typed verify step.

    Only :attr:`answer` ``= "yes"`` advances the run. ``"no"`` transitions
    the step to ``failed``; ``"escalate"`` transitions to ``failed`` with
    ``verify_response.escalated=true`` semantics so the senior's review
    path is explicit in the persisted state (per Initiative #1198).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["confirm"]
    answer: Literal["yes", "no", "escalate"]


class OperationCallVerifyResponse(BaseModel):
    """Captured result of a dispatched ``operation_call`` verify step.

    The engine populates this from ``call_operation()``'s return value;
    callers do not construct it themselves. Stored in
    ``runbook_run_step_states.verify_response`` for later replay /
    audit. :attr:`matched` is ``True`` when the structural-equality +
    presence match against the template-side ``expect`` succeeded;
    :attr:`actual` is the call result, retained verbatim for the
    mismatch-case forensics.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["operation_call"]
    matched: bool
    actual: dict[str, object]


#: The verify-response surface a caller (operator) sends back to
#: ``runbook_next`` or the engine populates from a dispatched call.
#: Discriminated on ``type`` -- the same tag the template-side
#: :class:`~meho_backplane.runbooks.schemas.Verify` union uses, so the
#: response shape mirrors the gate shape one-for-one. An unknown ``type``
#: surfaces as a Pydantic validation error (no silent fall-through to
#: the first union member).
VerifyResponse = Annotated[
    ConfirmVerifyResponse | OperationCallVerifyResponse,
    Field(discriminator="type"),
]


class StepBodyVerify(BaseModel):
    """The verify surface as exposed at *run* time -- substituted-and-frozen.

    Same discriminator (``type``) as the template-side
    :class:`~meho_backplane.runbooks.schemas.Verify` union, but every
    ``${run.target}`` / ``${run.params.X}`` substitution in
    :attr:`op_id` / :attr:`params` / :attr:`expect` has already been
    resolved by the engine (#1301). This shape is what the operator /
    agent reads to know *what they will be asked* once the step's
    action is performed -- the prompt text (for ``confirm``) or the
    op-call shape and expected result (for ``operation_call``).

    Fields are nullable by ``type`` (``prompt`` populated only on
    ``confirm``; ``op_id`` / ``params`` / ``expect`` populated only on
    ``operation_call``). A flat shape with optional fields is used
    rather than a discriminated sub-union because :class:`StepBody`
    already discriminates at the parent level on ``StepBody.type``;
    nesting a second discriminated union here would force two parse
    paths for one logical decision and complicates JSON-Schema
    generation.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["confirm", "operation_call"]
    prompt: str | None = None
    op_id: str | None = None
    params: dict[str, object] | None = None
    expect: dict[str, object] | None = None


class StepBody(BaseModel):
    """The opaque-by-construction single-step shape returned by ``runbook_next``.

    All ``${run.target}`` and ``${run.params.X}`` substitutions are
    already resolved by the engine (G12.3-T2, #1301); the strings here
    are post-substitution and final. This is what the operator / agent
    sees -- *and the only step they see* at this position in the run.

    What this shape carries:

    * :attr:`id` -- the step's id (matches ``runbook_run_step_states.step_id``).
    * :attr:`title` / :attr:`body` -- substituted authoring text.
    * :attr:`type` -- ``"operation_call"`` (MEHO dispatches the action) or
      ``"manual"`` (operator performs the step off-MEHO).
    * :attr:`op_id` / :attr:`params` -- populated only for
      ``operation_call`` steps; the substituted call shape.
    * :attr:`verify` -- the substituted-and-frozen verify gate the
      caller must respond to on the next ``runbook_next`` call.

    What this shape **must not** carry, by structural construction
    (regression-tested in ``test_step_body_omits_future_step_fields``):

    * The full template's step list.
    * Any reference to step ids other than the current one.
    * The unsubstituted (template) body.

    No discriminated union split between operation-call and manual
    variants is used here -- ``op_id`` / ``params`` are simply nullable
    by ``type``. The split would buy stronger typing at the cost of an
    extra adapter layer for callers that just want to render the
    body; the issue spec explicitly chose the flat shape (#1300).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    body: str
    type: Literal["operation_call", "manual"]
    op_id: str | None = None
    params: dict[str, object] | None = None
    verify: StepBodyVerify


class StepPosition(BaseModel):
    """1-indexed position of the current step within the template.

    :attr:`n` is the 1-indexed step number; :attr:`total` is the
    template's full step count. Position is the **only** structural
    hint about the template's overall shape that the run surface
    exposes -- operators need to know "step 3 of 12" for progress UX,
    but exposing the count alone is materially different from
    exposing the *contents* of the other steps (which :class:`StepBody`
    deliberately does not).

    Invariants (enforced by :meth:`_validate_n_within_total`):

    * ``n >= 1`` (Pydantic ``Field(ge=1)``) -- there is no step 0.
    * ``total >= 1`` (Pydantic ``Field(ge=1)``) -- a runbook has at
      least one step; an empty template is rejected at publish time
      anyway (#1295).
    * ``n <= total`` -- you cannot be on step 11 of a 10-step template.
    """

    model_config = ConfigDict(frozen=True)

    n: Annotated[int, Field(ge=1)]
    total: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _validate_n_within_total(self) -> StepPosition:
        """Reject ``n > total`` with a 422-shaped ``ValueError``."""
        if self.n > self.total:
            raise ValueError(f"step position n ({self.n}) exceeds total ({self.total})")
        return self


class StartRunRequest(BaseModel):
    """Request body for ``runbook_start`` -- begin a new run on a template.

    :attr:`template_slug` references a *published* runbook template; the
    service layer (G12.3-T3) resolves it to a pinned ``(slug, version)``
    at start time so later template edits cannot alter this run's step
    list (per Initiative #1198 deprecation interplay rules).
    :attr:`target` is the run's subject (the host, the cluster, the
    cert thumbprint); :attr:`params` is the substitution context for
    ``${run.params.X}`` and may be empty.
    """

    model_config = ConfigDict(frozen=True)

    template_slug: str
    target: str
    params: dict[str, object] = Field(default_factory=dict)


class CurrentStepResponse(BaseModel):
    """Returned by ``runbook_start`` and the non-completion path of ``runbook_next``.

    Carries the run coordinates (``run_id`` / ``template_slug`` /
    ``template_version``), the structural position hint
    (:class:`StepPosition`), and -- crucially -- exactly one
    :class:`StepBody`: the step the run is *currently on*. No previous
    steps (already executed), no following steps (the opacity property
    this Initiative is built around).

    Tagged with ``kind: Literal["current_step"]`` so the parent
    :data:`NextStepResponse` discriminated union routes payloads on a
    visible field rather than a callable shape-sniffing discriminator
    (option 1 of the design decision documented at the module docstring
    and the #1300 issue body).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["current_step"] = "current_step"
    run_id: uuid.UUID
    template_slug: str
    template_version: int
    position: StepPosition
    current_step: StepBody


class RunCompletedResponse(BaseModel):
    """Returned by ``runbook_next`` when the previous step was the last.

    The terminal-state shape: no step body, just the run coordinates
    and the transition timestamp. The companion abort-side shape is
    :class:`AbortRunResponse`.

    The :attr:`state` and :attr:`kind` literals both carry ``"completed"``
    by design: :attr:`kind` is the parent-union discriminator (per the
    explicit-tag decision documented at the module docstring), while
    :attr:`state` matches the run's storage-level state column
    (``runbook_runs.state``) so the response shape mirrors the row
    one-for-one.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["completed"] = "completed"
    run_id: uuid.UUID
    state: Literal["completed"] = "completed"
    completed_at: datetime


#: Reply shape of ``runbook_next`` -- discriminated on ``kind``. The
#: non-completion path returns a :class:`CurrentStepResponse` (one step
#: body, no future-step leakage); the completion path returns a
#: :class:`RunCompletedResponse` (terminal-state marker, no step
#: content). An unknown ``kind`` surfaces as a Pydantic validation
#: error (no silent fall-through).
NextStepResponse = Annotated[
    CurrentStepResponse | RunCompletedResponse,
    Field(discriminator="kind"),
]


class NextStepRequest(BaseModel):
    """Request body for ``runbook_next`` -- advance the run.

    :attr:`last_verified` is the caller's *claim* that the previous
    step's verify gate was satisfied. It is **informational only**:
    the substrate (engine, G12.3-T2) is the verify oracle, and a
    request from a caller whose previous-step state is not
    ``verified`` returns 400 *regardless* of what
    :attr:`last_verified` says. The field exists so the wire log
    captures the caller's belief alongside the substrate's decision
    -- useful for diagnosing a client that thinks it advanced when
    the substrate did not.

    :attr:`verify_response` carries the operator's answer for a
    ``confirm`` step or the engine's captured result for an
    ``operation_call`` step. ``None`` is valid only on the very first
    ``runbook_next`` call (when no prior step exists to verify).
    """

    model_config = ConfigDict(frozen=True)

    last_verified: bool
    verify_response: VerifyResponse | None = None


class AbortRunRequest(BaseModel):
    """Request body for ``runbook_abort`` -- terminate the run mid-flight.

    :attr:`reason` is required and non-empty (``Field(min_length=1)``)
    because it is persisted to ``audit_log.payload`` for the abort
    event -- an empty reason would defeat the audit trail that
    Initiative #1198's "abort-with-audit" guarantee rests on.
    """

    model_config = ConfigDict(frozen=True)

    reason: Annotated[str, Field(min_length=1)]


class AbortRunResponse(BaseModel):
    """Returned by ``runbook_abort`` -- the terminal-state coordinates."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    state: Literal["abandoned"] = "abandoned"
    abandoned_at: datetime


class ReassignRunRequest(BaseModel):
    """Request body for ``runbook_reassign`` -- transfer ownership of a run.

    :attr:`new_assignee` is the operator subject identifier of the
    new owner. Non-empty (``Field(min_length=1)``) because the
    reassign path writes to ``runbook_runs.assigned_to`` which is
    ``NOT NULL`` at the storage layer and is the predicate for
    every subsequent ``runbook_next`` ownership check.
    """

    model_config = ConfigDict(frozen=True)

    new_assignee: Annotated[str, Field(min_length=1)]


class ReassignRunResponse(BaseModel):
    """Returned by ``runbook_reassign`` -- the new ownership coordinates."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    assigned_to: str
    reassigned_at: datetime


class ListRunsFilter(BaseModel):
    """Optional filters for ``runbook_list_runs``.

    All three fields default to ``None`` (no filter applied). A bare
    :class:`ListRunsFilter` -- equivalent to passing no filter at all
    -- lists every run the caller can see: own runs for ``OPERATOR``,
    all tenant runs for ``TENANT_ADMIN`` (the visibility split is
    enforced at the service layer, G12.3-T3).

    :attr:`assignee` filters to a single operator subject;
    :attr:`status` to a single run state from the closed vocabulary
    (matching the storage-level ``CheckConstraint`` on
    ``runbook_runs.state``); :attr:`template_slug` to a single
    template (across all versions).
    """

    model_config = ConfigDict(frozen=True)

    assignee: str | None = None
    status: Literal["in_progress", "completed", "abandoned"] | None = None
    template_slug: str | None = None


class RunSummary(BaseModel):
    """List-view projection returned by ``runbook_list_runs``.

    Run-level state only: no step contents are exposed. The
    step-by-step content is opaque-by-construction (only
    ``runbook_next`` ever returns a step body, and only one step at a
    time), so :attr:`current_step_id` is the *id* of the step the
    run is currently on -- enough for a UI to render "step 3:
    drain-node" -- but not the body.

    :attr:`current_step_id` and :attr:`position` are ``None`` for
    runs in terminal state (``completed`` / ``abandoned``); a
    terminal-state run has no "current" step.

    :attr:`completed_at` and :attr:`abandoned_at` are mutually
    exclusive (and both ``None`` for ``in_progress`` runs); the
    transition that fired sets the corresponding column on
    ``runbook_runs``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    template_slug: str
    template_version: int
    assigned_to: str
    target: str
    state: Literal["in_progress", "completed", "abandoned"]
    started_at: datetime
    completed_at: datetime | None = None
    abandoned_at: datetime | None = None
    current_step_id: str | None = None
    position: StepPosition | None = None
