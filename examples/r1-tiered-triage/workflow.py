# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""R1 tiered-triage harness -- the runnable closed-loop wiring.

Initiative #807 R1 (Task #1084). The pattern:

1. **Cheap tier on a schedule.** Reads the latest broadcast events
   and the tenant's accumulated triage policy (memory scope ``tenant``
   with slug prefix ``r1-policy-``). For each event, decides skip or
   escalate.
2. **Escalate via ``invoke_agent``.** When the cheap tier picks an
   event, it calls the ``invoke_agent`` meta-tool against the deep
   tier's name. The shared usage budget (G11.1-T5 #812) caps the
   whole cascade.
3. **Deep tier investigates.** Returns a structured
   :class:`PolicyDecision` (validated against the ``output_schema``
   on :file:`agent.deep-tier-investigator.json`).
4. **Harness persists the policy to memory.** The harness wraps the
   :class:`~meho_backplane.agent.invoke.ChildRunner` so that every
   deep-tier return value is written back to memory as
   ``r1-policy-<alert_class>`` before control returns to the cheap
   tier. That closes the loop: the next firing of the cheap tier
   reads the new policy entry and short-circuits re-triage of the
   same class of event.

Why the policy write lives in the harness, not the deep agent's loop
====================================================================

The deep agent emits a structured :class:`PolicyDecision` -- a typed
value the harness can persist deterministically. If the agent itself
called ``add_to_memory``, the model would be responsible for picking
the slug, formatting the body, and ensuring idempotence on re-runs --
three extra failure modes that have nothing to do with the
investigation. Pulling the write into the harness keeps the agent
prompt narrow ("produce a decision") and the persistence boundary
auditable in plain Python.

The same pattern is used by R3 (``examples/kb_writeback``): the
investigation agent emits a structured ``Finding``; the harness writes
it to the kb. R1 follows the same shape against the memory layer (G5).

Why ``invoke_agent`` and not a separate harness-driven deep call
=================================================================

The cheap tier's prompt is built around the escalation tool. Driving
the deep tier from outside the cheap loop would mean teaching the
cheap tier to *signal* escalation via its structured output, then
parsing that output in Python -- a less faithful reproduction of the
production wiring. ``invoke_agent`` is the runtime's actual
composition surface; the harness uses it so the test exercises the
real ``invoke_agent`` -> ``ChildRunner`` -> child loop -> finalize
path. The wrapped finalizer is the harness's only addition.

Identity inheritance
====================

The child agent (deep tier) runs under the parent's identity --
:class:`~meho_backplane.agent.invoke.make_invoke_agent_tool`'s
``operator = ctx.deps`` line is unconditional. Lineage is recorded
via :data:`~meho_backplane.agent.invoke.current_agent_run_id_var`
(``parent_run_id`` on the child's ``agent_run`` row). The escalate
edge therefore carries the cheap tier's RBAC across to the deep
tier; if a tenant wants the deep tier to hold *additional* grants
the cheap tier does not, those are wired on the deep agent's
*principal* (see :file:`permissions.json`) -- the principal-side
grants compose with the inherited operator's role.

Run shape
=========

The harness is async. Callers either drive it directly from another
async function (CLI verb, integration test, an in-process scheduler
firing) or wrap it in :func:`asyncio.run` at the script edge. The
public entry point :func:`run_closed_loop` returns a structured
:class:`TriageRunResult` so the caller can inspect what happened
without re-querying memory / the audit log.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.agent import AgentDefinition, PydanticAgentRun
from meho_backplane.agent.invoke import (
    ChildAgentResolver,
    ChildRunFinalizer,
    ChildRunRecorder,
)
from meho_backplane.memory.schemas import MemoryEntry, MemoryScope
from meho_backplane.memory.service import MemoryService

if TYPE_CHECKING:  # pragma: no cover -- typing-only imports
    from meho_backplane.auth.operator import Operator

__all__ = [
    "POLICY_SLUG_PREFIX",
    "POLICY_SLUG_RE",
    "BroadcastEvent",
    "PolicyDecision",
    "PolicyWriteBack",
    "TriageRunResult",
    "build_cheap_tier_input",
    "load_known_policy",
    "load_runnable_definitions",
    "make_policy_persisting_hooks",
    "persist_policy_decision",
    "policy_slug_for_class",
    "run_closed_loop",
]


#: Slug prefix the harness uses for every policy entry it writes back
#: to memory. The cheap tier's prompt asks it to look up
#: ``r1-policy-<alert_class>``; this is the constant the harness
#: writes the same string against, so the two halves of the loop
#: cannot drift on the slug shape.
POLICY_SLUG_PREFIX: str = "r1-policy-"

#: Pattern an ``alert_class`` slug must match before the harness will
#: turn it into a policy slug. Mirrors the kebab-case constraint on
#: the deep-tier agent's ``output_schema.alert_class`` pattern in
#: :file:`agent.deep-tier-investigator.json`. Defends in depth against
#: a misconfigured override that bypasses the JSON schema.
_ALERT_CLASS_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9.-]*$")

#: Pattern :func:`policy_slug_for_class` produces / :func:`load_known_policy`
#: filters on. The harness asserts each slug parses against this so a
#: future policy-write that bypasses :func:`policy_slug_for_class`
#: (e.g. operator-authored memory entries) surfaces as a structured
#: mismatch rather than a silent retrieval miss.
POLICY_SLUG_RE: re.Pattern[str] = re.compile(rf"^{re.escape(POLICY_SLUG_PREFIX)}[a-z][a-z0-9.-]*$")


#: Path the loader resolves agent-definition JSON files against. The
#: shared definitions ship adjacent to this module so the harness and
#: the operator-facing ``meho agent create`` flow read the same
#: bytes -- a definition change lands in both consumers atomically.
_EXAMPLE_DIR: Path = Path(__file__).resolve().parent


class BroadcastEvent(BaseModel):
    """One event the cheap tier classifies.

    A trimmed view of the broadcast feed -- enough fields to drive
    the cheap-tier interestingness rules without committing to the
    full broadcast payload shape (which the broadcast subsystem owns
    and may extend). Frozen so a harness caller can stash an instance
    in audit / log records without mutation surprises.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    alert_class: str = Field(
        min_length=1,
        description=(
            "Short kebab-case class slug the cheap tier groups events under "
            "for policy lookup (e.g. 'vault-token-mint-prod')."
        ),
    )
    timestamp: str = Field(min_length=1)
    symptom: str = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    """Structured output of one deep-tier investigation run.

    Mirrors the ``output_schema`` block on
    :file:`agent.deep-tier-investigator.json`. The duplication is
    deliberate: the JSON schema is what the framework validates the
    model's response against; this Pydantic class is what Python
    callers (the harness, the tests, a consumer's own surface) build
    and read against. Both move in lock-step; the test suite asserts
    the two are equivalent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_class: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9.-]*$")
    verdict: str = Field(pattern=r"^(benign|acknowledged|actionable)$")
    re_escalate: bool
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyWriteBack:
    """Record of one harness-issued memory write for a deep-tier decision.

    The harness returns one of these per persisted policy so a caller
    can audit the closed-loop step without re-querying memory. ``slug``
    is the canonical :data:`POLICY_SLUG_PREFIX` + alert_class
    composition; ``entry`` is the :class:`MemoryEntry` the substrate
    returned (so the caller has the id + timestamps without a
    separate read).
    """

    slug: str
    entry: MemoryEntry
    decision: PolicyDecision


@dataclass(slots=True)
class TriageRunResult:
    """The structured return of one :func:`run_closed_loop` invocation.

    Carries every piece of evidence a caller needs to audit the
    closed loop without re-running the agent or re-querying memory:

    * :attr:`cheap_tier_output` -- the cheap tier's final text answer.
      The agent's prompt produces a terse one-line-per-event list.
    * :attr:`policy_entries_at_start` -- the policy memory entries the
      harness loaded before the cheap-tier run. Snapshot, not live --
      the cheap tier sees this list, not the post-run state.
    * :attr:`escalations_observed` -- the deep-tier briefings the
      cheap tier produced (one per ``invoke_agent`` call captured by
      the wrapped child runner).
    * :attr:`policy_write_backs` -- one entry per persisted policy.
      In the happy path ``len(policy_write_backs) == len(escalations_observed)``;
      a deep-tier run that emitted a non-:class:`PolicyDecision`
      output (test stub, schema mismatch) records the escalation but
      skips the write.
    """

    cheap_tier_output: str
    policy_entries_at_start: list[MemoryEntry]
    escalations_observed: list[str] = field(default_factory=list)
    policy_write_backs: list[PolicyWriteBack] = field(default_factory=list)


def policy_slug_for_class(alert_class: str) -> str:
    """Compose the canonical policy slug for an ``alert_class``.

    Raises :class:`ValueError` when ``alert_class`` does not match
    the kebab-case shape the deep-tier output schema requires. The
    harness asserts every slug it writes round-trips through
    :data:`POLICY_SLUG_RE`, so a bad ``alert_class`` is caught here
    rather than landing as a memory row the cheap tier cannot find.
    """
    if not _ALERT_CLASS_RE.fullmatch(alert_class):
        raise ValueError(
            f"alert_class {alert_class!r} does not match the kebab-case "
            f"shape '[a-z][a-z0-9.-]*'; harness refuses to persist the policy"
        )
    return f"{POLICY_SLUG_PREFIX}{alert_class}"


def _load_definition_json(filename: str) -> dict[str, Any]:
    """Read one agent-definition JSON file from the example directory.

    Helper for :func:`load_runnable_definitions`. Kept private because
    the public entry point returns built :class:`AgentDefinition`
    instances; raw dicts are an implementation detail. A missing file
    raises :class:`FileNotFoundError` directly -- the harness has no
    sensible fallback (the JSON is the contract).
    """
    parsed: dict[str, Any] = json.loads((_EXAMPLE_DIR / filename).read_text(encoding="utf-8"))
    return parsed


def load_runnable_definitions() -> tuple[AgentDefinition, AgentDefinition]:
    """Build :class:`AgentDefinition` instances from the shipped JSON files.

    Returns the ``(cheap, deep)`` pair. The toolset / tier / turn-
    budget fields come straight from the JSON so a single source of
    truth holds for both ``meho agent create`` consumers and the
    harness. ``output_type`` on the deep tier is set to
    :class:`PolicyDecision` so the runtime constrains the loop's
    final answer to a validated instance (this is the structured-
    output contract :class:`AgentDefinition` exposes).
    """
    cheap_raw = _load_definition_json("agent.cheap-tier-classifier.json")
    deep_raw = _load_definition_json("agent.deep-tier-investigator.json")

    # AgentDefinition (the runtime class) takes ``request_limit`` /
    # ``output_type`` rather than the JSON's ``turn_budget`` /
    # ``output_schema`` keys (which are the wire shape the
    # ``AgentDefinitionCreate`` Pydantic schema accepts). The harness
    # bridges between the two so the JSON stays the canonical deploy
    # artefact -- a future change there does not need a parallel
    # update inside this function.
    cheap = AgentDefinition(
        name=cheap_raw["name"],
        system_prompt=cheap_raw["system_prompt"],
        request_limit=cheap_raw["turn_budget"],
        toolset=cheap_raw.get("toolset"),
    )
    deep = AgentDefinition(
        name=deep_raw["name"],
        system_prompt=deep_raw["system_prompt"],
        request_limit=deep_raw["turn_budget"],
        toolset=deep_raw.get("toolset"),
        output_type=PolicyDecision,
    )
    return cheap, deep


async def load_known_policy(
    *,
    operator: Operator,
    memory: MemoryService | None = None,
) -> list[MemoryEntry]:
    """Return every ``r1-policy-*`` entry in the operator's tenant.

    Reads memory scope ``tenant`` and filters by the
    :data:`POLICY_SLUG_PREFIX` prefix in the slug -- :meth:`MemoryService.list_memories`
    supports an explicit ``slug_pattern`` substring filter that the
    service evaluates server-side, so the call returns only the
    relevant rows without a Python-side scan.

    The cheap-tier harness calls this once per firing before
    building the cheap-tier input. A future optimisation could cache
    the result per scheduler tick; v0.2 ships the simple read-on-
    every-firing shape because memory corpora per tenant are small
    (consumer-needs.md L131 names ~15 files / tenant).
    """
    service = memory if memory is not None else MemoryService()
    entries: list[MemoryEntry] = await service.list_memories(
        operator,
        scope=MemoryScope.TENANT,
        slug_pattern=POLICY_SLUG_PREFIX,
        # 200 is well above the realistic alert-class count for any
        # tenant in v0.2; if a tenant accumulates >200 alert classes
        # the cheap tier's prompt no longer fits anyway, and that's
        # the operator's signal to prune.
        limit=200,
    )
    return entries


def build_cheap_tier_input(
    *,
    known_policy: list[MemoryEntry],
    recent_events: list[BroadcastEvent],
) -> str:
    """Assemble the cheap tier's loop input string.

    The cheap-tier prompt expects two clearly-labelled sections in
    the input:

    * ``## Known policy`` -- the policy entries the deep tier has
      written. Each rendered as ``- slug: <slug>\\n  body: <body>``.
    * ``## Recent events`` -- the broadcast events the cheap tier
      classifies. Each rendered as a JSON line so the agent can
      parse the fields without doing string surgery on prose.

    Section headings are part of the prompt's contract (the cheap
    tier's system prompt names them). Changing the headings here
    requires updating the prompt in
    :file:`agent.cheap-tier-classifier.json` and the assertion in
    :mod:`tests.test_examples_r1_tiered_triage`.
    """
    parts: list[str] = ["## Known policy", ""]
    if not known_policy:
        parts.append("(none -- this is a fresh tenant; every interesting event will escalate)")
    else:
        for entry in known_policy:
            parts.append(f"- slug: {entry.slug}")
            # Indent the body two spaces so a prose body with its own
            # markdown headings does not collide with the top-level
            # ``## Recent events`` heading downstream.
            for line in entry.body.splitlines():
                parts.append(f"  {line}")
            parts.append("")
    parts.append("## Recent events")
    parts.append("")
    if not recent_events:
        parts.append("(none -- nothing to triage this firing)")
    else:
        for event in recent_events:
            parts.append(event.model_dump_json())
    return "\n".join(parts)


def make_policy_persisting_hooks(
    *,
    operator: Operator,
    memory: MemoryService | None = None,
    sink: list[PolicyWriteBack] | None = None,
    escalations_observed: list[str] | None = None,
) -> tuple[ChildRunRecorder, ChildRunFinalizer]:
    """Return ``(recorder, finalizer)`` hooks for the closed-loop run.

    The hooks plug into
    :class:`~meho_backplane.agent.invoke.make_invoke_agent_tool`'s
    ``recorder`` + ``finalizer`` slots. The runtime call shape is:

    1. The cheap tier's loop calls ``invoke_agent(agent_name, inputs)``.
    2. ``make_invoke_agent_tool``'s tool body:
       - Checks the depth bound.
       - Resolves the child definition.
       - **Calls the recorder** with the parent operator + the
         resolved child definition + the parent run id. Returns a
         child run id (a synthetic UUID in the harness; a durable
         row in the production invocation surface).
       - Drives the child loop via the runtime's :meth:`run_child`
         (sharing the parent's usage budget).
       - **Calls the finalizer** with the child run id + the loop's
         raw output. The harness's finalizer is where the
         :class:`PolicyDecision` -> memory write lives.

    A child loop that *fails* (turn budget exhausted, tool error,
    model error) still flows through the finalizer with
    ``output=None`` + ``error=<message>``; the harness skips the
    policy write in that case (no decision to persist).

    Why the recorder + finalizer pair and not the child runner:
    ``make_invoke_agent_tool`` captures ``child_runner`` at tool-
    build time, so a post-hoc reassignment of ``runtime.run_child``
    has no effect. The recorder/finalizer hooks are the runtime's
    documented extension points and the production invocation
    surface (T4 #811 / T6 #813) targets the same API; using them
    keeps the harness composing against the real seam.
    """
    service = memory if memory is not None else MemoryService()

    async def _record(
        *,
        operator: Operator,
        definition: AgentDefinition,
        parent_run_id: uuid.UUID | None,
    ) -> uuid.UUID:
        # Synthetic id -- the harness has no DB row to point at. A
        # production invocation surface would create a real
        # ``agent_run`` row here and return its id; the harness gets
        # away with a fresh UUID because the only consumer of the id
        # is the finalizer, which keys on the output, not on the row.
        import uuid as _uuid_mod

        if escalations_observed is not None:
            # Record the child definition's name as the escalation
            # marker. The cheap tier's prompt names exactly one valid
            # escalation target (the deep tier); seeing N entries here
            # means N events the cheap tier classified as interesting.
            escalations_observed.append(definition.name)
        return _uuid_mod.uuid4()

    async def _finalize(
        run_id: uuid.UUID,
        *,
        output: Any,
        error: str | None,
    ) -> None:
        if error is not None:
            # A failed child loop has no PolicyDecision to persist.
            # The cheap tier's loop already sees the failure as a
            # ModelRetry; the harness records nothing further.
            return
        if not isinstance(output, PolicyDecision):
            # A non-PolicyDecision output means the deep tier's
            # output_schema validation gave up (a test FunctionModel
            # returning a plain string, a future schema drift). The
            # cheap tier still gets the raw output back from
            # ``invoke_agent``; the harness skips the write.
            return
        write_back = await persist_policy_decision(
            operator=operator,
            decision=output,
            memory=service,
        )
        if sink is not None:
            sink.append(write_back)

    return _record, _finalize


async def persist_policy_decision(
    *,
    operator: Operator,
    decision: PolicyDecision,
    memory: MemoryService | None = None,
) -> PolicyWriteBack:
    """Write *decision* to memory as ``r1-policy-<alert_class>``.

    Idempotent on slug: the memory substrate's
    :meth:`MemoryService.remember` upserts on the
    ``(scope, slug)`` key, so re-running an investigation on the
    same alert class overwrites the previous entry rather than
    spawning a duplicate. The body is rendered Markdown the cheap
    tier reads as-is in the next firing.
    """
    service = memory if memory is not None else MemoryService()
    slug = policy_slug_for_class(decision.alert_class)
    body = _render_policy_body(decision)
    entry = await service.remember(
        operator,
        MemoryScope.TENANT,
        body,
        slug=slug,
        # Stamp the harness-issued provenance so a future audit /
        # promote sweep can distinguish R1-written policy entries
        # from operator hand-edits or other automation.
        metadata={
            "source": "r1-tiered-triage",
            "verdict": decision.verdict,
            "re_escalate": decision.re_escalate,
        },
    )
    return PolicyWriteBack(slug=slug, entry=entry, decision=decision)


def _render_policy_body(decision: PolicyDecision) -> str:
    """Render a :class:`PolicyDecision` into the memory body string.

    The cheap tier reads this body verbatim when it loads the policy
    on the next firing. Plain Markdown with three fixed sections so
    the cheap tier's prompt can parse it deterministically:

    * Verdict + ``re_escalate`` (the load-bearing fields).
    * Summary (the operator-readable rationale).
    * Evidence (fenced so BM25 ranks the literal log lines).
    * Recommended action (only when ``actionable``).
    """
    parts: list[str] = []
    parts.append(f"verdict: {decision.verdict}")
    parts.append(f"re_escalate: {str(decision.re_escalate).lower()}")
    parts.append("")
    parts.append("## Summary")
    parts.append("")
    parts.append(decision.summary.rstrip())
    if decision.evidence:
        parts.append("")
        parts.append("## Evidence")
        parts.append("")
        for line in decision.evidence:
            parts.append(f"- `{line}`")
    if decision.verdict == "actionable" and decision.recommended_action:
        parts.append("")
        parts.append("## Recommended action")
        parts.append("")
        parts.append(decision.recommended_action.rstrip())
    parts.append("")
    return "\n".join(parts)


async def run_closed_loop(
    *,
    operator: Operator,
    recent_events: list[BroadcastEvent],
    runtime: PydanticAgentRun,
    memory: MemoryService | None = None,
    child_resolver: ChildAgentResolver | None = None,
) -> TriageRunResult:
    """Drive one closed-loop tick of the tiered-triage pattern.

    Public entry point. Composition order:

    1. Load known policy from memory (the deep-tier history the
       cheap tier short-circuits against).
    2. Build the cheap-tier input by concatenating the policy and
       the events.
    3. Resolve the cheap + deep agent definitions from the JSON
       files (single source of truth).
    4. Wire a child-agent resolver that returns the deep-tier
       definition for the well-known name, plus a wrapped child
       runner that persists deep-tier outputs to memory.
    5. Drive the cheap-tier loop. The ``invoke_agent`` tool
       (wrapped) handles escalation + policy write-back transparently.
    6. Return a :class:`TriageRunResult` with every artefact the
       caller needs to audit the run.

    Args:
        operator: The principal under whom both tiers run. The deep
            tier inherits this identity through
            :func:`~meho_backplane.agent.invoke.make_invoke_agent_tool`'s
            ``operator = ctx.deps`` line; principal-side grants on
            the deep tier's own Keycloak sub compose with this
            inherited operator.
        recent_events: The broadcast batch the cheap tier classifies.
            The caller is responsible for fetching this -- the
            production scheduler pulls it from the broadcast feed;
            tests construct it inline.
        runtime: The :class:`PydanticAgentRun` the harness drives.
            Wired with the JSON-loaded definitions, the
            :class:`ChildAgentResolver` constructed here, and the
            policy-persisting wrapped child runner.
        memory: Override the default :class:`MemoryService` for
            tests or for a consumer with a custom RBAC resolver.
        child_resolver: Override the default name-based resolver
            for tests that want to substitute the deep tier with
            a stub or a different investigator.

    Returns:
        A :class:`TriageRunResult` carrying the cheap-tier output,
        the policy snapshot the cheap tier saw, the escalations the
        cheap tier produced, and the policy entries the harness
        persisted.
    """
    service = memory if memory is not None else MemoryService()

    known_policy = await load_known_policy(operator=operator, memory=service)
    cheap_def, deep_def = load_runnable_definitions()
    inputs = build_cheap_tier_input(
        known_policy=known_policy,
        recent_events=recent_events,
    )

    if child_resolver is None:

        async def _name_resolver(
            _operator: Operator,
            agent_name: str,
        ) -> AgentDefinition | None:
            # Single-edge resolver: the cheap tier may escalate to
            # the deep tier and only the deep tier. Any other name
            # returns None and the framework surfaces a ModelRetry
            # the cheap tier can reason about ("no such agent ...").
            if agent_name == deep_def.name:
                return deep_def
            return None

        resolver: ChildAgentResolver = _name_resolver
    else:
        resolver = child_resolver

    escalations_observed: list[str] = []
    write_backs: list[PolicyWriteBack] = []

    # Build the runtime's recorder + finalizer hooks. The recorder
    # tees every escalation (one per cheap-tier ``invoke_agent``
    # call); the finalizer is where the PolicyDecision -> memory
    # write happens. See :func:`make_policy_persisting_hooks` for
    # the protocol shapes and why the hook pattern beats wrapping
    # ``run_child`` directly.
    recorder, finalizer = make_policy_persisting_hooks(
        operator=operator,
        memory=service,
        sink=write_backs,
        escalations_observed=escalations_observed,
    )
    # Compose a fresh runtime that re-uses the caller's model
    # factory / resolver wiring and installs the harness's
    # composition hooks alongside any the caller already wired. The
    # caller's hooks are deliberately *replaced* (not chained) so
    # the harness owns the closed-loop semantics for this tick; a
    # consumer that wants both shapes runs the harness twice with
    # different runtime configurations.
    composing_runtime = PydanticAgentRun(
        model_factory=runtime.model_factory,
        model_resolver=runtime.model_resolver,
        child_agent_resolver=resolver,
        child_run_recorder=recorder,
        child_run_finalizer=finalizer,
    )

    handle = composing_runtime.start(cheap_def, operator, inputs)
    result = await composing_runtime.result(handle)

    return TriageRunResult(
        cheap_tier_output=str(result.output),
        policy_entries_at_start=known_policy,
        escalations_observed=escalations_observed,
        policy_write_backs=write_backs,
    )
