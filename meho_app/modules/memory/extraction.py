# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Memory extraction module: PydanticAI agent, output models, and background task.

Converts conversation findings into structured memories (entities, patterns, outcomes)
using a PydanticAI agent with structured output. Runs as a fire-and-forget background
task after conversation completion.

The extraction pipeline:
1. Receives per-connector findings from the orchestrator (plain dicts)
2. Builds an extraction prompt per connector
3. Runs a PydanticAI agent to extract structured memories
4. Feeds results into MemoryService.bulk_create() for dedup-aware storage
"""

from pydantic import BaseModel, Field
from pydantic_ai import Agent, InstrumentationSettings

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.database import get_session_maker
from meho_app.modules.memory.models import ConfidenceLevel, MemoryType
from meho_app.modules.memory.schemas import MemoryCreate
from meho_app.modules.memory.service import get_memory_service

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic Output Models
# ---------------------------------------------------------------------------


class ExtractedMemory(BaseModel):
    """A single memory extracted from a conversation."""

    title: str = Field(
        ...,
        max_length=500,
        description="Concise title for scanning",
    )
    body: str = Field(
        ...,
        description="Full context: what was found, details, relationships",
    )
    memory_type: MemoryType = Field(
        ...,
        description="entity, pattern, or outcome",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="2-5 tags: connector type, technology, entity names, problem category",
    )


class ExtractionResult(BaseModel):
    """Structured output from the extraction LLM call."""

    is_trivial: bool = Field(
        ...,
        description="True if conversation too trivial to extract memories from",
    )
    memories: list[ExtractedMemory] = Field(
        default_factory=list,
        description="Extracted memories, empty if is_trivial=True",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of extraction decisions",
    )


# ---------------------------------------------------------------------------
# Extraction Prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_INSTRUCTIONS = (
    "You extract structured memories from infrastructure diagnostic conversations. "
    "Return validated JSON matching the ExtractionResult schema."
)

EXTRACTION_PROMPT_TEMPLATE = """You are extracting structured memories from an infrastructure diagnostic conversation with the connector "{connector_name}".

## Memory Types

### entity
Named infrastructure objects: VMs, pods, services, clusters, hosts, datastores. Concrete things with names/IDs that the agent interacted with.
Example: "VM web-prod-03 runs on esxi-host-2, has 8GB RAM, CentOS 7"

### pattern
Diagnostic patterns: recurring symptoms and their root causes. Observations about how this infrastructure behaves.
Example: "When pod X keeps restarting on this cluster, it's usually an OOM issue -- check resource limits"

### outcome
Resolution outcomes: what the problem was, what fixed it, and the result.
Example: "VM web-prod-03 was unresponsive -- increased memory to 16GB, resolved the OOM. User confirmed fixed"

Note: "config" type memories are operator-curated only (created via the "remember this" command). Auto-extraction produces entity, pattern, and outcome types.

## Trivial Conversation Check

This conversation involved {tool_calls_count} tool invocations.

If this conversation is trivial -- fewer than 3 tool invocations AND no clear resolution or diagnostic finding -- set is_trivial=true and return empty memories. Short conversations WITH resolutions should still be extracted (preserves "quick fix" memories).

## Tag Guidance

Generate 2-5 tags per memory from: connector type, technology name, entity names, problem category. Tags should help with retrieval and categorization.

## Quality Guidance

Prefer fewer high-quality memories over many vague ones. Each memory should be independently useful -- a future agent reading just that memory should gain actionable context. Include specific names, IDs, values, and relationships.

## Conversation Findings

{findings}

## Instructions

Extract all relevant entities, patterns, and outcomes from the conversation above. Return your analysis as structured JSON matching the ExtractionResult schema."""


def build_extraction_prompt(
    findings: str,
    connector_name: str,
    tool_calls_count: int,
) -> str:
    """
    Build the extraction prompt for a single connector's findings.

    Args:
        findings: The specialist agent's reasoning + tool results for this connector.
        connector_name: Human-readable connector name.
        tool_calls_count: Number of tool invocations in the conversation.

    Returns:
        Formatted extraction prompt string.
    """
    return EXTRACTION_PROMPT_TEMPLATE.format(
        connector_name=connector_name,
        tool_calls_count=tool_calls_count,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# PydanticAI Extraction Agent
# ---------------------------------------------------------------------------


def create_extraction_agent() -> Agent:
    """
    Create the memory extraction PydanticAI agent.

    Uses the configured memory_extraction_model and returns ExtractionResult
    as structured output.
    """
    config = get_config()

    return Agent(
        config.memory_extraction_model,
        output_type=ExtractionResult,
        instructions=EXTRACTION_SYSTEM_INSTRUCTIONS,
        instrument=InstrumentationSettings(),
    )


# ---------------------------------------------------------------------------
# Background Task Function
# ---------------------------------------------------------------------------


async def run_memory_extraction(
    connector_findings: list[dict],
    tenant_id: str,
    conversation_id: str,
    tool_calls_count: int,
) -> None:
    """
    Background task: extract memories from conversation findings.

    Creates its own DB session (independent of the request lifecycle).
    Runs extraction per-connector and feeds results into MemoryService.bulk_create().

    Args:
        connector_findings: List of dicts with keys: connector_id, connector_name, findings.
        tenant_id: Tenant scope for created memories.
        conversation_id: Conversation ID for provenance tracking.
        tool_calls_count: Number of tool invocations in the conversation (for trivial detection).
    """
    try:
        session_maker = get_session_maker()

        async with session_maker() as session:
            memory_service = get_memory_service(session)
            agent = create_extraction_agent()

            for cf in connector_findings:
                try:
                    prompt = build_extraction_prompt(
                        findings=cf["findings"],
                        connector_name=cf["connector_name"],
                        tool_calls_count=tool_calls_count,
                    )
                    result = await agent.run(prompt)
                    extraction: ExtractionResult = result.output

                    if extraction.is_trivial or not extraction.memories:
                        logger.info(
                            "memory_extraction_skipped",
                            connector_name=cf["connector_name"],
                            reason="trivial" if extraction.is_trivial else "no_memories",
                            reasoning=extraction.reasoning,
                        )
                        continue

                    # Convert ExtractedMemory objects to MemoryCreate objects
                    memory_creates = [
                        MemoryCreate(
                            title=mem.title,
                            body=mem.body,
                            memory_type=mem.memory_type,
                            tags=mem.tags,
                            confidence_level=ConfidenceLevel.AUTO_EXTRACTED,
                            source_type="extraction",
                            created_by="system",
                            connector_id=cf["connector_id"],
                            tenant_id=tenant_id,
                            conversation_id=conversation_id,
                        )
                        for mem in extraction.memories
                    ]

                    bulk_result = await memory_service.bulk_create(memory_creates)
                    logger.info(
                        "memory_extraction_complete",
                        connector_name=cf["connector_name"],
                        created=bulk_result.created,
                        merged=bulk_result.merged,
                        total_memories=len(extraction.memories),
                        reasoning=extraction.reasoning,
                    )

                except Exception as e:
                    logger.error(
                        "memory_extraction_connector_failed",
                        connector_name=cf.get("connector_name", "unknown"),
                        error=str(e),
                        exc_info=True,
                    )

            await session.commit()

    except Exception as e:
        logger.warning(
            "memory_extraction_failed",
            error=str(e),
            conversation_id=conversation_id,
            exc_info=True,
        )
