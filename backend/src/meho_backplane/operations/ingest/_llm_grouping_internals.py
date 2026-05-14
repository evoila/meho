# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Private helpers for the T3 LLM grouping orchestrator.

Schemas, parsers, prompt-rendering, and DB-query helpers live here so
:mod:`meho_backplane.operations.ingest.llm_groups` stays focused on
orchestration. The split keeps both files comfortably under the
project's per-file size budget; nothing in this module is part of the
public surface re-exported by ``__init__.py``.

Re-exports the system prompts as module-level constants so the
orchestrator can pass them verbatim to :meth:`LlmClient.generate_json`
without re-declaring them in every call site.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from typing import Any, Final, Protocol
from uuid import UUID

import structlog
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest.exceptions import LlmOutputInvalid

__all__ = [
    "ASSIGN_OPS_SYSTEM_PROMPT",
    "DEFAULT_GROUPING_BATCH_SIZE",
    "DEFAULT_MAX_GROUPS",
    "DEFAULT_MIN_GROUPS",
    "NONE_GROUP_KEY",
    "PASS1_MAX_OUTPUT_TOKENS",
    "PASS2_MAX_OUTPUT_TOKENS",
    "PROPOSE_GROUPS_SYSTEM_PROMPT",
    "GroupAssignment",
    "GroupProposal",
    "LlmClient",
    "build_connector_id",
    "chunk_sequence",
    "expected_llm_call_count",
    "load_existing_groups",
    "load_unassigned_ops",
    "parse_assignment_response",
    "parse_proposal_response",
    "render_assign_ops_prompt",
    "render_propose_groups_prompt",
]

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: Default batch size for Pass 2 (per-op assignment). The op count
#: divided by this number determines the Pass-2 LLM call count.
#: Sized to keep each batch's prompt well under the Anthropic Messages
#: API's 200K-token context window even with verbose op summaries; the
#: real ceiling is per-call latency + cost, not context length.
DEFAULT_GROUPING_BATCH_SIZE: Final[int] = 50

#: Minimum number of groups the LLM is asked to propose. Below ~8, the
#: taxonomy collapses into vendor-bucket-by-tag; above ~15, the agent's
#: scope-then-search flow starts to feel like keyword search again.
#: Operator can override per ingest if the spec has unusual shape.
DEFAULT_MIN_GROUPS: Final[int] = 8

#: Maximum number of groups (see :data:`DEFAULT_MIN_GROUPS`).
DEFAULT_MAX_GROUPS: Final[int] = 15

#: Group-key sentinel returned by Pass 2 when no group is a fit. Ops
#: assigned to this value stay ``group_id=NULL`` and bubble up as
#: ``operations_unassigned`` -- the operator assigns them manually
#: via T4's ``edit_op``.
NONE_GROUP_KEY: Final[str] = "none"

#: Pattern enforcing snake_case on ``group_key``. Same shape the
#: typed-connector groups use (``vm_lifecycle``, ``cluster``,
#: ``events``) so the operator-facing review payload looks uniform
#: across spec-ingested and typed connectors.
_GROUP_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Token budget for Pass 1. Sized for the median connector (~50-100
#: ops); large connectors are still well under model output limits
#: because each proposal is 100-200 tokens and the model is bounded to
#: ``DEFAULT_MAX_GROUPS`` entries by the prompt instructions.
PASS1_MAX_OUTPUT_TOKENS: Final[int] = 4096

#: Token budget for one Pass-2 batch. Each entry is a ``"op_id":
#: "group_key"`` pair, ~30-80 tokens; 50 ops fits comfortably under
#: this budget with room for the response wrapper.
PASS2_MAX_OUTPUT_TOKENS: Final[int] = 4096

PROPOSE_GROUPS_SYSTEM_PROMPT: Final[str] = (
    "You design operation taxonomies for the MEHO backplane. You are given a list of "
    "API operations from a vendor's OpenAPI specification and must propose a small, "
    "agent-actionable set of operation groups. You respond with ONLY a JSON array. "
    "Do not include explanatory prose, code fences, or any text outside the JSON. "
    "Every group's when_to_use field must be specific enough that an AI agent reading "
    "only that paragraph can decide whether to scope a search to this group."
)

ASSIGN_OPS_SYSTEM_PROMPT: Final[str] = (
    "You assign API operations to operation groups for the MEHO backplane. You receive "
    "a fixed set of group definitions and a batch of operations, and must return a JSON "
    'object mapping each operation\'s op_id to exactly one group_key (or "none" when no '
    "group fits). Respond with ONLY the JSON object. Do not include explanatory prose, "
    "code fences, or any text outside the JSON."
)


# ---------------------------------------------------------------------------
# LLM client Protocol
# ---------------------------------------------------------------------------


class LlmClient(Protocol):
    """Minimum surface the grouping pass needs from a chassis LLM client.

    Designed as a structural :class:`Protocol` rather than an abstract
    base so the chassis can ship a concrete adapter (Anthropic Sonnet/
    Haiku via the Messages API), tests can ship a deterministic stub,
    and downstream code paths can mock with ``unittest.mock.AsyncMock``
    without inheriting an ABC.

    Implementations resolve their model id, API key, retry/backoff,
    and prompt-caching policy internally. T3 only cares about the
    request/response shape: a system prompt + a user prompt go in, a
    raw string comes back. JSON validation is T3's concern, not the
    client's.
    """

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        """Return the LLM's raw text response."""
        ...


# ---------------------------------------------------------------------------
# Output schemas (LLM-facing JSON contract)
# ---------------------------------------------------------------------------


class GroupProposal(BaseModel):
    """One group proposed by Pass 1.

    The full LLM Pass-1 response is a JSON array of these. ``frozen=True``
    keeps the validator output read-only between parse and persist.
    """

    model_config = ConfigDict(frozen=True)

    group_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    when_to_use: str = Field(min_length=1, max_length=2048)

    @field_validator("group_key")
    @classmethod
    def _group_key_must_be_snake_case(cls, value: str) -> str:
        if not _GROUP_KEY_PATTERN.fullmatch(value):
            raise ValueError(
                f"group_key {value!r} must match {_GROUP_KEY_PATTERN.pattern!r} "
                "(snake_case, starts with a letter)",
            )
        return value


class GroupAssignment(BaseModel):
    """One element of the Pass-2 mapping (verbatim from the LLM).

    Pass 2's response is a JSON object ``{"<op_id>": "<group_key>", ...}``
    rather than a list, so this model isn't validated directly --
    instead :func:`parse_assignment_response` builds it per pair after
    JSON-loading. The class exists primarily for type clarity on
    internal call sites; the JSON validation lives in
    :func:`parse_assignment_response`.
    """

    model_config = ConfigDict(frozen=True)

    op_id: str = Field(min_length=1)
    group_key: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _build_jinja_environment() -> Environment:
    """Build the Jinja2 :class:`Environment` for the prompt templates.

    ``StrictUndefined`` raises :class:`jinja2.UndefinedError` on any
    missing variable so a typo in the call site breaks the test loudly
    rather than silently rendering ``""``. ``autoescape`` is disabled
    via :func:`select_autoescape` with an empty list -- the rendered
    output is sent verbatim to an LLM, not embedded in HTML, so
    autoescaping would actively corrupt the prompt (e.g. ``&`` ->
    ``&amp;``).
    """
    return Environment(
        loader=PackageLoader(
            "meho_backplane.operations.ingest",
            package_path="prompts",
        ),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _op_view(proto_or_row: Any) -> dict[str, Any]:
    """Project an op into the dict shape the prompt templates iterate over."""
    return {
        "op_id": proto_or_row.op_id,
        "summary": proto_or_row.summary,
        "tags": list(proto_or_row.tags or []),
    }


def render_propose_groups_prompt(
    *,
    connector_id: str,
    product: str,
    version: str,
    impl_id: str,
    operations: Sequence[Any],
    min_groups: int = DEFAULT_MIN_GROUPS,
    max_groups: int = DEFAULT_MAX_GROUPS,
) -> str:
    """Render the Pass-1 (propose-groups) prompt body as a string."""
    env = _build_jinja_environment()
    template = env.get_template("propose_groups.md.j2")
    return template.render(
        connector_id=connector_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_count=len(operations),
        operations=[_op_view(op) for op in operations],
        min_groups=min_groups,
        max_groups=max_groups,
    )


def render_assign_ops_prompt(
    *,
    connector_id: str,
    product: str,
    version: str,
    groups: Sequence[GroupProposal],
    operations: Sequence[Any],
) -> str:
    """Render the Pass-2 (assign-ops) prompt body as a string."""
    env = _build_jinja_environment()
    template = env.get_template("assign_op_to_group.md.j2")
    return template.render(
        connector_id=connector_id,
        product=product,
        version=version,
        groups=[
            {
                "group_key": g.group_key,
                "name": g.name,
                "when_to_use": g.when_to_use,
            }
            for g in groups
        ],
        operations=[_op_view(op) for op in operations],
    )


# ---------------------------------------------------------------------------
# LLM-output parsing
# ---------------------------------------------------------------------------

#: Strip leading/trailing fenced-code markup the model sometimes emits
#: in addition to bare JSON. Matches a single optional ```json``` or
#: ``` ... ``` block; everything outside the first match is preserved
#: so unexpected wrapper prose still surfaces in the parse error.
_FENCE_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL,
)


def strip_code_fences(raw: str) -> str:
    """Remove a single surrounding ```json ... ``` fence, if present."""
    match = _FENCE_PATTERN.match(raw)
    if match is None:
        return raw.strip()
    return match.group(1).strip()


def parse_proposal_response(raw_output: str) -> list[GroupProposal]:
    """Parse Pass-1 output into a validated :class:`GroupProposal` list.

    Failure modes that raise :class:`LlmOutputInvalid`:

    * JSON parse error (the model wrapped its output in prose, or
      truncated mid-token).
    * Top-level value isn't a JSON array.
    * Any element fails the :class:`GroupProposal` pydantic schema
      (missing fields, oversized ``when_to_use``, non-snake_case
      ``group_key``).
    * Duplicate ``group_key`` values across the array -- the row
      uniqueness constraint on :class:`OperationGroup` would catch
      this at DB flush time, but we'd rather fail fast with a clearer
      message.
    """
    cleaned = strip_code_fences(raw_output)
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmOutputInvalid(
            pass_name="propose_groups",
            raw_output=raw_output,
            parse_error=exc,
        ) from exc
    if not isinstance(decoded, list):
        raise LlmOutputInvalid(
            pass_name="propose_groups",
            raw_output=raw_output,
            parse_error=ValueError(
                f"expected a JSON array at the top level, got {type(decoded).__name__}",
            ),
        )
    try:
        proposals = [GroupProposal.model_validate(item) for item in decoded]
    except ValidationError as exc:
        raise LlmOutputInvalid(
            pass_name="propose_groups",
            raw_output=raw_output,
            parse_error=exc,
        ) from exc
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.group_key in seen:
            raise LlmOutputInvalid(
                pass_name="propose_groups",
                raw_output=raw_output,
                parse_error=ValueError(
                    f"duplicate group_key in proposal: {proposal.group_key!r}",
                ),
            )
        seen.add(proposal.group_key)
    return proposals


def _decode_assignment_json(raw_output: str) -> dict[str, Any]:
    """Decode + structurally-validate a Pass-2 LLM response.

    Returns the raw JSON dict; the caller filters its entries against
    the known ``op_id`` and ``group_key`` sets. Raises
    :class:`LlmOutputInvalid` if the top-level structure is wrong.
    """
    cleaned = strip_code_fences(raw_output)
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmOutputInvalid(
            pass_name="assign_ops",
            raw_output=raw_output,
            parse_error=exc,
        ) from exc
    if not isinstance(decoded, dict):
        raise LlmOutputInvalid(
            pass_name="assign_ops",
            raw_output=raw_output,
            parse_error=ValueError(
                f"expected a JSON object at the top level, got {type(decoded).__name__}",
            ),
        )
    return decoded


def parse_assignment_response(
    raw_output: str,
    *,
    valid_op_ids: set[str],
    valid_group_keys: set[str],
) -> dict[str, str]:
    """Parse Pass-2 output into an ``op_id -> group_key`` mapping.

    The parser is **defensive but not destructive**:

    * Unknown keys (an ``op_id`` not in *valid_op_ids*) are dropped
      with a warning log.
    * Unknown values (a ``group_key`` not in *valid_group_keys* and
      not :data:`NONE_GROUP_KEY`) are coerced to ``"none"`` (operator
      assigns manually later). The op stays in the result as
      unassigned -- we still want to record that the model
      considered it.

    The only conditions that raise :class:`LlmOutputInvalid` are
    structural: JSON parse error or top-level value isn't a JSON
    object. Both cases would otherwise leave the caller with no
    actionable signal.
    """
    decoded = _decode_assignment_json(raw_output)
    mapping: dict[str, str] = {}
    for raw_key, raw_value in decoded.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            _log.warning(
                "llm_grouping_assignment_skipped_non_string",
                op_id_type=type(raw_key).__name__,
                value_type=type(raw_value).__name__,
            )
            continue
        if raw_key not in valid_op_ids:
            _log.warning(
                "llm_grouping_assignment_skipped_unknown_op",
                op_id=raw_key,
            )
            continue
        if raw_value == NONE_GROUP_KEY:
            mapping[raw_key] = NONE_GROUP_KEY
            continue
        if raw_value not in valid_group_keys:
            _log.warning(
                "llm_grouping_assignment_unknown_group_key",
                op_id=raw_key,
                group_key=raw_value,
            )
            mapping[raw_key] = NONE_GROUP_KEY
            continue
        mapping[raw_key] = raw_value
    return mapping


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def load_unassigned_ops(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    tenant_id: UUID | None,
) -> list[EndpointDescriptor]:
    """Return every :class:`EndpointDescriptor` for the connector with no ``group_id``.

    Sort by ``op_id`` so re-runs see deterministic batch boundaries --
    helpful for prompt-cache hit rates and for diff-friendly test
    assertions across runs.
    """
    stmt = (
        select(EndpointDescriptor)
        .where(
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            EndpointDescriptor.group_id.is_(None),
        )
        .order_by(EndpointDescriptor.op_id)
    )
    if tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def load_existing_groups(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    tenant_id: UUID | None,
) -> list[OperationGroup]:
    """Return every :class:`OperationGroup` row in the connector scope.

    Drives the partial-regrouping branch: if existing groups are
    present, we skip Pass 1 and use them as-is for Pass 2 over the
    unassigned-op subset.
    """
    stmt = (
        select(OperationGroup)
        .where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
        )
        .order_by(OperationGroup.group_key)
    )
    if tenant_id is None:
        stmt = stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        stmt = stmt.where(OperationGroup.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def build_connector_id(product: str, version: str, impl_id: str) -> str:
    """Render the operator-facing ``<impl_id>-<version>`` identifier.

    Inverse of :func:`meho_backplane.operations.ingest.parser.parse_connector_id`.
    Used to populate audit-row payloads and structlog kwargs so the
    string operators see in logs / CLI output matches the one they
    typed at ``meho connector ingest`` time.
    """
    return f"{impl_id}-{version}"


def chunk_sequence(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Yield successive *size*-length slices of *items*.

    Pure helper -- avoids pulling :func:`itertools.batched` to keep
    the project's import surface minimal and the helper trivially
    unit-testable.
    """
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def expected_llm_call_count(op_count: int, batch_size: int) -> int:
    """Return the LLM-call count a full grouping run produces.

    ``1`` for Pass 1 plus ``ceil(op_count / batch_size)`` for Pass 2.
    Used by the test suite to pin the documented call-count contract;
    not part of the production hot path.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1; got {batch_size}")
    if op_count < 1:
        return 0
    return 1 + math.ceil(op_count / batch_size)
