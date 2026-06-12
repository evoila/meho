# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The investigation + retrieval :class:`AgentDefinition` instances.

Two definitions, both deliberately generic so a consumer can adapt the
prompts without re-wiring the loop:

* :data:`INVESTIGATION_AGENT` -- runs a bounded reasoning pass over a
  symptom blob, returns a structured :class:`Finding` (subject, summary,
  evidence list, suggested slug). The structured-output shape is what
  lets the harness persist the result to the knowledge base
  unambiguously: no string parsing of a free-text reply.
* :data:`RETRIEVAL_AGENT` -- the consumer pattern for the next run.
  Takes a free-form question, frames a search query, and returns a
  text answer grounded in the kb hits the harness loaded for it.

Why two definitions, not one
============================

The two halves of the loop run at different times against (usually)
different tenant contexts, so they read naturally as two definitions
rather than a single agent. The investigation half is the "writer";
the retrieval half is the "reader". Each can be invoked independently
by a scheduled trigger or a human, and each carries its own prompt
discipline (the writer is asked to produce a slug; the reader is asked
to cite the slugs it relied on).

Why no ``toolset`` block on either definition
=============================================

Both definitions take their context entirely as the loop's input
string. The kb write happens *after* the investigation completes, in
the harness, via :meth:`KbService.create_entry` -- the model isn't
asked to choose when or whether to write. That keeps the sample
deterministic and avoids tangling the example with the broader
toolset-resolver story (which is G11.1-T3's surface). A consumer
who wants the model itself to gate the write (e.g. via the MCP
``add_to_knowledge`` tool) can extend the sample by adding a
``toolset`` block and dropping the harness-side persistence step.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.agent import AgentDefinition

__all__ = [
    "INVESTIGATION_AGENT",
    "RETRIEVAL_AGENT",
    "Finding",
    "build_finding_body",
]


class Finding(BaseModel):
    """The structured output of one investigation run.

    Frozen + Pydantic-validated so the harness can persist the result
    without string parsing. ``slug`` is the kb-shaped identifier the
    model is asked to propose; the harness validates it against
    :func:`meho_backplane.kb.schemas.validate_slug` before writing,
    so a malformed slug surfaces as a :class:`ValueError` at the
    boundary rather than a silent retrieval miss later.

    The deliberate narrowness (one subject + one summary + a short
    evidence list) keeps the sample readable. A real consumer's
    Finding shape will accumulate additional fields (severity, blast
    radius, owning team) the consumer's harness already knows about.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(
        min_length=1,
        description="One-line restatement of what the investigation looked at.",
    )
    summary: str = Field(
        min_length=1,
        description="The investigator's conclusion, two-to-five sentences.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Verbatim quotes / log fragments / config snippets the conclusion rests on.",
    )
    slug: str = Field(
        min_length=1,
        description=(
            "Suggested kb slug for the finding -- kebab-case, "
            "starts with a lowercase letter, no leading digits. "
            "The harness validates this before writing; a bad slug "
            "raises rather than landing under the wrong key."
        ),
    )


#: System prompt for the investigation agent. Generic on purpose --
#: the consumer's harness substitutes the business-logic-specific
#: variant. The prompt is **not** the part this sample is teaching;
#: the sample teaches the *loop* (investigation -> kb write ->
#: retrieved as context). What the prompt does cover is the
#: structured-output contract (every field, why it matters) so the
#: model produces a writable :class:`Finding` instead of free text
#: that has to be re-parsed.
_INVESTIGATION_SYSTEM_PROMPT: str = """\
You are an investigation agent. The operator hands you a symptom or
fault signature drawn from operational telemetry; your job is to
examine it, draw a conclusion you can defend, and produce a structured
finding the team's knowledge base can hold.

Your output is a Finding object with four required fields:

* `subject` -- one line that restates what you looked at. Concrete,
  not abstract: "vCenter 9.0 snapshot revert fails on quiesced VMs",
  not "a snapshot issue".
* `summary` -- two to five sentences containing the actual reasoning
  and conclusion. State the cause, not just the symptom.
* `evidence` -- the verbatim fragments (log lines, config keys, error
  messages, version strings) the conclusion rests on. Future operators
  search the kb against these terms, so leave them as the operator
  would type them, not paraphrased.
* `slug` -- a kebab-case identifier the kb will store under. Shape
  rule: starts with a lowercase letter, ends with a lowercase letter
  or digit, contains only lowercase letters, digits, hyphens, or dots.
  Examples: `vcenter-9.0-snapshot-quiesce`, `argocd-sync-wave-deadlock`.

Write the summary in plain technical English -- no marketing voice,
no apologies, no preamble. Future operators read this directly under
time pressure.
"""


INVESTIGATION_AGENT: AgentDefinition = AgentDefinition(
    name="kb-writeback-investigator",
    system_prompt=_INVESTIGATION_SYSTEM_PROMPT,
    # Three turns is the model envelope a bounded investigation needs in
    # practice: read the symptom, optionally call one tool to look
    # something up (the sample's default surface lets the model call
    # `search_operations` / `call_operation`), and emit the structured
    # answer. Raise if your consumer's investigations chain more tool
    # calls -- this is a sample budget, not a hard cap on the pattern.
    request_limit=3,
    output_type=Finding,
)


#: System prompt for the retrieval / consumer-of-kb agent. Same
#: discipline as above: the prompt teaches the model the contract
#: (cite the slug you used), not the business problem.
_RETRIEVAL_SYSTEM_PROMPT: str = """\
You are a retrieval agent. Below you have the operator's question and
a short list of kb entries the harness has already loaded for you
(each one carries a slug, a snippet, and the metadata captured at
write time).

Your output is a plain-text answer grounded in the entries you were
given. Cite the slug of every kb entry whose content informed your
answer; if no entry is relevant, say so directly rather than
inventing one. Do not fabricate slugs -- a slug you cite must appear
in the list of entries above. Two to four sentences is the right
length for most questions.
"""


RETRIEVAL_AGENT: AgentDefinition = AgentDefinition(
    name="kb-writeback-retriever",
    system_prompt=_RETRIEVAL_SYSTEM_PROMPT,
    # Two turns: the operator question + the model's answer. No tool
    # calls expected because the harness pre-loads the kb hits into
    # the loop input; a consumer wanting the model to drive the kb
    # search itself adds `search_knowledge` to a toolset block.
    request_limit=2,
)


def build_finding_body(finding: Finding) -> str:
    """Render a :class:`Finding` into the Markdown body the kb will store.

    The body is what :class:`~meho_backplane.kb.service.KbService`
    embeds and indexes; the operator and the retrieval agent read it
    back through the kb's search hits. Keep the rendering plain
    Markdown -- no front-matter (the kb walker can read it but the
    metadata round-trip is for the per-finding facts already on the
    :class:`Finding`, not for prose).

    The evidence section is rendered as a fenced block so a future
    full-text search ranks the literal log line / config key the
    operator would type into the search bar against the canonical
    formatting -- BM25 doesn't care about case but it does care about
    token boundaries, and burying the evidence in a paragraph makes
    every term stem with surrounding prose.
    """
    parts: list[str] = []
    parts.append(f"# {finding.subject}")
    parts.append("")
    parts.append(finding.summary.rstrip())
    if finding.evidence:
        parts.append("")
        parts.append("## Evidence")
        parts.append("")
        for line in finding.evidence:
            parts.append(f"- `{line}`")
    parts.append("")
    return "\n".join(parts)
