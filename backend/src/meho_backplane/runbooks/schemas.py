# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 shape contract for the G12.2 runbook template lifecycle (#1295).

Pure types -- no service logic, no routes, no tools. Every downstream
G12.2 surface (service, REST routes, MCP tools) and the G12.3 execution
engine validate against these models. The storage-level shapes are the
SQLAlchemy models in :mod:`meho_backplane.db.models` (G12.1-T1, #1292);
this module is the validation layer that sits between operator input and
the ``runbook_templates.steps`` JSONB column.

The load-bearing data structure is the **step shape**. Each step is a
discriminated union on ``type``:

* :class:`OperationCallStep` (``type="operation_call"``) -- the agent
  dispatches the step via the operation registry.
* :class:`ManualStep` (``type="manual"``) -- the operator performs the
  step off-MEHO (SSH, web UI) and reports back.

Each step's ``verify`` field is itself a discriminated union on ``type``:

* :class:`ConfirmVerify` (``type="confirm"``) -- operator answers the
  prompt; only an affirmative advances.
* :class:`OperationCallVerify` (``type="operation_call"``) -- MEHO
  dispatches the verify call and matches the result against ``expect``
  by structural-equality + presence (no operators, no JSONPath, no
  boolean composition -- substrate minimalism, same call as #1177).

The discriminated-union + model-validator posture mirrors
:class:`meho_backplane.scheduler.schemas.ScheduledTriggerCreate`: a
malformed body surfaces as a clean 422 at the HTTP boundary (Pydantic
renders ``ValueError`` as a validation error) rather than as a flush-time
failure deeper in the stack.

The ``${...}`` substitution allowlist (:func:`validate_substitutions`)
is defense in depth: at **publish** time the template body is walked
recursively and every substitution pattern except ``${run.target}`` and
``${run.params.X}`` is rejected. G12.3 re-checks at advance time using
the same exported helper, so a template that somehow reached storage with
a disallowed pattern still cannot expand it at runtime.

The ``slug`` identifier reuses :data:`meho_backplane.kb.schemas.SLUG_PATTERN`
verbatim (per #1292 / G12.1-T1) -- it is imported, never redefined.
Step ids use the tighter :data:`STEP_ID_PATTERN`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from meho_backplane.kb.schemas import SLUG_PATTERN

__all__ = [
    "SLUG_PATTERN",
    "STEP_ID_PATTERN",
    "ConfirmVerify",
    "DeprecateTemplateRequest",
    "DeprecateTemplateResponse",
    "DiscardTemplateRequest",
    "DiscardTemplateResponse",
    "DraftTemplateRequest",
    "DraftTemplateResponse",
    "EditTemplateRequest",
    "EditTemplateResponse",
    "ForkInfo",
    "ListTemplatesFilter",
    "ManualStep",
    "OperationCallStep",
    "OperationCallVerify",
    "PublishTemplateRequest",
    "PublishTemplateResponse",
    "RunbookTemplateBody",
    "ShowTemplateResponse",
    "Step",
    "TemplateSummary",
    "Verify",
    "validate_op_id_static",
    "validate_substitutions",
]


#: Step id pattern. Same lowercase-anchored shape as the kb slug pattern
#: but capped tighter (no dots) because step ids are short procedure
#: handles authors type by hand (``revoke-old-cert``,
#: ``verify-cluster-quorum``), not version-numbered filenames. Anchored
#: start + end: a lowercase letter leads, the remainder is lowercase
#: letters / digits / hyphens, total length <= 64.
STEP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{0,63}$")

#: Every ``${...}`` occurrence. The inner group is captured non-greedily
#: so adjacent substitutions in one string are matched independently. The
#: capture excludes the braces, so the captured text is the bare path
#: (``run.target``, ``run.params.foo``).
_SUBSTITUTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([^}]*)\}")

#: The parameter-name grammar inside ``${run.params.X}``. Lowercase
#: letter or underscore leads; remainder is lowercase letters / digits /
#: underscores. No dots, so a nested path (``${run.params.X.Y}``) is
#: rejected -- only one flat param level is allowed.
_PARAM_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^run\.params\.[a-z_][a-z0-9_]*$")


def validate_substitutions(value: object) -> None:
    """Reject every ``${...}`` pattern except the two allowlisted forms.

    Walks *value* recursively (``str`` / ``dict`` / ``list``) and inspects
    every ``${...}`` occurrence found in any string. The only accepted
    patterns are:

    * ``${run.target}`` -- the run's target host / resource.
    * ``${run.params.X}`` -- a run parameter, where ``X`` matches
      ``[a-z_][a-z0-9_]*`` (one flat level; no nested ``X.Y``).

    Any other pattern raises :class:`ValueError`. Dict keys are walked as
    well as values: a substitution smuggled into a key
    (``{"${evil}": 1}``) is just as dangerous as one in a value.

    The function returns ``None`` on success and is the single sink both
    this module's template validator (publish time) and G12.3's execution
    engine (advance time) call -- centralising the allowlist so a future
    contract change ships in one place.
    """
    if isinstance(value, str):
        for match in _SUBSTITUTION_PATTERN.finditer(value):
            inner = match.group(1)
            if inner == "run.target":
                continue
            if _PARAM_NAME_PATTERN.match(inner):
                continue
            raise ValueError(f"disallowed substitution pattern: {match.group(0)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_substitutions(key)
            validate_substitutions(item)
    elif isinstance(value, list):
        for item in value:
            validate_substitutions(item)
    # Scalars other than str (int / float / bool / None) carry no
    # substitution surface; nothing to walk.


def validate_op_id_static(op_id: str) -> None:
    """Reject *any* ``${...}`` substitution token in an ``op_id``.

    Stricter than :func:`validate_substitutions`: an ``op_id`` names the
    operation a runbook step dispatches -- it is operation *identity*,
    not call payload. Neither allowlisted form (``${run.target}`` /
    ``${run.params.X}``) is legal here. Permitting even an allowlisted
    substitution would let an operator-supplied run parameter redirect a
    published step or verify to a different operation at runtime
    (parameter injection into operation identity), defeating the
    publish-time review of which operations a runbook may invoke. The
    operation set a runbook can call is fixed at publish time.

    Raised as :class:`ValueError` so Pydantic renders it as a 422 at the
    HTTP boundary, matching :func:`validate_substitutions`.
    """
    match = _SUBSTITUTION_PATTERN.search(op_id)
    if match is not None:
        raise ValueError(f"disallowed substitution in op_id: {match.group(0)}")


class ConfirmVerify(BaseModel):
    """Operator answers a yes/no prompt; only an affirmative advances.

    The minimal verify shape: MEHO shows :attr:`prompt` to the operator
    and gates the step on their answer. No structured result, no
    comparison -- the human is the oracle.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["confirm"]
    prompt: str


class OperationCallVerify(BaseModel):
    """MEHO dispatches the verify call and matches the result against ``expect``.

    The match is structural-equality + presence only: every key/value in
    :attr:`expect` must be present and equal in the call result. There are
    deliberately no operators, no JSONPath, and no boolean composition --
    substrate minimalism (same call as #1177): determinism over
    expressivity. :attr:`params` may carry ``${...}`` substitutions
    (allowlisted at publish time); :attr:`expect` may too.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["operation_call"]
    op_id: str
    params: dict[str, object]
    expect: dict[str, object]


#: A step's verify gate -- discriminated on ``type``. Pydantic routes a
#: payload to :class:`ConfirmVerify` or :class:`OperationCallVerify` by
#: the ``type`` tag and surfaces an unknown tag as a clean validation
#: error (no silent fall-through to the first union member).
Verify = Annotated[
    ConfirmVerify | OperationCallVerify,
    Field(discriminator="type"),
]


class OperationCallStep(BaseModel):
    """A step the agent dispatches via the operation registry.

    :attr:`op_id` is the registry operation id (e.g.
    ``vmware.composite.vm.create``); :attr:`params` is the call payload,
    which may contain ``${...}`` substitutions resolved at advance time.
    :attr:`body` is operator-readable Markdown context (may also contain
    substitutions). :attr:`verify` gates advance to the next step.
    """

    model_config = ConfigDict(frozen=True)

    id: Annotated[str, Field(pattern=STEP_ID_PATTERN.pattern)]
    title: str
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: Literal["operation_call"]
    op_id: str
    params: dict[str, object]
    verify: Verify


class ManualStep(BaseModel):
    """A step the operator performs off-MEHO (SSH, web UI, console).

    Carries no operation call -- :attr:`body` is the operator-readable
    instruction (Markdown; may contain ``${...}`` substitutions) and
    :attr:`verify` gates advance once the operator reports the step done.
    """

    model_config = ConfigDict(frozen=True)

    id: Annotated[str, Field(pattern=STEP_ID_PATTERN.pattern)]
    title: str
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: Literal["manual"]
    verify: Verify


#: One ordered step in a runbook -- discriminated on ``type``. The
#: ``type`` tag routes a payload to :class:`OperationCallStep` or
#: :class:`ManualStep`; an unknown tag is a validation error.
Step = Annotated[
    OperationCallStep | ManualStep,
    Field(discriminator="type"),
]


class RunbookTemplateBody(BaseModel):
    """The author-facing template shape stored in ``runbook_templates.steps``.

    This is what a template author writes and what the G12.2 service
    layer (T2) serialises into the ``steps`` JSONB column (alongside the
    ``title`` / ``description`` / ``target_kind`` columns it lifts out).
    :attr:`steps` is ordered; each step's verify gates advance to the
    next at run time.

    The :meth:`_validate_step_ids_unique_and_substitutions_allowlisted`
    validator enforces the two template-level invariants the per-step
    models cannot see on their own: step-id uniqueness across the
    template, and the ``${...}`` substitution allowlist over every
    string the template carries.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    description: str
    target_kind: str | None = None
    steps: list[Step]

    @model_validator(mode="after")
    def _validate_step_ids_unique_and_substitutions_allowlisted(
        self,
    ) -> RunbookTemplateBody:
        """Enforce unique step ids + allowlisted substitutions across the body.

        Raised as :class:`ValueError` so Pydantic renders it as a 422 at
        the HTTP boundary (matching the
        :class:`~meho_backplane.scheduler.schemas.ScheduledTriggerCreate`
        posture). Two invariants:

        1. **Step ids are unique** within the template -- a duplicate id
           would make run-time step addressing (G12.3) ambiguous.
        2. **Substitutions are allowlisted** -- every step body, op-call
           params, verify params, and verify expect is walked recursively
           and any ``${...}`` pattern other than ``${run.target}`` /
           ``${run.params.X}`` is rejected (defense in depth; G12.3
           re-checks at advance time via :func:`validate_substitutions`).
           Step / verify ``op_id`` is held to the stricter
           :func:`validate_op_id_static` rule -- operation identity is
           static, so *no* substitution (not even an allowlisted one) may
           appear there.
        """
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id!r}")
            seen.add(step.id)

            validate_substitutions(step.body)
            verify = step.verify
            if isinstance(step, OperationCallStep):
                validate_op_id_static(step.op_id)
                validate_substitutions(step.params)
            if isinstance(verify, OperationCallVerify):
                validate_op_id_static(verify.op_id)
                validate_substitutions(verify.params)
                validate_substitutions(verify.expect)
        return self


class ForkInfo(BaseModel):
    """Surfaced by ``meho.runbook.edit_template`` when editing a published template forks.

    Editing a *published* template cannot mutate it in place (published
    templates are immutable); the edit forks a new draft instead. This
    shape tells the senior what they are forking from -- the source
    version and how many runs are still pinned to it -- so they can decide
    whether the fork is the right move.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    in_flight_run_count: int


class DraftTemplateRequest(BaseModel):
    """Request body for ``meho.runbook.draft_template`` -- create a new draft.

    :attr:`slug` is validated against :data:`SLUG_PATTERN` (the kb slug
    contract, reused verbatim).
    """

    model_config = ConfigDict(frozen=True)

    slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern)]
    body: RunbookTemplateBody


class DraftTemplateResponse(BaseModel):
    """Response for ``meho.runbook.draft_template`` -- the created draft's coordinates."""

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    status: Literal["draft"]


class EditTemplateRequest(BaseModel):
    """Request body for ``meho.runbook.edit_template`` -- edit a draft or fork a publish."""

    model_config = ConfigDict(frozen=True)

    slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern)]
    body: RunbookTemplateBody


class EditTemplateResponse(BaseModel):
    """Response for ``meho.runbook.edit_template``.

    :attr:`version` equals the input version when editing a draft in
    place; it is a new version when forking from a published template, in
    which case :attr:`forked_from` is populated with the source's
    :class:`ForkInfo`. On the draft-edit path :attr:`forked_from` is
    ``None``.

    :attr:`status` is normally ``"draft"`` (both the in-place edit and the
    fork mint/keep a draft). The one exception is the **no-op fork skip**
    (#144): editing a published/deprecated version with a body byte-identical
    to the source creates no draft and returns the *unchanged source* — so
    ``version`` is the source version, ``forked_from`` is ``None``, and
    ``status`` is the source's own ``"published"`` / ``"deprecated"``. The
    non-``"draft"`` status is the signal that no new draft was created.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    status: Literal["draft", "published", "deprecated"]
    forked_from: ForkInfo | None = None


class PublishTemplateRequest(BaseModel):
    """Request body for ``meho.runbook.publish_template`` -- promote a draft to published."""

    model_config = ConfigDict(frozen=True)

    slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern)]
    version: int


class PublishTemplateResponse(BaseModel):
    """Response for ``meho.runbook.publish_template`` -- the now-published coordinates."""

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    status: Literal["published"]


class DeprecateTemplateRequest(BaseModel):
    """Request body for ``meho.runbook.deprecate_template`` -- retire a published version."""

    model_config = ConfigDict(frozen=True)

    slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern)]
    version: int


class DeprecateTemplateResponse(BaseModel):
    """Response for ``meho.runbook.deprecate_template`` -- the now-deprecated coordinates."""

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    status: Literal["deprecated"]


class DiscardTemplateRequest(BaseModel):
    """Request body for ``meho.runbook.discard_template`` -- delete an unpublished draft."""

    model_config = ConfigDict(frozen=True)

    slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern)]
    version: int


class DiscardTemplateResponse(BaseModel):
    """Response for ``meho.runbook.discard_template`` -- the discarded draft's coordinates.

    ``status`` is the synthetic terminal marker ``"discarded"`` -- unlike
    the other lifecycle verbs it is **not** a stored
    :class:`~meho_backplane.db.models.RunbookTemplate` status (the row is
    deleted, not transitioned), it signals to the caller that the draft
    was removed. Only unpublished drafts are discardable; published /
    deprecated versions are retired via ``deprecate`` (preserving history),
    never discarded.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    status: Literal["discarded"]


class ListTemplatesFilter(BaseModel):
    """Optional filters for ``meho.runbook.list_templates``.

    Both fields default to ``None`` (no filter). A bare
    :class:`ListTemplatesFilter` lists every template the caller can see.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["draft", "published", "deprecated"] | None = None
    target_kind: str | None = None


class TemplateSummary(BaseModel):
    """Operator-readable summary row surfaced by ``meho.runbook.list_templates``.

    The list-view projection -- enough to identify a template and its
    lifecycle state without loading the full step list (which
    :class:`ShowTemplateResponse` carries).
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    title: str
    status: Literal["draft", "published", "deprecated"]
    target_kind: str | None
    edited_at: datetime


class ShowTemplateResponse(BaseModel):
    """Full template surface returned by ``meho.runbook.show_template``.

    The complete template including the ordered :attr:`steps` and the
    authorship / timestamp provenance. Mirrors the
    :class:`~meho_backplane.db.models.RunbookTemplate` column set projected
    to wire types.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    version: int
    title: str
    description: str
    target_kind: str | None
    status: Literal["draft", "published", "deprecated"]
    steps: list[Step]
    created_by: str
    created_at: datetime
    edited_by: str
    edited_at: datetime
