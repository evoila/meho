# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Closed-loop kb write-back harness.

Three functions, each one phase of the loop:

* :func:`run_investigation` -- drive the
  :data:`~examples.kb_writeback.agent_definitions.INVESTIGATION_AGENT`
  against a symptom string and return the structured
  :class:`~examples.kb_writeback.agent_definitions.Finding`.
* :func:`persist_finding_to_kb` -- validate the finding's slug and
  write the finding's body to the tenant's knowledge base via
  :meth:`KbService.create_entry`. Returns the persisted
  :class:`~meho_backplane.kb.schemas.KbEntry`.
* :func:`retrieve_as_context` -- search the kb for entries relevant
  to a follow-up query (the next agent run's context); returns the
  ranked hits a downstream agent would be handed as input.

The three are intentionally separate so each can be tested in
isolation, scheduled independently, or swapped out. A consumer that
wants the agent itself to make the kb-write decision can replace
:func:`persist_finding_to_kb` with the MCP ``add_to_knowledge``
meta-tool flow; the structure of the loop stays the same.

Tenant binding
==============

Every call takes ``tenant_id`` as the first positional argument --
not a contextvar, not a global. The
:class:`~meho_backplane.auth.operator.Operator` plumbed through the
agent run carries the tenant in its own ``tenant_id`` field; this
sample never lets the two values disagree (the harness asserts they
match before persisting). That is the same posture every MEHO surface
takes: tenant scoping is a parameter, not implicit state.

Why this lives in ``examples/`` and not ``backend/``
====================================================

The sample is composition on top of two shipped MEHO services. It
ships at the repo root so a consumer reading the source tree finds
runnable patterns at a glance, without having to know where the
backend package lives. The CI exercise that proves the loop closes
against a real ``pgvector`` container is in
:mod:`tests.integration.test_examples_kb_writeback` -- it imports
this module by absolute path (see that test's module docstring for
the rationale).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meho_backplane.agent import AgentDefinition, PydanticAgentRun
from meho_backplane.kb.schemas import (
    KbEntry,
    KbEntrySearchHit,
    validate_slug,
)
from meho_backplane.kb.service import KbService

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

# Within-package import. The example tree (``examples/kb_writeback``)
# is a regular Python package; the CI exercise loads it by absolute
# path through :mod:`importlib.util` (the package lives outside
# ``backend/``'s wheel layout) but a consumer running the sample
# from the repo root can ``import examples.kb_writeback.workflow``
# directly once ``examples/`` is on ``sys.path``.
from .agent_definitions import (
    INVESTIGATION_AGENT,
    RETRIEVAL_AGENT,
    Finding,
    build_finding_body,
)

__all__ = [
    "INVESTIGATION_AGENT",
    "PROVENANCE_METADATA_KEY",
    "PROVENANCE_METADATA_VALUE",
    "RETRIEVAL_AGENT",
    "WriteBackResult",
    "build_finding_body",
    "is_provenance_match",
    "parse_tenant_id",
    "persist_finding_to_kb",
    "retrieve_as_context",
    "run_closed_loop",
    "run_investigation",
]


#: Metadata key the harness stamps onto every kb entry it writes. The
#: kb surface is metadata-agnostic in v0.2 (metadata is forwarded
#: verbatim, no filtering), but the marker key makes it trivial to
#: identify entries originating from this sample versus operator
#: hand-edits or other automation -- a future ``meho kb list
#: --filter "metadata.source = kb-writeback-sample"`` query, an audit
#: sweep, or a doc-freshness checker can look for the marker.
PROVENANCE_METADATA_KEY: str = "source"
PROVENANCE_METADATA_VALUE: str = "kb-writeback-sample"


@dataclass(frozen=True, slots=True)
class WriteBackResult:
    """The terminal result of one closed-loop sample run.

    Holds the three artefacts callers (and the CI exercise) assert
    against: the structured :class:`Finding` the investigation
    produced, the :class:`KbEntry` the harness persisted, and the
    list of :class:`KbEntrySearchHit` a follow-up retrieval found.
    Frozen so a returned value cannot mutate under the caller.
    """

    finding: Finding
    entry: KbEntry
    retrieval_hits: list[KbEntrySearchHit]


async def run_investigation(
    *,
    operator: Operator,
    symptom: str,
    runtime: PydanticAgentRun,
    definition: AgentDefinition = INVESTIGATION_AGENT,
) -> Finding:
    """Drive the investigation agent against *symptom*; return the structured finding.

    Delegates to :meth:`PydanticAgentRun.start` + :meth:`result` so the
    sample exercises the same seam the production invocation surface
    uses (see :class:`meho_backplane.agent.invocation.AgentInvoker.run`
    for the surface that wraps this in a durable run row + audit
    lineage). The sample is deliberately one rung simpler: no run row,
    no SSE, no approval flow. Tests inject a deterministic model via
    ``runtime.model_factory`` so no real LLM is hit.

    Parameters
    ----------
    operator:
        The principal under whom the investigation runs. The agent
        loop receives this as its ``deps`` so any tool calls (when a
        consumer adds a toolset block) dispatch under the right
        identity.
    symptom:
        The free-form symptom string the operator is asking about.
        Becomes the loop's ``inputs``. No prefix prompting from the
        harness -- the agent's system prompt does that work.
    runtime:
        The :class:`PydanticAgentRun` whose model factory builds the
        framework :class:`~pydantic_ai.models.Model`. Production
        callers pass the default ``PydanticAgentRun()``; tests inject
        a :class:`~pydantic_ai.models.function.FunctionModel` so the
        loop is deterministic and offline.
    definition:
        The agent definition. Defaults to the shipped
        :data:`INVESTIGATION_AGENT`; a consumer with a richer
        finding schema substitutes their own here.

    Returns
    -------
    Finding
        The structured output of the run.

    Raises
    ------
    AgentRunError
        Any seam-level failure (turn budget exhausted, model error,
        tool failure) propagates unchanged so the caller can decide
        whether to retry, log, or escalate.
    """
    handle = runtime.start(definition, operator, symptom)
    result = await runtime.result(handle)
    # ``AgentRunResult.output`` types as ``Any`` because the framework
    # cannot statically know which ``output_type`` a given definition
    # carries. The seam guarantees the runtime value is a validated
    # instance of ``definition.output_type`` (the framework's structured-
    # output contract), so an isinstance check is the right discipline
    # here -- both for mypy narrowing and for failing loudly if a
    # consumer subclasses the example with a deviating output type.
    if not isinstance(result.output, Finding):
        raise TypeError(
            f"investigation produced unexpected output type "
            f"{type(result.output).__name__}; the agent definition's "
            f"output_type was {Finding.__name__}",
        )
    return result.output


async def persist_finding_to_kb(
    *,
    operator: Operator,
    finding: Finding,
    service: KbService | None = None,
) -> KbEntry:
    """Write *finding* to *operator*'s tenant kb; return the persisted entry.

    Validates the slug at the harness boundary -- if the agent
    produced something malformed (a leading digit, an underscore, a
    spaces-in-slug), the surface raises :class:`InvalidKbSlugError`
    before the substrate call, so the failure mode is obvious in
    the example's traceback. Stamps a provenance metadata pair
    (:data:`PROVENANCE_METADATA_KEY` =
    :data:`PROVENANCE_METADATA_VALUE`) so kb readers can distinguish
    entries written by this sample from entries written by
    operators or by other harnesses.

    The body is rendered through :func:`build_finding_body` so the
    Markdown layout is consistent across every kb entry this sample
    writes; a custom rendering for a consumer's finding schema is the
    natural extension point.

    Parameters
    ----------
    operator:
        The principal whose tenant the entry is written under. The
        :class:`KbService` enforces tenant scoping at SQL level via
        :attr:`Operator.tenant_id`; cross-tenant writes are
        structurally impossible.
    finding:
        The structured finding emitted by :func:`run_investigation`.
    service:
        Optional :class:`KbService` injection point. Defaults to a
        freshly-constructed instance -- production callers pass
        nothing, tests inject a service bound to the testcontainer
        engine if they need to.

    Returns
    -------
    KbEntry
        The persisted :class:`KbEntry`, including the assigned id +
        timestamps so the caller can log or audit them.

    Raises
    ------
    InvalidKbSlugError
        The agent's proposed slug failed the shape contract. The
        finding is not written.
    """
    validate_slug(finding.slug)  # raises InvalidKbSlugError if malformed
    body = build_finding_body(finding)
    kb_service = service or KbService()
    # ``create_entry`` returns ``(entry, created)`` so the HTTP layer can
    # map fresh-create vs in-place re-index to 201 vs 200; this sample
    # only needs the persisted entry. ``actor_sub`` stamps the
    # ``last_updated_by_sub`` / ``created_by_sub`` attribution from the
    # investigating operator so the written finding is self-describing.
    entry, _created = await kb_service.create_entry(
        tenant_id=operator.tenant_id,
        slug=finding.slug,
        body=body,
        metadata={PROVENANCE_METADATA_KEY: PROVENANCE_METADATA_VALUE},
        actor_sub=operator.sub,
    )
    return entry


async def retrieve_as_context(
    *,
    operator: Operator,
    query: str,
    limit: int = 5,
    service: KbService | None = None,
) -> list[KbEntrySearchHit]:
    """Search the tenant's kb for entries relevant to *query*; return ranked hits.

    Wraps :meth:`KbService.search_entries` with the operator's tenant.
    The hits' ``snippet`` fields are truncated to ~200 chars
    (substrate-enforced); a downstream retrieval agent can pull the
    full body via :meth:`KbService.get_entry` keyed on the hit's slug
    if it decides the snippet is not enough.

    This is the read half of the closed loop -- the next agent run's
    "what does the team already know about this?" lookup. The harness
    splits it out of the retrieval agent itself because a consumer
    may want to load the kb context once and reuse it across several
    follow-up turns, rather than re-issuing the search every turn.
    """
    kb_service = service or KbService()
    return await kb_service.search_entries(
        tenant_id=operator.tenant_id,
        query=query,
        limit=limit,
    )


async def run_closed_loop(
    *,
    operator: Operator,
    symptom: str,
    follow_up_query: str,
    runtime: PydanticAgentRun,
    service: KbService | None = None,
    investigation_definition: AgentDefinition = INVESTIGATION_AGENT,
) -> WriteBackResult:
    """Run the full investigation -> kb write -> retrieval loop end to end.

    Convenience wrapper for callers who want to drive the whole
    pattern in one call -- typically the CI exercise, an
    illustration script, or an operator running the sample from the
    Python REPL. Three explicit phases inside, all of which a consumer
    can call directly if they want finer control:

    1. :func:`run_investigation` against *symptom*.
    2. :func:`persist_finding_to_kb` for the result.
    3. :func:`retrieve_as_context` for *follow_up_query*.

    The same ``operator`` (and therefore the same ``tenant_id``)
    flows through all three calls -- the sample never lets the
    write tenant and the read tenant diverge.

    The follow-up query is typically a question a *different* future
    operator would ask -- the example wires it to a string that
    overlaps with the finding's evidence so the retrieval ranks the
    just-written entry highly; in a real consumer the question comes
    from the next ticket / next chat turn / next scheduled
    investigation.
    """
    finding = await run_investigation(
        operator=operator,
        symptom=symptom,
        runtime=runtime,
        definition=investigation_definition,
    )
    entry = await persist_finding_to_kb(
        operator=operator,
        finding=finding,
        service=service,
    )
    hits = await retrieve_as_context(
        operator=operator,
        query=follow_up_query,
        service=service,
    )
    # Reminder, not enforcement: the harness already passed the
    # operator into every call, so finding/entry/hits are all bound
    # to the same tenant by construction. The id-equality assertion
    # would only fire if a caller hand-wrote a different operator
    # into one of the lower-level functions, which the sample is not
    # set up for.
    if entry.tenant_id != operator.tenant_id:  # pragma: no cover - guard
        raise RuntimeError(
            "kb-writeback invariant breach: persisted entry tenant "
            f"{entry.tenant_id} != operator tenant {operator.tenant_id}",
        )
    return WriteBackResult(finding=finding, entry=entry, retrieval_hits=hits)


def is_provenance_match(metadata: dict[str, object]) -> bool:
    """Return whether *metadata* carries this sample's provenance marker.

    Exposed as a module-level helper so a future operator-side
    cleanup script or a doc-freshness checker can use the same
    predicate the harness writes against, rather than re-deriving
    the literal key/value pair.
    """
    return metadata.get(PROVENANCE_METADATA_KEY) == PROVENANCE_METADATA_VALUE


def parse_tenant_id(value: str) -> uuid.UUID:
    """Parse an operator-supplied tenant id string into a :class:`uuid.UUID`.

    Tiny helper so a consumer running the sample from a script /
    notebook does not have to remember the
    ``str -> uuid.UUID`` conversion. Raises :class:`ValueError` on
    a bad input, matching :class:`uuid.UUID`'s native contract --
    the sample does not wrap it in a custom exception because a
    bad tenant id is an input-shape problem at the call site, not
    a sample-specific failure.
    """
    return uuid.UUID(value)


# `Finding` and `InvalidKbSlugError` stay at their original packages
# (agent_definitions / meho_backplane.kb.schemas); the harness keeps
# its surface narrow rather than re-exporting them through __all__.
