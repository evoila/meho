# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tool-aware observation compression for the specialist agent scratchpad.

Compresses verbose Pydantic model tool outputs to compact text representations
before they enter the scratchpad. Each tool type has a purpose-built compression
template -- no generic formatting.

Phase 33 (v1.69 Token Optimization): ~50% token reduction per investigation.
"""

from __future__ import annotations

import os
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.inference import infer
from meho_app.modules.agents.react_agent.tools.call_operation import (
    CallOperationOutput,
)
from meho_app.modules.agents.react_agent.tools.dns_resolve import DnsResolveOutput
from meho_app.modules.agents.react_agent.tools.http_probe import HttpProbeOutput
from meho_app.modules.agents.react_agent.tools.reduce_data import (
    ReduceDataOutput,
)
from meho_app.modules.agents.react_agent.tools.search_knowledge import (
    SearchKnowledgeOutput,
)
from meho_app.modules.agents.react_agent.tools.search_operations import (
    SearchOperationsOutput,
)
from meho_app.modules.agents.react_agent.tools.tcp_probe import TcpProbeOutput
from meho_app.modules.agents.react_agent.tools.tls_check import TlsCheckOutput

logger = get_logger(__name__)

# Maximum columns to show in reduce_data markdown tables
_MAX_TABLE_COLUMNS = 25

# Maximum characters for individual cell values in reduce_data tables
_MAX_CELL_LENGTH = 60

# Maximum characters for operation descriptions in search_operations
_MAX_DESCRIPTION_LENGTH = 80

# Maximum number of per-parameter descriptions to include per operation
_MAX_PARAM_DESCRIPTIONS = 4

# Number of knowledge excerpts to show in fallback mode
_MAX_FALLBACK_EXCERPTS = 3

# Maximum characters per knowledge excerpt in fallback mode
_MAX_EXCERPT_LENGTH = 200


async def compress_observation(
    observation: Any,
    tool_name: str,
    thought: str = "",
) -> str:
    """Compress a tool observation to its essential signal.

    Dispatches to tool-specific compressors based on the observation type.
    String observations (error messages) pass through unchanged. Unknown
    model types fall back to str() representation.

    Args:
        observation: Raw tool output (Pydantic model or string).
        tool_name: Name of the tool that produced this observation.
        thought: The specialist's current reasoning (used for knowledge
            summarization context).

    Returns:
        Compact text representation of the observation.
    """
    # String observations (errors, approval denials) pass through unchanged
    if isinstance(observation, str):
        return observation

    # Dispatch to tool-specific compressors
    if isinstance(observation, CallOperationOutput):
        return _compress_call_operation(observation)

    if isinstance(observation, SearchOperationsOutput):
        return _compress_search_operations(observation)

    if isinstance(observation, SearchKnowledgeOutput):
        return await _compress_search_knowledge(observation, thought)

    if isinstance(observation, ReduceDataOutput):
        return _compress_reduce_data(observation)

    # Phase 96.1: Network diagnostic tool compressors
    if isinstance(observation, DnsResolveOutput):
        return _compress_dns_resolve(observation)
    if isinstance(observation, TcpProbeOutput):
        return _compress_tcp_probe(observation)
    if isinstance(observation, HttpProbeOutput):
        return _compress_http_probe(observation)
    if isinstance(observation, TlsCheckOutput):
        return _compress_tls_check(observation)

    # Unknown model type -- fall back to str()
    return str(observation)


def _compress_call_operation(output: CallOperationOutput) -> str:
    """Compress call_operation output to metadata-only format.

    Strips raw result data entirely -- the data lives in DuckDB cache and
    is accessible via reduce_data. Returns only table name, row count, and
    column list.

    Args:
        output: The CallOperationOutput from tool execution.

    Returns:
        Compressed metadata string.
    """
    if not output.success:
        return f"Error: {output.error}"

    if output.table:
        row_count = output.row_count or 0
        if output.columns:
            col_parts = []
            for c in output.columns:
                c_str = str(c)
                if output.column_types and c_str in output.column_types:
                    c_str += f":{output.column_types[c_str]}"
                col_parts.append(c_str)
            columns_str = ", ".join(col_parts)
        else:
            columns_str = "unknown"
        return (
            f"Cached table '{output.table}': {row_count} rows\n"
            f"Columns: {columns_str}\n"
            f"(Use reduce_data to query this table)"
        )

    if output.results:
        return f"Operation returned {len(output.results)} result(s) (non-cacheable)"

    return "Operation completed successfully (no data returned)"


def _compress_search_operations(
    output: SearchOperationsOutput,
) -> str:  # NOSONAR (cognitive complexity)
    """Compress search_operations output to name+description+params list.

    Includes parameter signatures with enum values and defaults so the LLM
    knows what to pass to call_operation. Without this, the LLM is guessing
    parameters blind.

    Format examples:
    - ``status [Running/Pending/Succeeded/Failed]`` — enum values
    - ``limit:integer (default:100)`` — default value
    - Compact descriptions for up to 4 params whose description differs
      meaningfully from the parameter name.

    Args:
        output: The SearchOperationsOutput from tool execution.

    Returns:
        Compact operation list with descriptions, parameter signatures,
        enum values, and defaults.
    """
    if not output.operations:
        return "No operations found"

    lines = [f"Found {output.total_found} operations:"]
    for op in output.operations:
        desc = op.description or op.name
        if len(desc) > _MAX_DESCRIPTION_LENGTH:
            desc = desc[:_MAX_DESCRIPTION_LENGTH] + "..."

        entry = f"- {op.operation_id}: {desc}"

        # Include category if non-null and different from op name
        if op.category and op.category != op.name:
            entry += f" ({op.category})"

        # Include parameter signatures so the LLM knows what call_operation needs
        if op.parameters:
            param_parts = []
            for p in op.parameters:
                sig = p.name
                if p.type and p.type != "string":
                    sig += f":{p.type}"
                if p.required:
                    sig += "*"  # asterisk marks required
                # Append enum choices inline
                if p.enum:
                    sig += " [" + "/".join(p.enum) + "]"
                # Append default value inline
                if p.default is not None:
                    sig += f" (default:{p.default})"
                param_parts.append(sig)
            entry += f"\n  params: {', '.join(param_parts)}"

            # Add compact descriptions for params whose description differs
            # meaningfully from just the parameter name (max 4 to stay compact)
            described = 0
            for p in op.parameters:
                if described >= _MAX_PARAM_DESCRIPTIONS:
                    break
                if not p.description:
                    continue
                # Skip trivial descriptions that just repeat the name
                desc_lower = p.description.lower().strip()
                name_lower = p.name.lower().replace("_", " ")
                if desc_lower == name_lower or desc_lower == f"{name_lower}.":
                    continue
                entry += f"\n    {p.name}: {p.description}"
                described += 1

        lines.append(entry)

    return "\n".join(lines)


async def _compress_search_knowledge(
    output: SearchKnowledgeOutput,
    thought: str,
) -> str:
    """Compress search_knowledge output via Haiku synthesis.

    Uses Claude Haiku 4.5 to synthesize matched documents into a single
    concise paragraph addressing the specialist's current investigation
    focus. Falls back to rule-based excerpts on any failure.

    Args:
        output: The SearchKnowledgeOutput from tool execution.
        thought: The specialist's current reasoning for context.

    Returns:
        Synthesized summary or fallback excerpts with source attribution.
    """
    if not output.results:
        return "No knowledge documents found"

    # Build source attribution header
    sources = []
    for r in output.results:
        if r.source:
            filename = os.path.basename(r.source)
            sources.append(filename)
        else:
            sources.append("unknown")
    header = f"Knowledge ({output.total_found} docs: {', '.join(sources)})"

    # Try Haiku summarization
    try:
        combined = "\n\n".join(f"[{r.source or 'unknown'}]: {r.content}" for r in output.results)
        system_prompt = (
            "You are a senior infrastructure engineer distilling documentation. "
            "Synthesize the provided documents into a single concise paragraph "
            "addressing the investigator's current focus. Include only actionable "
            "information. No preamble."
        )
        message = f"Investigator's current focus: {thought}\n\nDocuments:\n{combined}"
        summary = await infer(
            system_prompt=system_prompt,
            message=message,
            model="anthropic:claude-haiku-4-5",
            temperature=0.0,
        )
        return f"{header}:\n{summary}"

    except Exception as exc:
        logger.warning(
            "Knowledge summarization failed, using excerpt fallback",
            exc_info=exc,
        )
        # Fallback: first N chars of each result
        excerpts = []
        for r in output.results[:_MAX_FALLBACK_EXCERPTS]:
            excerpt = r.content[:_MAX_EXCERPT_LENGTH]
            if len(r.content) > _MAX_EXCERPT_LENGTH:
                excerpt += "..."
            excerpts.append(f"- {excerpt}")

        return f"{header}:\n" + "\n".join(excerpts)


def _compress_reduce_data(output: ReduceDataOutput) -> str:
    """Compress reduce_data output to markdown pipe table.

    Strips the Pydantic metadata wrapper entirely. Renders all rows as a
    clean markdown table with column trimming for wide results.

    Args:
        output: The ReduceDataOutput from tool execution.

    Returns:
        Markdown table or error message.
    """
    if not output.success:
        parts = [f"SQL error: {output.error}"]
        if output.available_tables:
            parts.append(f"Available tables: {', '.join(output.available_tables)}")
        return "\n".join(parts)

    if not output.rows:
        return "Query returned 0 rows"

    columns = list(output.columns)
    trimmed = False
    if len(columns) > _MAX_TABLE_COLUMNS:
        trimmed = True
        total_cols = len(columns)
        columns = columns[:_MAX_TABLE_COLUMNS]

    # Header row
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"

    # Data rows
    data_rows = []
    for row in output.rows:
        cells = []
        for col in columns:
            val = str(row.get(col, ""))
            # Escape pipe characters in cell values
            val = val.replace("|", "\\|")
            if len(val) > _MAX_CELL_LENGTH:
                val = val[:_MAX_CELL_LENGTH] + "..."
            cells.append(val)
        data_rows.append("| " + " | ".join(cells) + " |")

    # Footer
    footer = f"({output.row_count} rows)"
    if trimmed:
        footer += f" (showing {_MAX_TABLE_COLUMNS} of {total_cols} columns)"

    return "\n".join([header, separator, *data_rows, footer])


# ──────────────────────────────────────────────────────────────────────────────
# Phase 96.1: Network diagnostic tool compressors
# ──────────────────────────────────────────────────────────────────────────────


def _compress_dns_resolve(output: DnsResolveOutput) -> str:  # NOSONAR (cognitive complexity)
    """Compress dns_resolve output to compact per-record-type lines.

    Format: DNS {hostname}: {rtype}: {comma-separated values}
    """
    if not output.success:
        errors = "; ".join(output.errors) if output.errors else "unknown error"
        return f"DNS {output.hostname}: FAILED ({errors})"

    lines = [f"DNS {output.hostname}:"]
    for rtype, records in output.records.items():
        if rtype in ("A", "AAAA", "NS"):
            values = ", ".join(r.get("host", "") for r in records)
        elif rtype == "CNAME":
            values = ", ".join(r.get("cname", "") for r in records)
        elif rtype == "MX":
            values = ", ".join(
                f"{r.get('host', '')} (pri={r.get('priority', '')})" for r in records
            )
        elif rtype == "SRV":
            values = ", ".join(
                f"{r.get('host', '')}:{r.get('port', '')} (pri={r.get('priority', '')})"
                for r in records
            )
        elif rtype == "TXT":
            values = ", ".join(str(r.get("text", "")) for r in records)
        elif rtype == "SOA":
            r = records[0] if records else {}
            values = f"ns={r.get('nsname', '')} serial={r.get('serial', '')}"
        else:
            values = str(records)
        lines.append(f"  {rtype}: {values}")

    if output.errors:
        for err in output.errors:
            lines.append(f"  ERR: {err}")

    return "\n".join(lines)


def _compress_tcp_probe(output: TcpProbeOutput) -> str:
    """Compress tcp_probe output to a single status line."""
    line = f"TCP {output.host}:{output.port} -> {output.status}"
    if output.latency_ms is not None and output.status == "connected":
        line += f" ({output.latency_ms:.1f}ms)"
    if output.error:
        line += f" [{output.error}]"
    return line


def _compress_http_probe(output: HttpProbeOutput) -> str:
    """Compress http_probe output to status line + optional redirect chain."""
    if output.error and not output.success:
        return f"HTTP {output.url}: FAILED ({output.error})"

    lines = [
        f"HTTP {output.url} -> {output.status_code} "
        f"({output.latency_ms:.0f}ms, {output.content_type})"
    ]

    # Show final URL if different (redirect happened)
    if output.final_url and output.final_url != output.url:
        lines.append(f"  Final URL: {output.final_url}")

    # Show redirect chain if present
    for step in output.redirect_chain:
        lines.append(f"  -> {step.get('status_code', '?')} {step.get('url', '')}")

    # Body preview truncated to 200 chars for scratchpad
    if output.body_preview:
        preview = output.body_preview[:200]
        if len(output.body_preview) > 200:
            preview += "..."
        lines.append(f"  Body: {preview}")

    return "\n".join(lines)


def _compress_tls_check(output: TlsCheckOutput) -> str:
    """Compress tls_check output to certificate summary."""
    if output.error and not output.success:
        return f"TLS {output.hostname}:{output.port}: FAILED ({output.error})"

    subject_cn = output.subject.get("commonName", "unknown")
    chain_str = "valid" if output.chain_valid else "INVALID"

    line = (
        f"TLS {output.hostname}:{output.port} -> {subject_cn}, "
        f"expires {output.expires_at} ({output.days_until_expiry}d), "
        f"{output.protocol_version}, chain: {chain_str}"
    )

    lines = [line]

    # Show SANs on second line if more than 1
    if len(output.sans) > 1:
        lines.append(f"  SANs: {', '.join(output.sans)}")

    # Show error for chain-invalid but successful checks
    if output.error and output.success:
        lines.append(f"  Warning: {output.error}")

    return "\n".join(lines)
