# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-run priming text for the MCP ``initialize.instructions`` preamble.

Initiative #1199 (G12.4) -- the priming text the agent sees when it
attaches to an operator who has ``in_progress`` runs. T1 ships the
helper as a pure-ish module so the composition rules (per-run block
shape, N=5 cap, summary-form fallback, verbatim wording) are reviewable
in isolation; T2 (#1316) wires it into :func:`assemble_preamble`.

The text format is fixed by Initiative #1199 and is **load-bearing**:

  <<RUNBOOK_PRIMING -- CRITICAL>>
  You are mid-runbook `<slug>` v<version> on step <n>/<total> (`<step_id>`).
  Follow only the current step. Do not look ahead. Do not improvise. Do not combine steps.
  If the step looks wrong, call runbook_abort and escalate to a senior in chat.
  Use runbook_next to advance once the current step's verify passes.

  <<END_RUNBOOK_PRIMING>>

One block per in-progress run, capped at :data:`MAX_PRIMING_BLOCKS`. Beyond
that, a single summary block points at ``runbook_list_runs`` so the
preamble stays in budget.

Priming is **UX hint, not enforcement**. Step opacity (G12.3, #1313) is
the real adherence mechanism; if priming breaks, opacity still holds.
The phrasing is a high-confidence nudge to keep the agent inside the
opacity floor's intended discipline -- "follow only the current step"
mirrors the substrate's guarantee that only the current step is
returned by ``runbook_next``.

Untrusted-content isolation
---------------------------

Guard delimiters (:data:`BLOCK_START` / :data:`BLOCK_END`) are hard-
coded module constants emitted by the wrapper, never interpolated from
run state. The same positional-wrapper discipline the conventions
preamble uses (see ``conventions/preamble.py:42-63``): a run whose
slug somehow contained the literal terminator string cannot escape the
block, because the terminator is not substituted from user content.

The slug regex enforced at template publish time (see
:mod:`meho_backplane.runbooks.schemas`) already prevents the
terminator-shaped substring in practice; the wrapper discipline is
defence in depth, regression-tested at
:func:`test_guard_delimiters_are_hard_coded`.
"""

from __future__ import annotations

from typing import Final, NamedTuple
from uuid import UUID

from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import ListRunsFilter, RunSummary

__all__ = [
    "BLOCK_END",
    "BLOCK_START",
    "MAX_PRIMING_BLOCKS",
    "RunbookPrimingResult",
    "assemble_runbook_priming",
]


#: Opening delimiter for a priming block. Hard-coded; the wrapper emits
#: the literal -- it is never substituted from run state. The em dash
#: (``--``) form is the verbatim text fixed by Initiative #1199; agents
#: are trained against the exact byte sequence, so the constant must
#: render byte-for-byte at every block boundary.
BLOCK_START: Final[str] = "<<RUNBOOK_PRIMING — CRITICAL>>"

#: Closing delimiter for a priming block. Pairs with :data:`BLOCK_START`;
#: see the module docstring for the positional-wrapper rationale (a body
#: containing this literal cannot escape the block because the
#: terminator is wrapper-emitted, not user-emitted).
BLOCK_END: Final[str] = "<<END_RUNBOOK_PRIMING>>"

#: Maximum number of per-run priming blocks before collapsing to a
#: single summary block. Beyond ~5 runs the per-block text dominates
#: the preamble's token budget; the summary form refers the agent to
#: ``runbook_list_runs`` (the read tool from G12.3-T6, #1313) and
#: tells them to proceed one at a time. Same opacity discipline still
#: applies to each run when they advance it.
MAX_PRIMING_BLOCKS: Final[int] = 5


class RunbookPrimingResult(NamedTuple):
    """Packed priming text + diagnostics for caller logging.

    Three fields:

    * ``text`` -- the assembled priming string, or ``""`` when the
      operator has no in-progress runs. The caller (T2 wiring) collapses
      the empty string to "skip the priming section entirely" so the
      preamble shape is byte-for-byte identical to its conventions-only
      form for operators without runbooks in flight.
    * ``block_count`` -- the count of in-progress runs the operator has,
      whether or not they were rendered as per-run blocks. ``0`` when
      the operator has no runs; ``K`` (the full count) even when the
      summary form fires because the count is still useful for logging.
    * ``summarized`` -- ``True`` when ``block_count > MAX_PRIMING_BLOCKS``
      and the helper rendered the summary form instead of per-run
      blocks; ``False`` otherwise (including the empty case).

    Why a :class:`NamedTuple` and not a :class:`pydantic.BaseModel`?
    Internal-only return shape; no JSON marshalling boundary. Matches
    the :class:`~meho_backplane.conventions.preamble.PreambleResult`
    posture from the conventions preamble (one NamedTuple at the
    assembler boundary; pydantic stays at the wire boundary).
    """

    text: str
    block_count: int
    summarized: bool


async def assemble_runbook_priming(
    operator_sub: str,
    tenant_id: UUID,
) -> RunbookPrimingResult:
    """Build the priming text for *operator_sub*'s ``in_progress`` runs in *tenant_id*.

    Calls :meth:`RunbookRunService.list_runs` with
    ``caller_is_admin=False`` so the operator-scope visibility floor
    applies regardless of the caller's actual role -- priming is
    always operator-personal, even if the operator happens to be a
    ``TENANT_ADMIN``. The list is filtered to ``state='in_progress'``
    so terminal runs do not leak into the priming.

    Returns three shapes by case:

    * **Empty** -- no in-progress runs -> ``RunbookPrimingResult("", 0, False)``.
      The caller skips the priming section entirely.
    * **1 .. N=5** -- one verbatim block per run, concatenated with a
      blank line between them.
      ``RunbookPrimingResult(text, len(runs), False)``.
    * **>N=5** -- one summary block referring the agent to
      ``runbook_list_runs``. ``RunbookPrimingResult(text, len(runs), True)``.

    The helper makes no DB query beyond the single
    :meth:`list_runs` call; ``current_step_id`` and ``position`` arrive
    on the :class:`~meho_backplane.runbooks.runs_schemas.RunSummary`
    rows directly (per #1300 / #1308). No caching -- per Initiative
    #1199 the priming is generated fresh on every MCP ``initialize``
    call because run state can change between sessions.

    A :class:`RunSummary` with ``current_step_id is None`` would be a
    contract violation (an in-progress run always has a current step
    per the service layer's invariant); such a row is skipped from
    per-block rendering but still counted toward ``block_count`` so
    the summary-form threshold is honoured deterministically.
    """
    service = RunbookRunService()
    runs = await service.list_runs(
        tenant_id,
        operator_sub,
        caller_is_admin=False,
        filter_=ListRunsFilter(status="in_progress"),
    )
    count = len(runs)
    if count == 0:
        return RunbookPrimingResult(text="", block_count=0, summarized=False)
    if count > MAX_PRIMING_BLOCKS:
        return RunbookPrimingResult(
            text=_render_summary_block(count),
            block_count=count,
            summarized=True,
        )
    blocks = [_render_run_block(run) for run in runs if run.current_step_id is not None]
    return RunbookPrimingResult(
        text="\n\n".join(blocks),
        block_count=count,
        summarized=False,
    )


def _render_run_block(run: RunSummary) -> str:
    """Render the per-run priming block for a single in-progress run.

    Wraps the body in the hard-coded :data:`BLOCK_START` / :data:`BLOCK_END`
    delimiters. The body fields (``slug`` / ``version`` / position /
    step id) are taken from the :class:`RunSummary`; the wrapper is
    positional (no string substitution into the delimiter), so the
    terminator cannot be emitted from run state.

    Caller guarantees ``run.current_step_id is not None`` (filtered in
    :func:`assemble_runbook_priming` before this is called); the
    ``assert`` re-asserts at the boundary so a future contract change
    surfaces here rather than as a ``None``-shaped ``f-string`` in the
    rendered text.
    """
    assert run.current_step_id is not None, "caller filters runs without current_step_id"
    position = run.position
    # An in-progress run with no position is also a contract
    # violation (the service computes position whenever the
    # template body is present); fall back to a "n/total" of
    # ``?/?`` rather than crashing the preamble assembly.
    position_text = "?/?" if position is None else f"{position.n}/{position.total}"
    body = (
        f"You are mid-runbook `{run.template_slug}` v{run.template_version} "
        f"on step {position_text} (`{run.current_step_id}`).\n"
        "Follow only the current step. Do not look ahead. Do not improvise. "
        "Do not combine steps.\n"
        "If the step looks wrong, call runbook_abort and escalate to a "
        "senior in chat.\n"
        "Use runbook_next to advance once the current step's verify passes."
    )
    return f"{BLOCK_START}\n{body}\n\n{BLOCK_END}"


def _render_summary_block(count: int) -> str:
    """Render the summary priming block for >N=5 in-progress runs.

    Refers the agent to ``runbook_list_runs`` (the read tool from
    G12.3-T6, #1313) and tells them to proceed one at a time. Re-
    states the opacity discipline so the summary form does not loosen
    the per-run wording's adherence floor.
    """
    body = (
        f"You have {count} in-progress runbook runs assigned to you "
        f"(too many to list inline).\n"
        "Call runbook_list_runs to see them and proceed one at a time.\n"
        "Follow the same opacity discipline: do not look ahead in any run."
    )
    return f"{BLOCK_START}\n{body}\n\n{BLOCK_END}"
