"""Topology subcommand group - query infrastructure topology.

Commands:
    lookup      Look up entities by UUID or fuzzy search, with depth-N graph traversal
    correlate   View and resolve cross-system entity correlations
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import typer

from meho_claude.cli.output import output_error, output_response

app = typer.Typer(help="Query infrastructure topology.")


# ---------------------------------------------------------------------------
# Internal helpers (test-mockable seams)
# ---------------------------------------------------------------------------


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


def _topology_search(
    conn: sqlite3.Connection,
    state_dir,
    query: str,
    limit: int = 10,
    connector_name: str | None = None,
) -> list[dict]:
    """Hybrid search wrapper (test-mockable seam)."""
    from meho_claude.core.topology.search import topology_hybrid_search

    return topology_hybrid_search(
        conn, state_dir, query, limit=limit, connector_name=connector_name,
    )


def _query_relationships_recursive(
    conn: sqlite3.Connection, entity_id: str, depth: int,
) -> list[dict]:
    """Traverse relationships to depth N using recursive CTE with cycle prevention.

    Returns list of dicts: {entity_id, rel_type, depth, name, entity_type,
    connector_name, connector_type}.
    """
    sql = """
        WITH RECURSIVE traversal(entity_id, rel_type, depth, path) AS (
            SELECT
                CASE WHEN r.from_entity_id = ? THEN r.to_entity_id ELSE r.from_entity_id END,
                r.relationship_type, 1,
                ? || ',' || CASE WHEN r.from_entity_id = ? THEN r.to_entity_id ELSE r.from_entity_id END
            FROM topology_relationships r
            WHERE r.from_entity_id = ? OR r.to_entity_id = ?
            UNION
            SELECT
                CASE WHEN r.from_entity_id = t.entity_id THEN r.to_entity_id ELSE r.from_entity_id END,
                r.relationship_type, t.depth + 1,
                t.path || ',' || CASE WHEN r.from_entity_id = t.entity_id THEN r.to_entity_id ELSE r.from_entity_id END
            FROM topology_relationships r
            JOIN traversal t ON (r.from_entity_id = t.entity_id OR r.to_entity_id = t.entity_id)
            WHERE t.depth < ?
              AND t.path NOT LIKE '%' || CASE WHEN r.from_entity_id = t.entity_id THEN r.to_entity_id ELSE r.from_entity_id END || '%'
        )
        SELECT DISTINCT t.entity_id, t.rel_type, t.depth,
               e.name, e.entity_type, e.connector_name, e.connector_type
        FROM traversal t
        JOIN topology_entities e ON e.id = t.entity_id
        ORDER BY t.depth, e.entity_type, e.name
    """
    rows = conn.execute(
        sql, (entity_id, entity_id, entity_id, entity_id, entity_id, depth),
    ).fetchall()

    return [
        {
            "entity_id": row["entity_id"],
            "rel_type": row["rel_type"],
            "depth": row["depth"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            "connector_name": row["connector_name"],
            "connector_type": row["connector_type"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def lookup(
    ctx: typer.Context,
    entity: str = typer.Argument(..., help="Entity name, UUID, or search term."),
    depth: int = typer.Option(1, "--depth", help="Relationship traversal depth."),
    connector: str | None = typer.Option(None, "--connector", help="Filter by connector name."),
) -> None:
    """Look up topology entities and their relationships."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.topology.store import TopologyStore

    store = TopologyStore(state_dir)
    try:
        # Step 1: Try exact ID match
        matched = store.get_entity_by_id(entity)

        if matched is None:
            # Step 2: Fuzzy search
            results = _topology_search(
                store.conn, state_dir, entity, limit=10, connector_name=connector,
            )

            # Step 3: Disambiguate
            if not results:
                output_error(
                    "Entity not found",
                    code="ENTITY_NOT_FOUND",
                    suggestion="Try a different search term or 'meho topology lookup' with a UUID.",
                )
                return

            if len(results) == 1:
                matched = store.get_entity_by_id(results[0]["id"])
            else:
                # Multiple matches -- return candidate list for Claude to disambiguate
                candidates = [
                    {
                        "id": r["id"],
                        "name": r.get("name", ""),
                        "entity_type": r.get("entity_type", ""),
                        "connector_name": r.get("connector_name", ""),
                        "relevance_score": r.get("relevance_score", 0.0),
                    }
                    for r in results
                ]
                output_response(
                    {"status": "multiple_matches", "candidates": candidates},
                    human=ctx.obj.human,
                    start_time=start_time,
                )
                return

        if matched is None:
            output_error(
                "Entity not found",
                code="ENTITY_NOT_FOUND",
                suggestion="Try a different search term or 'meho topology lookup' with a UUID",
            )
            return

        entity_id = matched.id

        # Step 4: Build full response
        entity_dict = matched.model_dump()

        # Relationships via recursive CTE
        relationships = _query_relationships_recursive(store.conn, entity_id, depth)

        # Correlations (all statuses)
        correlations = store.get_correlations(entity_id)

        response: dict[str, Any] = {
            "status": "ok",
            "entity": entity_dict,
            "relationships": relationships,
            "correlations": correlations,
        }

        if ctx.obj.human:
            _render_lookup_human(entity_dict, relationships, correlations)

        output_response(response, human=ctx.obj.human, start_time=start_time)

    finally:
        store.close()


def _render_lookup_human(
    entity_dict: dict, relationships: list[dict], correlations: list[dict],
) -> None:
    """Render lookup results as Rich panels and tables to stderr."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console(stderr=True)

    # Entity card
    lines = [
        f"[bold]{entity_dict['name']}[/bold]",
        f"Type: {entity_dict['entity_type']}",
        f"Connector: {entity_dict.get('connector_name', 'N/A')} ({entity_dict['connector_type']})",
    ]
    if entity_dict.get("description"):
        lines.append(f"Description: {entity_dict['description']}")
    scope = entity_dict.get("scope", {})
    if scope:
        for k, v in scope.items():
            lines.append(f"{k}: {v}")
    console.print(Panel("\n".join(lines), title="Entity", border_style="blue"))

    # Relationships table
    if relationships:
        table = Table(title="Relationships")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Connector")
        table.add_column("Relationship")
        table.add_column("Depth")
        for r in relationships:
            table.add_row(
                r["name"], r["entity_type"], r.get("connector_name", ""),
                r["rel_type"], str(r["depth"]),
            )
        console.print(table)
    else:
        console.print("[dim]No relationships found.[/dim]")

    # Correlations panel
    if correlations:
        corr_lines = []
        for c in correlations:
            status_color = {"pending": "yellow", "confirmed": "green", "rejected": "red"}.get(
                c["status"], "white"
            )
            details = c.get("match_details", "")
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except (json.JSONDecodeError, TypeError):
                    pass
            corr_lines.append(
                f"[{status_color}]{c['status'].upper()}[/{status_color}] "
                f"{c.get('entity_a_name', '?')} <-> {c.get('entity_b_name', '?')} "
                f"({c['match_type']}, confidence: {c['confidence']})"
            )
        console.print(Panel("\n".join(corr_lines), title="Correlations", border_style="cyan"))


@app.command()
def correlate(
    ctx: typer.Context,
    confirm_id: str | None = typer.Option(None, "--confirm", help="Confirm a pending correlation by ID."),
    reject_id: str | None = typer.Option(None, "--reject", help="Reject a pending correlation by ID."),
    show_all: bool = typer.Option(False, "--all", help="Show all correlations, not just pending."),
) -> None:
    """View and resolve cross-system entity correlations."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.topology.correlator import CorrelationEngine
    from meho_claude.core.topology.store import TopologyStore

    store = TopologyStore(state_dir)
    try:
        if confirm_id is not None:
            # Verify correlation exists and is pending
            row = store.conn.execute(
                "SELECT id, status FROM topology_correlations WHERE id = ?",
                (confirm_id,),
            ).fetchone()

            if row is None:
                output_error(
                    f"Correlation not found: {confirm_id}",
                    code="CORRELATION_NOT_FOUND",
                    suggestion="Run 'meho topology correlate' to list pending correlations.",
                )
                return

            if row["status"] != "pending":
                output_error(
                    f"Correlation {confirm_id} is not pending (status: {row['status']})",
                    code="CORRELATION_NOT_PENDING",
                    suggestion="Only pending correlations can be confirmed or rejected.",
                )
                return

            correlator = CorrelationEngine(store.conn)
            correlator.confirm_correlation(confirm_id)

            # Fetch updated correlation with entity details
            updated = _fetch_correlation_detail(store.conn, confirm_id)
            output_response(
                {"status": "ok", "action": "confirmed", "correlation": updated},
                human=ctx.obj.human,
                start_time=start_time,
            )

            if ctx.obj.human:
                _render_correlate_action_human("confirmed", updated)

        elif reject_id is not None:
            # Verify correlation exists and is pending
            row = store.conn.execute(
                "SELECT id, status FROM topology_correlations WHERE id = ?",
                (reject_id,),
            ).fetchone()

            if row is None:
                output_error(
                    f"Correlation not found: {reject_id}",
                    code="CORRELATION_NOT_FOUND",
                    suggestion="Run 'meho topology correlate' to list pending correlations.",
                )
                return

            if row["status"] != "pending":
                output_error(
                    f"Correlation {reject_id} is not pending (status: {row['status']})",
                    code="CORRELATION_NOT_PENDING",
                    suggestion="Only pending correlations can be confirmed or rejected.",
                )
                return

            correlator = CorrelationEngine(store.conn)
            correlator.reject_correlation(reject_id)

            # Fetch updated correlation with entity details
            updated = _fetch_correlation_detail(store.conn, reject_id)
            output_response(
                {"status": "ok", "action": "rejected", "correlation": updated},
                human=ctx.obj.human,
                start_time=start_time,
            )

            if ctx.obj.human:
                _render_correlate_action_human("rejected", updated)

        else:
            # List mode
            if show_all:
                correlations = _fetch_all_correlations(store.conn)
            else:
                correlations = [dict(r) for r in store.get_pending_correlations()]

            pending_count = sum(1 for c in correlations if c.get("status") == "pending")

            response: dict[str, Any] = {
                "status": "ok",
                "correlations": correlations,
                "pending_count": pending_count,
            }

            if ctx.obj.human:
                _render_correlate_list_human(correlations)

            output_response(response, human=ctx.obj.human, start_time=start_time)

    finally:
        store.close()


def _fetch_correlation_detail(conn: sqlite3.Connection, correlation_id: str) -> dict:
    """Fetch a single correlation with entity details."""
    row = conn.execute(
        """SELECT c.id, c.entity_a_id, c.entity_b_id, c.match_type,
                  c.confidence, c.match_details, c.status,
                  c.discovered_at, c.resolved_at, c.resolved_by,
                  ea.name as entity_a_name, ea.entity_type as entity_a_type,
                  ea.connector_name as entity_a_connector,
                  eb.name as entity_b_name, eb.entity_type as entity_b_type,
                  eb.connector_name as entity_b_connector
           FROM topology_correlations c
           JOIN topology_entities ea ON c.entity_a_id = ea.id
           JOIN topology_entities eb ON c.entity_b_id = eb.id
           WHERE c.id = ?""",
        (correlation_id,),
    ).fetchone()

    return dict(row) if row else {}


def _fetch_all_correlations(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all correlations with entity details (all statuses)."""
    rows = conn.execute(
        """SELECT c.id, c.entity_a_id, c.entity_b_id, c.match_type,
                  c.confidence, c.match_details, c.status,
                  c.discovered_at, c.resolved_at, c.resolved_by,
                  ea.name as entity_a_name, ea.entity_type as entity_a_type,
                  ea.connector_name as entity_a_connector,
                  eb.name as entity_b_name, eb.entity_type as entity_b_type,
                  eb.connector_name as entity_b_connector
           FROM topology_correlations c
           JOIN topology_entities ea ON c.entity_a_id = ea.id
           JOIN topology_entities eb ON c.entity_b_id = eb.id"""
    ).fetchall()
    return [dict(row) for row in rows]


def _render_correlate_list_human(correlations: list[dict]) -> None:
    """Render correlation list as Rich table to stderr."""
    from rich.console import Console
    from rich.table import Table

    console = Console(stderr=True)

    if not correlations:
        console.print("[dim]No correlations found.[/dim]")
        return

    table = Table(title="Correlations")
    table.add_column("ID", max_width=8)
    table.add_column("Entity A")
    table.add_column("Entity B")
    table.add_column("Match Type")
    table.add_column("Confidence")
    table.add_column("Status")

    for c in correlations:
        status = c.get("status", "?")
        status_color = {"pending": "yellow", "confirmed": "green", "rejected": "red"}.get(
            status, "white"
        )
        table.add_row(
            c["id"][:8],
            f"{c.get('entity_a_name', '?')} ({c.get('entity_a_type', '?')}, {c.get('entity_a_connector', '?')})",
            f"{c.get('entity_b_name', '?')} ({c.get('entity_b_type', '?')}, {c.get('entity_b_connector', '?')})",
            c.get("match_type", "?"),
            str(c.get("confidence", 0.0)),
            f"[{status_color}]{status}[/{status_color}]",
        )

    console.print(table)


def _render_correlate_action_human(action: str, correlation: dict) -> None:
    """Render confirm/reject result as Rich panel to stderr."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console(stderr=True)
    color = "green" if action == "confirmed" else "red"
    lines = [
        f"[{color}]Correlation {action}[/{color}]",
        f"Entity A: {correlation.get('entity_a_name', '?')} ({correlation.get('entity_a_connector', '?')})",
        f"Entity B: {correlation.get('entity_b_name', '?')} ({correlation.get('entity_b_connector', '?')})",
        f"Match: {correlation.get('match_type', '?')} (confidence: {correlation.get('confidence', 0.0)})",
    ]
    console.print(Panel("\n".join(lines), title=f"Correlation {action.title()}", border_style=color))
