# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI exercise for the R1 tiered-triage example
(:mod:`examples/r1-tiered-triage`).

The example ships as docs + runnable JSON payloads + a Python
harness under ``examples/r1-tiered-triage/`` (Initiative G11.6
#807 reference pattern R1, Task G11.6-T1 #1084). Three failure
modes the example must guard against:

1. **Schema drift.** The runnable JSON payloads
   (:file:`agent.cheap-tier-classifier.json`,
   :file:`agent.deep-tier-investigator.json`,
   :file:`scheduler.cron.json`) hard-code field names that mirror
   the live Pydantic schemas
   (:class:`meho_backplane.agents.schemas.AgentDefinitionCreate`,
   :class:`meho_backplane.scheduler.schemas.ScheduledTriggerCreate`).
   If the schemas rename / drop / retype a field, the example
   silently rots until an operator copy-pastes it and the backend
   422s. This test validates the JSON against the live schemas so
   the rot is loud at PR time.

2. **Link rot.** The two markdown docs (:file:`README.md`,
   :file:`GUIDE.md`) reference siblings in ``docs/``, ``backend/src/``,
   ``cli/``, and ``deploy/`` via relative paths. If a referenced
   file moves / renames, the example points at vapour. This test
   walks every relative link in those markdown files and asserts
   the target path exists on disk.

3. **Harness wiring drift.** The closed-loop harness (:mod:`workflow`)
   composes :class:`PydanticAgentRun`'s
   :attr:`child_agent_resolver` + :attr:`child_run_recorder` +
   :attr:`child_run_finalizer` slots. If the runtime's hook
   protocols evolve (signature, async-shape, return type), the
   harness breaks at run time -- a compile-clean failure. The
   end-to-end test drives one closed-loop tick against a
   deterministic :class:`pydantic_ai.models.function.FunctionModel`
   and asserts the loop closes (cheap tier escalates -> deep tier
   produces :class:`PolicyDecision` -> harness writes
   ``r1-policy-<class>`` to memory) without leaning on any external
   service.

The test is **runnable as a plain pytest** with no DB / network /
secrets -- it operates against the autouse
:func:`tests.conftest._default_database_url` fixture's
SQLite-backed engine and patches the embedding service the same
way :mod:`test_memory_service` does. The end-to-end smoke against
a live model + a production memory substrate would land as a
vacuous-skip in CI (per the slim skill's vacuous-skip rule); the
schema + link + in-process harness drive here are the
deterministic always-on gates.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Repo-root + example-dir resolution
# ---------------------------------------------------------------------------


def _repo_root(start: Path) -> Path:
    """Walk up from the test file to the repo root.

    The R1 example lives at ``examples/r1-tiered-triage/`` under
    the repo root; the test file lives at ``backend/tests/`` under
    the same root. Locating the root via a stable marker keeps the
    test working under worktrees / out-of-tree builds where
    ``__file__`` cannot be assumed to share a prefix with the
    example.
    """
    here = start.resolve()
    for parent in (here, *here.parents):
        if (parent / "examples" / "r1-tiered-triage" / "README.md").exists():
            return parent
    raise RuntimeError("could not find repo root containing examples/r1-tiered-triage/README.md")


REPO_ROOT: Path = _repo_root(Path(__file__))
EXAMPLE_DIR: Path = REPO_ROOT / "examples" / "r1-tiered-triage"

#: Regex for a markdown link target: extracts the path inside
#: ``](...)``. Anchored on the closing bracket so it doesn't match
#: image alt-text or HTML-style anchors. Includes both link and
#: image syntax.
_LINK_RE: re.Pattern[str] = re.compile(r"\]\((?P<target>[^)]+)\)")


def _load_workflow_module() -> Any:
    """Load the example's :mod:`workflow` module by absolute path.

    The example dir is kebab-cased (``r1-tiered-triage``), which is
    not a valid Python package name -- a plain ``import`` would
    fail. The test loads ``workflow.py`` via :mod:`importlib.util`
    so the harness exercise here runs against the exact source the
    operator ships, not a copy.
    """
    workflow_path = EXAMPLE_DIR / "workflow.py"
    spec = importlib.util.spec_from_file_location(
        "r1_tiered_triage_workflow",
        workflow_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load workflow.py spec from {workflow_path}")
    module = importlib.util.module_from_spec(spec)
    # Cache the module on sys.modules so re-loading inside other
    # tests (or after a worktree refresh) does not double-import the
    # PolicyDecision symbol -- the isinstance() check in the
    # harness's finalizer matches by class identity.
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Settings + fake embedding fixture (matches test_memory_service shape)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for this module.

    Mirrors the autouse fixture in :mod:`test_memory_service` so the
    harness's :class:`MemoryService` instantiation does not blow up
    on missing-required-env at import time.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Deterministic 384-dim placeholder embedding. Mirrors
#: :data:`test_memory_service._FAKE_EMBEDDING` -- the harness writes
#: a memory entry through :meth:`MemoryService.remember`, which
#: routes through :func:`meho_backplane.retrieval.indexer.index_document`
#: -> the embedding service. Patching the service with a fixed vector
#: avoids the fastembed cold-start cost without affecting any
#: assertion (we read back by ``(scope, slug)``, not by similarity).
_FAKE_EMBEDDING: list[float] = [0.01] * 384


@pytest.fixture
def _fake_embedding_service() -> Iterator[None]:
    """Patch the embedding singleton imported by the indexer."""
    fake = AsyncMock()
    fake.encode_one.return_value = _FAKE_EMBEDDING
    fake.encode_many.return_value = [_FAKE_EMBEDDING]
    fake.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        yield


def _make_operator(
    *,
    sub: str = "test-op-r1",
    tenant_id: uuid.UUID | None = None,
    role: TenantRole = TenantRole.TENANT_ADMIN,
) -> Operator:
    """Build an :class:`Operator` for the harness drive.

    Default role is :class:`TenantRole.TENANT_ADMIN` because the
    harness writes a ``TENANT``-scoped memory entry on every
    closed-loop tick -- the RBAC matrix denies ``TENANT`` writes
    from the lower ``OPERATOR`` role.
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id or uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=role,
    )


# ---------------------------------------------------------------------------
# Test 1 -- agent definition JSONs validate against the live schema
# ---------------------------------------------------------------------------


def test_cheap_tier_definition_payload_parses_against_live_schema() -> None:
    """:file:`agent.cheap-tier-classifier.json` validates against
    :class:`AgentDefinitionCreate`.

    Also pins the tier (``fast``) and toolset shape; the example's
    whole story is *"cheap tier on a schedule"*, and a future
    accidental tier bump would silently route the cheap tier through
    the deep-tier backend, blowing the cost cap.
    """
    payload = json.loads(
        (EXAMPLE_DIR / "agent.cheap-tier-classifier.json").read_text(encoding="utf-8"),
    )
    parsed = AgentDefinitionCreate.model_validate(payload)
    assert parsed.model_tier.value == "fast", (
        "R1 cheap tier must stay on the fast tier; see Initiative #807 R1 -- "
        "the pattern's name is 'cheap-tier on a schedule, escalating to deep'."
    )
    # The cheap tier's toolset is deliberately narrow: only the
    # call_operation meta-tool is registered; invoke_agent is
    # auto-appended by the runtime when child composition is wired.
    # An accidental broadening (e.g. opening list_operation_groups
    # / search_operations) would let the model do its own discovery
    # on every firing, which is a deep-tier shape.
    assert parsed.toolset.get("meta_tools") == ["call_operation"]


def test_deep_tier_definition_payload_parses_against_live_schema() -> None:
    """:file:`agent.deep-tier-investigator.json` validates against
    :class:`AgentDefinitionCreate`.

    Also pins:

    * ``model_tier == 'deep'`` -- the whole pairing's point.
    * ``output_schema`` present + shape: a deep-tier run without
      structured output cannot be persisted as policy, breaking the
      closed loop.
    """
    payload = json.loads(
        (EXAMPLE_DIR / "agent.deep-tier-investigator.json").read_text(encoding="utf-8"),
    )
    parsed = AgentDefinitionCreate.model_validate(payload)
    assert parsed.model_tier.value == "deep", (
        "R1 deep tier must stay on the deep tier; see Initiative #807 R1 -- "
        "an accidental tier drop would degrade the investigator silently."
    )
    assert parsed.output_schema is not None, (
        "R1 deep tier MUST carry an output_schema so the harness can "
        "validate the loop's final answer as a PolicyDecision before "
        "persisting it to memory."
    )
    # Pin the structural shape of the output schema so a future PR
    # cannot quietly drop a required field the harness's
    # PolicyDecision Pydantic class still reads.
    required = parsed.output_schema.get("required", [])
    assert set(required) == {"alert_class", "verdict", "re_escalate", "summary", "evidence"}


def test_scheduler_payload_parses_against_live_schema() -> None:
    """:file:`scheduler.cron.json` validates against
    :class:`ScheduledTriggerCreate` (with a placeholder UUID).

    Pins the cron expression (``*/15 * * * *``) and the
    ``identity_sub`` because the GUIDE walks the operator through
    both as load-bearing tuning knobs.
    """
    payload = json.loads(
        (EXAMPLE_DIR / "scheduler.cron.json").read_text(encoding="utf-8"),
    )
    # Pin the placeholder so a future edit doesn't replace it with
    # a real id by accident (the test would still parse, but the
    # operator's "substitute with your id" step in GUIDE would be
    # silently shipped).
    assert payload["agent_definition_id"] == "00000000-0000-0000-0000-000000000000"
    payload["agent_definition_id"] = str(uuid.uuid4())
    parsed = ScheduledTriggerCreate.model_validate(payload)
    assert parsed.kind.value == "cron"
    # The cron cadence is the GUIDE's documented baseline. A change
    # here without updating GUIDE Step 2's reasoning is silent drift.
    assert parsed.cron_expr == "*/15 * * * *"
    # The cheap tier impersonates its own principal when the
    # scheduler fires it. The identity_sub is the OIDC sub the
    # scheduler exchanges its service token for.
    assert parsed.identity_sub == "agent:r1-cheap-tier-classifier"
    # Defaults the operator should not change without considering
    # the at-least-once semantics in GUIDE.md.
    assert parsed.in_flight_policy.value == "fail_into_audit"


# ---------------------------------------------------------------------------
# Test 2 -- permissions JSON is structurally sound
# ---------------------------------------------------------------------------


def test_permissions_json_is_well_formed_and_carries_no_invoke_agent_grant() -> None:
    """:file:`permissions.json` is parseable + every row has UUID-or-``*`` ``target_scope``.

    The permission-grants surface (G11.2-T6 #819) accepts a
    ``{"permissions": [...]}`` body. We validate the structural
    shape here without round-tripping through the surface (which
    needs the API stack).

    Two contracts pinned, both load-bearing for the example's
    v0.2 accuracy:

    1. **``target_scope`` is UUID or ``*``** -- the API validator at
       ``backend/src/meho_backplane/api/v1/agent_grants.py``
       (``create_grant`` parses ``target_scope`` as a UUID and 422s
       anything else, except the literal ``*`` sentinel). A
       freeform ``"agent:<name>"`` string is rejected; documenting
       one in the example would land an operator on a copy-paste
       422 the first time they apply Step 3.

    2. **No ``meho.invoke_agent`` grant row.** v0.2 does NOT gate
       agent-to-agent dispatch on the grants table -- the
       ``invoke_agent`` meta-tool body at
       ``backend/src/meho_backplane/agent/invoke.py`` only checks
       the depth cap + name resolution via the harness's
       ``child_agent_resolver``. Shipping an ``invoke_agent`` row
       would either 422 (with the rejected
       ``target_scope="agent:..."`` shape) or mislead operators into
       thinking the row is what enables the escalate edge. The
       harness's ``_name_resolver`` in ``workflow.py`` is the real
       composition surface.
    """
    raw = json.loads((EXAMPLE_DIR / "permissions.json").read_text(encoding="utf-8"))
    assert "permissions" in raw, "permissions.json must wrap rows in a top-level 'permissions' key"
    rows = raw["permissions"]
    assert isinstance(rows, list)
    assert rows, "permissions.json must declare at least one grant"

    # Validate the row shape; each row must have these four keys at
    # minimum (the agent-grants API accepts more, but the example's
    # contract is the four core fields).
    required_keys = {"principal_sub", "op_pattern", "target_scope", "verdict"}
    for row in rows:
        missing = required_keys - row.keys()
        assert not missing, f"permission row missing required keys: {missing}; row={row}"

    # Contract 1: target_scope is UUID or "*". The principal_sub
    # placeholders carry their own non-UUID sentinels we substitute
    # at apply time -- target_scope is the field the API actually
    # parses as a UUID.
    for row in rows:
        scope = row["target_scope"]
        if scope == "*":
            continue
        try:
            uuid.UUID(scope)
        except ValueError as exc:
            raise AssertionError(
                f"target_scope must be a UUID or '*'; got {scope!r} on row {row} -- "
                "the agent-grants API 422s a freeform string. "
                "See backend/src/meho_backplane/api/v1/agent_grants.py."
            ) from exc

    # Contract 2: no meho.invoke_agent grant. v0.2's runtime does
    # not consult the grants table for agent-to-agent dispatch
    # (see backend/src/meho_backplane/agent/invoke.py); the
    # escalate edge is enabled by the harness's child_agent_resolver,
    # not by a grant row. Documenting one in the example would be
    # fiction.
    invoke_grants = [row for row in rows if row["op_pattern"] == "meho.invoke_agent"]
    assert not invoke_grants, (
        "permissions.json must NOT carry a meho.invoke_agent grant -- v0.2 "
        "does not gate agent-to-agent dispatch on grant rows; the escalate "
        "edge is enabled by the harness's child_agent_resolver in workflow.py. "
        f"Found unexpected row(s): {invoke_grants}"
    )


# ---------------------------------------------------------------------------
# Test 3 -- identity_budget_seed.py imports + the arg parser works
# ---------------------------------------------------------------------------


def test_identity_budget_seed_script_imports_and_parses_args() -> None:
    """:file:`identity_budget_seed.py` is importable and its CLI parser
    accepts the GUIDE's documented arguments.

    The script is the operator's only path to seeding the per-
    identity budget rows (no CLI verb exists in v0.2). If the
    script's argparse drifts away from the GUIDE's documented
    arguments, the operator's copy-paste of GUIDE Step 4 fails at
    run time. Pinning the parser's accepted args here catches the
    drift at PR time.
    """
    seed_path = EXAMPLE_DIR / "identity_budget_seed.py"
    spec = importlib.util.spec_from_file_location("r1_seed", seed_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The parse helper accepts the GUIDE-documented argv shape.
    ns = module._parse_args(
        [
            "--tenant-id",
            "11111111-1111-1111-1111-111111111111",
            "--cheap-sub",
            "agent:r1-cheap-tier-classifier",
            "--deep-sub",
            "agent:r1-deep-tier-investigator",
        ]
    )
    assert ns.tenant_id == uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert ns.cheap_sub == "agent:r1-cheap-tier-classifier"
    assert ns.deep_sub == "agent:r1-deep-tier-investigator"


# ---------------------------------------------------------------------------
# Test 4 -- the harness module loads + helpers behave correctly
# ---------------------------------------------------------------------------


def test_workflow_module_loads_and_exports_expected_symbols() -> None:
    """The harness module imports cleanly and exposes its documented surface.

    Catches the case where a refactor moves the harness symbols
    around without updating the README's `What's here` table -- the
    test reads the public list against the documented one.
    """
    workflow = _load_workflow_module()
    expected_exports = {
        "MAX_ESCALATIONS_PER_FIRING",
        "BroadcastEvent",
        "POLICY_SLUG_PREFIX",
        "POLICY_SLUG_RE",
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
    }
    assert set(workflow.__all__) == expected_exports
    # The slug prefix is part of the closed-loop contract -- the
    # cheap tier reads it; the harness writes it. Pin to detect
    # drift in either direction.
    assert workflow.POLICY_SLUG_PREFIX == "r1-policy-"
    # The cap value is part of the cheap-tier prompt's documented
    # behaviour and the workflow's code-side enforcement. Pin so a
    # drift between the prompt's "at most 5" line and the harness's
    # raise threshold surfaces here.
    assert workflow.MAX_ESCALATIONS_PER_FIRING == 5


def test_policy_slug_for_class_validates_kebab_case() -> None:
    """:func:`policy_slug_for_class` rejects non-kebab-case ``alert_class``.

    The deep tier's output schema constrains ``alert_class`` to
    ``^[a-z][a-z0-9.-]*$``; the harness validates the same shape on
    the persistence boundary so an output_schema bypass (e.g. a
    consumer-injected stub model) cannot land a policy entry the
    cheap tier's lookup cannot find.
    """
    workflow = _load_workflow_module()
    slug = workflow.policy_slug_for_class("vault-token-mint-prod")
    assert slug == "r1-policy-vault-token-mint-prod"
    with pytest.raises(ValueError, match="does not match the kebab-case"):
        workflow.policy_slug_for_class("UPPERCASE-bad")
    with pytest.raises(ValueError, match="does not match the kebab-case"):
        workflow.policy_slug_for_class("123-leading-digit-bad")


@pytest.mark.asyncio
async def test_escalation_cap_enforced_in_recorder_hook() -> None:
    """The recorder hook caps escalations and rejects further calls.

    The cheap-tier prompt documents "at most 5 escalations per
    firing" but a prompt-side directive is advisory -- a misaligned
    model can attempt more. The harness enforces the cap inside
    :func:`make_policy_persisting_hooks`'s recorder by raising
    :class:`pydantic_ai.ModelRetry` on the (cap+1)-th call.

    Test exercise: build the hooks with ``max_escalations=2``, call
    the recorder twice (both succeed), then assert the third call
    surfaces ``ModelRetry`` with the cap-hit message. The
    ``escalations_observed`` log records only the two accepted
    escalations -- the refused call is not counted.
    """
    workflow = _load_workflow_module()
    operator = _make_operator()
    cheap, deep = workflow.load_runnable_definitions()

    observed: list[str] = []
    recorder, _finalizer = workflow.make_policy_persisting_hooks(
        operator=operator,
        escalations_observed=observed,
        max_escalations=2,
    )

    # First two escalations land cleanly; the recorder returns a
    # fresh UUID for each (mirroring the production invocation
    # surface's child_run_id contract).
    first_id = await recorder(operator=operator, definition=deep, parent_run_id=None)
    second_id = await recorder(operator=operator, definition=deep, parent_run_id=None)
    assert isinstance(first_id, uuid.UUID)
    assert isinstance(second_id, uuid.UUID)
    assert observed == [deep.name, deep.name]

    # Third attempt trips the cap. ModelRetry surfaces inside the
    # cheap-tier's loop as a tool-level error the model reasons
    # about ("stop escalating, report cap-hit count").
    with pytest.raises(ModelRetry, match="escalation cap hit"):
        await recorder(operator=operator, definition=deep, parent_run_id=None)

    # The refused call is not added to the observed log -- the
    # cap-hit error happens before the bookkeeping.
    assert observed == [deep.name, deep.name]
    # The cheap-tier definition is unaffected by the cap; pulling
    # it here keeps both load_runnable_definitions return values
    # consumed (the test exercises the definitions as a pair, not
    # the deep tier in isolation).
    assert cheap.name == "r1-cheap-tier-classifier"


def test_make_policy_persisting_hooks_rejects_negative_cap() -> None:
    """A negative ``max_escalations`` is a misconfiguration.

    Reject at hook-construction time so the operator sees the
    error immediately, instead of as a confusing zero-escalation
    run later.
    """
    workflow = _load_workflow_module()
    operator = _make_operator()
    with pytest.raises(ValueError, match="max_escalations must be >= 0"):
        workflow.make_policy_persisting_hooks(
            operator=operator,
            max_escalations=-1,
        )


def test_load_runnable_definitions_matches_json_payloads() -> None:
    """:func:`load_runnable_definitions` builds tier-correct definitions.

    The harness's behaviour pivots on (cheap, deep). A future edit
    that swaps the order or accidentally lifts the deep tier's
    output_type onto the cheap tier breaks the closed loop in
    subtle ways the next test catches end-to-end, but pinning here
    surfaces the regression with a clearer message.
    """
    workflow = _load_workflow_module()
    cheap, deep = workflow.load_runnable_definitions()
    assert cheap.name == "r1-cheap-tier-classifier"
    assert deep.name == "r1-deep-tier-investigator"
    assert cheap.output_type is None
    assert deep.output_type is workflow.PolicyDecision


def test_build_cheap_tier_input_renders_sections_for_empty_and_populated_states() -> None:
    """:func:`build_cheap_tier_input` produces the prompt-documented sections.

    The cheap-tier system prompt names ``## Known policy`` and
    ``## Recent events`` as the two sections it reads. The harness
    is the only place those strings are emitted; drift would put
    the cheap tier in front of an unrecognised input.
    """
    workflow = _load_workflow_module()
    empty = workflow.build_cheap_tier_input(known_policy=[], recent_events=[])
    assert "## Known policy" in empty
    assert "## Recent events" in empty
    # Empty-state sentinel text is the operator's signal that the
    # tenant has no policy or no events yet -- pin so a refactor
    # doesn't leave the section empty.
    assert "(none -- this is a fresh tenant" in empty
    assert "(none -- nothing to triage" in empty

    populated = workflow.build_cheap_tier_input(
        known_policy=[],
        recent_events=[
            workflow.BroadcastEvent(
                event_id="evt-1",
                alert_class="vault-token-mint-prod",
                timestamp="2026-05-27T20:00:00Z",
                symptom="Prod vault token minted",
                signals=["signal_change_class_on_prod"],
            ),
        ],
    )
    # Events serialise to one JSON line each so the prompt-side
    # parsing stays deterministic.
    assert '"event_id":"evt-1"' in populated


# ---------------------------------------------------------------------------
# Test 5 -- the closed loop closes end to end against in-memory DB
# ---------------------------------------------------------------------------


def _function_model_for_closed_loop(
    *,
    cheap_briefing: dict[str, str],
    deep_decision_dump: dict[str, Any],
) -> FunctionModel:
    """Build a deterministic two-tier model.

    The single callback serves both tiers; disambiguation is by
    system-prompt content (the cheap and deep tiers carry distinct
    prompts, so the message history identifies which loop is active).

    Cheap-tier behaviour:
      - Turn 1: emit an ``invoke_agent`` tool call with the briefing
        payload above (one escalation per closed-loop tick).
      - Turn 2 (after the deep tier returns): emit the final answer.

    Deep-tier behaviour:
      - Turn 1: emit a final-result tool call carrying the
        :class:`PolicyDecision` dump. The output_schema constrains the
        framework to call the structured-output tool; ``info.output_tools[0].name``
        is the framework-assigned name for it.
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_deep = any(
            part.part_kind == "system-prompt"
            and "deep tier of a two-tier alert triage" in part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_deep:
            # Final result on turn 1; output_tools[0] is the
            # framework-assigned structured-output tool.
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        deep_decision_dump,
                    ),
                ],
            )
        # Cheap tier: invoke once, then finish.
        has_tool_return = any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "invoke_agent",
                        {
                            "agent_name": "r1-deep-tier-investigator",
                            "inputs": (
                                f"event_id: {cheap_briefing['event_id']}\n"
                                f"alert_class: {cheap_briefing['alert_class']}\n"
                                f"signals: {cheap_briefing['signals']}\n"
                                f"first_observed: {cheap_briefing['first_observed']}\n"
                                f"symptom: {cheap_briefing['symptom']}\n"
                            ),
                        },
                    ),
                ],
            )
        return ModelResponse(
            parts=[
                TextPart(
                    f"event {cheap_briefing['event_id']}: escalate "
                    f"(alert_class={cheap_briefing['alert_class']}, "
                    f"signals=[{cheap_briefing['signals']}])\n"
                    "escalated: 1, skipped: 0, deferred-due-to-cap: 0"
                ),
            ],
        )

    return FunctionModel(fn)


@pytest.mark.asyncio
async def test_closed_loop_writes_policy_to_memory(
    _fake_embedding_service: None,
) -> None:
    """The cheap -> deep -> policy-write cycle closes end to end.

    Asserts every load-bearing property the example documents:

    * The cheap tier's ``invoke_agent`` call goes through the
      harness's recorder (so the escalations_observed log records
      one entry per escalation).
    * The deep tier's structured :class:`PolicyDecision` flows into
      the harness's finalizer hook.
    * :meth:`MemoryService.remember` persists the policy under
      scope ``tenant``, slug ``r1-policy-<alert_class>``, with the
      harness-stamped provenance.
    * A follow-up :func:`load_known_policy` returns the newly
      written entry, completing the closed loop.
    """
    workflow = _load_workflow_module()
    operator = _make_operator()

    cheap_briefing = {
        "event_id": "evt-001",
        "alert_class": "vault-token-mint-prod",
        "signals": "signal_change_class_on_prod",
        "first_observed": "2026-05-27T20:00:00Z",
        "symptom": "Prod vault token minted by an unfamiliar operator",
    }
    deep_decision_dump = {
        "alert_class": "vault-token-mint-prod",
        "verdict": "acknowledged",
        "re_escalate": False,
        "summary": (
            "The mint was authorised under a now-rotated emergency credential. "
            "Re-mints by the same principal in the next 24h are expected and "
            "should not re-escalate."
        ),
        "evidence": [
            "vault_audit token=hvs.xxx ttl=600s",
            "operator: oncall-rotation@example.com (verified via PIM)",
        ],
        "recommended_action": None,
    }

    runtime = PydanticAgentRun(
        model_factory=lambda: _function_model_for_closed_loop(
            cheap_briefing=cheap_briefing,
            deep_decision_dump=deep_decision_dump,
        ),
    )
    recent_events = [
        workflow.BroadcastEvent(
            event_id=cheap_briefing["event_id"],
            alert_class=cheap_briefing["alert_class"],
            timestamp=cheap_briefing["first_observed"],
            symptom=cheap_briefing["symptom"],
            signals=[cheap_briefing["signals"]],
        ),
    ]

    # Patch the retriever the search path uses; the harness's
    # list_memories call does NOT call retrieve(), so this is a
    # belt-and-suspenders patch for any future load_known_policy
    # variant that might (and to keep this test cheap if it ever
    # does).
    with patch(
        "meho_backplane.retrieval.retriever.retrieve",
        new=AsyncMock(return_value=[]),
    ):
        result = await workflow.run_closed_loop(
            operator=operator,
            recent_events=recent_events,
            runtime=runtime,
        )

    # The cheap tier escalated exactly once -- one event was interesting,
    # no prior policy short-circuited it.
    assert len(result.escalations_observed) == 1
    assert result.escalations_observed[0] == "r1-deep-tier-investigator"

    # The harness persisted one policy entry.
    assert len(result.policy_write_backs) == 1
    write_back = result.policy_write_backs[0]
    assert write_back.slug == "r1-policy-vault-token-mint-prod"
    assert write_back.decision.verdict == "acknowledged"
    assert write_back.decision.re_escalate is False
    # The persisted entry carries the harness's provenance marker
    # so a future audit / promote sweep can identify R1-written
    # policy rows.
    assert write_back.entry.metadata.get("source") == "r1-tiered-triage"
    assert write_back.entry.scope == MemoryScope.TENANT

    # Closing the loop: a follow-up load_known_policy returns the
    # just-written entry. The cheap tier of the next firing would
    # see this entry in its ``## Known policy`` section and
    # short-circuit re-triage of the same alert class.
    follow_up = await workflow.load_known_policy(operator=operator)
    follow_up_slugs = {e.slug for e in follow_up}
    assert "r1-policy-vault-token-mint-prod" in follow_up_slugs


@pytest.mark.asyncio
async def test_closed_loop_short_circuits_when_no_events(
    _fake_embedding_service: None,
) -> None:
    """An empty event batch yields zero escalations + zero policy writes.

    The harness is the runtime path that decides "did the cheap
    tier do nothing?" -- a regression here would silently double-
    count firings as escalations or emit policy entries when the
    cheap tier said nothing was interesting.
    """
    workflow = _load_workflow_module()
    operator = _make_operator()

    def _silent_cheap(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        # Cheap tier says nothing was interesting on turn 1.
        return ModelResponse(parts=[TextPart("escalated: 0, skipped: 0, deferred-due-to-cap: 0")])

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(_silent_cheap),
    )
    with patch(
        "meho_backplane.retrieval.retriever.retrieve",
        new=AsyncMock(return_value=[]),
    ):
        result = await workflow.run_closed_loop(
            operator=operator,
            recent_events=[],
            runtime=runtime,
        )

    assert result.escalations_observed == []
    assert result.policy_write_backs == []
    # And the cheap tier's final-answer line is the documented
    # shape (so a doc walker that scrapes audit rows for the "0
    # escalated" sentinel still finds it).
    assert "escalated: 0" in result.cheap_tier_output


# ---------------------------------------------------------------------------
# Test 6 -- markdown link rot (mirror R4)
# ---------------------------------------------------------------------------


def _extract_relative_links(text: str) -> list[str]:
    """Return every markdown link target that looks like a relative path.

    Skips absolute http(s) URLs, ``mailto:``, ``#anchor``-only
    targets, and ``<...>`` autolinks. Strips a trailing
    ``#section-anchor`` so a link like ``../foo.md#bar`` resolves
    against ``../foo.md`` on disk.
    """
    targets: list[str] = []
    for match in _LINK_RE.finditer(text):
        target = match.group("target").strip()
        if not target:
            continue
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if target.startswith("javascript:"):
            continue
        target_without_fragment = target.split("#", 1)[0]
        if not target_without_fragment:
            continue
        targets.append(target_without_fragment)
    return targets


@pytest.mark.parametrize(
    "doc_filename",
    ["README.md", "GUIDE.md"],
)
def test_example_doc_relative_links_resolve(doc_filename: str) -> None:
    """Every relative link in the R1 docs resolves to an on-disk path.

    Catches link rot (the referenced file moved or renamed) at PR
    time. Anchor drift (the section heading changed) is rarer and
    a per-doc concern; this sweep is the always-on file-existence
    floor. Mirrors :mod:`test_examples_r4_local_claude`.
    """
    doc_path = EXAMPLE_DIR / doc_filename
    text = doc_path.read_text(encoding="utf-8")
    targets = _extract_relative_links(text)
    assert targets, f"expected at least one relative link in {doc_filename}"
    missing: list[str] = []
    for target in targets:
        resolved = (doc_path.parent / target).resolve()
        if not resolved.exists():
            missing.append(f"{target} -> {resolved}")
    assert not missing, (
        f"{doc_filename} references {len(missing)} missing path(s):\n  " + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------------
# Test 7 -- PolicyDecision schema matches the JSON output_schema (drift guard)
# ---------------------------------------------------------------------------


def test_policy_decision_matches_deep_tier_output_schema() -> None:
    """The :class:`PolicyDecision` Pydantic model and the deep tier's
    JSON ``output_schema`` are equivalent.

    The deep tier emits structured output validated by the JSON
    schema in :file:`agent.deep-tier-investigator.json`; the harness
    persists the output as a :class:`PolicyDecision` Pydantic model.
    The two must stay equivalent or the harness's
    ``isinstance(..., PolicyDecision)`` check in the finalizer
    silently skips real deep-tier outputs whose JSON validates but
    whose Python class drifted.
    """
    workflow = _load_workflow_module()
    raw = json.loads(
        (EXAMPLE_DIR / "agent.deep-tier-investigator.json").read_text(encoding="utf-8"),
    )
    json_schema = raw["output_schema"]
    pyd_schema = workflow.PolicyDecision.model_json_schema()

    # The JSON schema may be stricter than the Pydantic class on
    # ``required`` (a Pydantic field with a ``default_factory``
    # drops out of Pydantic's required set even though the
    # framework's structured-output tool always sends the key).
    # The contract we enforce is: every Pydantic-required field is
    # also JSON-required, and the JSON schema's required set
    # remains a subset of the property set.
    assert set(pyd_schema["required"]).issubset(set(json_schema["required"])), (
        f"Pydantic-required fields not all in JSON schema: "
        f"pyd={pyd_schema['required']} json={json_schema['required']}"
    )
    assert set(json_schema["required"]).issubset(set(json_schema["properties"].keys()))
    # property name agreement
    assert set(json_schema["properties"].keys()) == set(pyd_schema["properties"].keys())
    # verdict enum agreement
    assert set(json_schema["properties"]["verdict"]["enum"]) == {
        "benign",
        "acknowledged",
        "actionable",
    }

    # A valid sample round-trips through both halves.
    sample = {
        "alert_class": "test-class",
        "verdict": "benign",
        "re_escalate": False,
        "summary": "test",
        "evidence": ["t"],
        "recommended_action": None,
    }
    decision = workflow.PolicyDecision.model_validate(sample)
    assert decision.alert_class == "test-class"

    # An invalid alert_class shape (UPPERCASE) is rejected by both.
    with pytest.raises(ValidationError):
        workflow.PolicyDecision.model_validate({**sample, "alert_class": "Bad-Case"})
