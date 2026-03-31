"""Connector subcommand group - manage infrastructure connectors.

Commands:
    add         Add a new connector (wizard or non-interactive)
    list        List all configured connectors
    test        Test a connector connection
    call        Call a connector operation with trust model enforcement
    search-ops  Search available operations via hybrid search
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import typer

from meho_claude.cli.output import output_error, output_response
from meho_claude.core.audit import audit_log
from meho_claude.core.trust import enforce_trust

app = typer.Typer(help="Manage infrastructure connectors.")

# Connector type categories
SPEC_TYPES = {"rest", "soap"}
SDK_TYPES = {"kubernetes", "vmware", "proxmox", "gcp"}


# ---------------------------------------------------------------------------
# Internal helpers (test-mockable seams)
# ---------------------------------------------------------------------------


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


def _discover_operations(spec_source: str, connector_name: str) -> list:
    """Discover operations from a spec (synchronous wrapper)."""
    from meho_claude.core.connectors.models import AuthConfig, ConnectorConfig
    from meho_claude.core.connectors.rest import RESTConnector

    # Determine spec_url vs spec_path
    spec_url = spec_source if spec_source.startswith(("http://", "https://")) else None
    spec_path = None if spec_url else spec_source

    config = ConnectorConfig(
        name=connector_name,
        connector_type="rest",
        base_url="https://placeholder.local",
        spec_url=spec_url,
        spec_path=spec_path,
        auth=AuthConfig(method="bearer", credential_name="placeholder"),
    )
    connector = RESTConnector(config)
    return asyncio.run(connector.discover_operations())


def _discover_sdk_operations(config) -> list:
    """Discover operations for SDK-based connectors (no spec parsing).

    Instantiates the typed connector via registry and calls
    discover_operations(). No network access needed -- all SDK
    connectors return hardcoded operation lists.
    """
    from meho_claude.core.connectors.registry import get_connector_class

    cls = get_connector_class(config.connector_type)
    connector = cls(config)
    try:
        return asyncio.run(connector.discover_operations())
    finally:
        connector.close()


def _confirm_save(operation_count: int, tag_groups: dict[str, int], auth_method: str) -> bool:
    """Show dry-run preview and ask for confirmation."""
    from rich.console import Console

    console = Console(stderr=True)
    console.print(
        f"\n[bold]Found {operation_count} endpoints, "
        f"{len(tag_groups)} tagged groups. "
        f"Auth: {auth_method}.[/]"
    )
    for tag, count in sorted(tag_groups.items()):
        console.print(f"  [dim]{tag}:[/] {count} operations")

    return typer.confirm("Save this connector?", default=True)


def _store_credentials(
    state_dir: Path, credential_name: str, auth_method: str, non_interactive: bool
) -> None:
    """Store credentials via CredentialManager."""
    from meho_claude.core.credentials import CredentialManager

    cm = CredentialManager(state_dir)

    if non_interactive:
        # In non-interactive mode, store a placeholder (user must configure later)
        cm.store(credential_name, {"_placeholder": True})
        return

    # Interactive: prompt for credential values based on auth method
    if auth_method == "bearer":
        token = typer.prompt("Bearer token", hide_input=True)
        cm.store(credential_name, {"token": token})
    elif auth_method == "basic":
        username = typer.prompt("Username")
        password = typer.prompt("Password", hide_input=True)
        cm.store(credential_name, {"username": username, "password": password})
    elif auth_method == "api_key":
        key = typer.prompt("API key", hide_input=True)
        cm.store(credential_name, {"api_key": key})
    else:
        cm.store(credential_name, {"_placeholder": True})


def _test_connector_connection(state_dir: Path, name: str) -> dict[str, Any]:
    """Load and test a connector, returning the result dict."""
    from meho_claude.core.connectors.loader import instantiate_connector, load_connector_config
    from meho_claude.core.credentials import CredentialManager

    # Find YAML config
    yaml_path = state_dir / "connectors" / f"{name}.yaml"
    if not yaml_path.exists():
        return {"status": "error", "message": f"No config found for connector: {name}"}

    config = load_connector_config(yaml_path)
    cm = CredentialManager(state_dir)
    connector = instantiate_connector(config, cm)
    try:
        return asyncio.run(connector.test_connection())
    finally:
        connector.close()


def _hybrid_search(
    conn, state_dir: Path, query: str, limit: int = 10, connector_name: str | None = None
) -> list[dict]:
    """Hybrid search wrapper (test-mockable seam)."""
    from meho_claude.core.search.hybrid import hybrid_search

    return hybrid_search(conn, state_dir, query, limit=limit, connector_name=connector_name)


def _lookup_operation(state_dir: Path, connector_name: str, operation_id: str) -> dict | None:
    """Look up an operation from the database.

    Returns operation as dict or None if not found.
    """
    from meho_claude.core.database import get_connection

    db_path = state_dir / "meho.db"
    if not db_path.exists():
        return None

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT id, connector_name, operation_id, display_name, description,
                      trust_tier, http_method, url_template, input_schema, output_schema,
                      tags, example_params, related_operations
               FROM operations
               WHERE connector_name = ? AND operation_id = ?""",
            (connector_name, operation_id),
        ).fetchone()

        if row is None:
            return None

        return {
            "id": row["id"],
            "connector_name": row["connector_name"],
            "operation_id": row["operation_id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "trust_tier": row["trust_tier"],
            "http_method": row["http_method"],
            "url_template": row["url_template"],
            "input_schema": row["input_schema"],
            "output_schema": row["output_schema"],
            "tags": row["tags"],
            "example_params": row["example_params"],
            "related_operations": row["related_operations"],
        }
    finally:
        conn.close()


def _load_and_execute_operation(
    state_dir: Path, connector_name: str, operation: dict, params: dict
) -> dict[str, Any]:
    """Load a connector and execute an operation.

    Returns the execution result dict from the connector.
    """
    from meho_claude.core.connectors.loader import instantiate_connector, load_connector_config
    from meho_claude.core.connectors.models import Operation
    from meho_claude.core.credentials import CredentialManager

    yaml_path = state_dir / "connectors" / f"{connector_name}.yaml"
    config = load_connector_config(yaml_path)
    cm = CredentialManager(state_dir)
    connector = instantiate_connector(config, cm)

    # Build Operation model from dict
    op_model = Operation(
        connector_name=operation["connector_name"],
        operation_id=operation["operation_id"],
        display_name=operation["display_name"],
        description=operation.get("description", ""),
        trust_tier=operation["trust_tier"],
        http_method=operation.get("http_method"),
        url_template=operation.get("url_template"),
    )

    try:
        return asyncio.run(connector.execute(op_model, params))
    finally:
        connector.close()


def _get_connector_type(state_dir: Path, connector_name: str) -> str | None:
    """Look up connector_type from meho.db for a given connector name.

    Returns None if connector not found or DB doesn't exist.
    """
    from meho_claude.core.database import get_connection

    db_path = state_dir / "meho.db"
    if not db_path.exists():
        return None

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT connector_type FROM connectors WHERE name = ?",
            (connector_name,),
        ).fetchone()
        return row["connector_type"] if row else None
    finally:
        conn.close()


def _extract_topology(
    state_dir: Path,
    connector_name: str,
    connector_type: str,
    operation_id: str,
    result_data: dict[str, Any],
) -> None:
    """Run topology extraction as a side effect after connector call.

    Lazily imports and calls run_extraction from topology extractor.
    Wrapped in try/except to be doubly safe -- never breaks connector flow.
    """
    try:
        from meho_claude.core.topology.extractor import run_extraction

        run_extraction(state_dir, connector_name, connector_type, operation_id, result_data)
    except Exception:
        pass  # Extraction is a side effect -- never break caller


def _check_and_cache(
    state_dir: Path, connector_name: str, operation_id: str, response_data: Any
) -> dict | None:
    """Check if response should be cached and cache it if so.

    Returns cache summary dict if cached, None otherwise.
    """
    from meho_claude.core.data.cache import ResponseCache

    cache = ResponseCache(state_dir / "cache.duckdb")
    try:
        data = response_data
        if isinstance(data, dict) and "data" in data:
            data = data["data"]

        if isinstance(data, list) and cache.should_cache(data):
            return cache.cache_response(connector_name, operation_id, data)
        return None
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def add(
    ctx: typer.Context,
    connector_type: str = typer.Option("rest", "--type", help="Connector type."),
    name: Optional[str] = typer.Option(None, "--name", help="Connector name."),
    url: Optional[str] = typer.Option(None, "--url", help="Base URL."),
    spec: Optional[str] = typer.Option(None, "--spec", help="OpenAPI spec URL or file path."),
    auth_method: Optional[str] = typer.Option(
        None, "--auth-method", help="Auth method: bearer, basic, api_key."
    ),
    credential_name: Optional[str] = typer.Option(
        None, "--credential-name", help="Credential store name."
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Skip interactive prompts."
    ),
    kubeconfig_path: Optional[str] = typer.Option(
        None, "--kubeconfig-path", help="Path to kubeconfig file."
    ),
    kubeconfig_context: Optional[str] = typer.Option(
        None, "--kubeconfig-context", help="Kubeconfig context name."
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id", help="GCP project ID."
    ),
    service_account_path: Optional[str] = typer.Option(
        None, "--service-account-path", help="GCP service account JSON path."
    ),
    proxmox_token_id: Optional[str] = typer.Option(
        None, "--proxmox-token-id", help="Proxmox API token ID (user@realm!token)."
    ),
    verify_ssl: bool = typer.Option(
        True, "--verify-ssl/--no-verify-ssl", help="Verify SSL certificates."
    ),
) -> None:
    """Add a new connector via wizard or flags."""
    start_time = time.time()
    settings = _get_settings(ctx)

    from meho_claude.core.connectors.loader import save_connector_config
    from meho_claude.core.connectors.models import AuthConfig, ConnectorConfig

    # --- Prompt for name (all types need a name) ---
    if not non_interactive:
        if not name:
            name = typer.prompt("Connector name")

    # --- Branch on connector type ---
    if connector_type in SPEC_TYPES:
        # ===================== REST / SOAP flow =====================
        # Prompt for spec-specific values in interactive mode
        if not non_interactive:
            if not url:
                url = typer.prompt("Base URL")
            if not spec:
                spec = typer.prompt("OpenAPI spec (URL or file path)")
            if not auth_method:
                auth_method = typer.prompt(
                    "Auth method",
                    type=typer.Choice(["bearer", "basic", "api_key"]),
                    default="bearer",
                )
            if not credential_name:
                credential_name = typer.prompt("Credential name", default=f"{name}-creds")

        # Validate required fields for REST/SOAP
        if not all([name, url, spec, auth_method, credential_name]):
            output_error(
                "Missing required fields. Provide all flags with --non-interactive or run interactively.",
                code="MISSING_FIELDS",
                suggestion="Required flags: --name, --url, --spec, --auth-method, --credential-name. Or run 'meho connector add' without flags for the interactive wizard.",
            )
            return

        # Discover operations from spec
        try:
            operations = _discover_operations(spec, name)
        except Exception as exc:
            output_error(
                f"Failed to parse spec: {exc}",
                code="SPEC_PARSE_ERROR",
                suggestion="Check that the spec URL/path is correct and the spec is valid OpenAPI 3.x.",
            )
            return

        # Build config for REST/SOAP
        spec_url = spec if spec.startswith(("http://", "https://")) else None
        spec_path_val = None if spec_url else spec

        config = ConnectorConfig(
            name=name,
            connector_type=connector_type,
            description="",
            base_url=url,
            spec_url=spec_url,
            spec_path=spec_path_val,
            auth=AuthConfig(method=auth_method, credential_name=credential_name),
        )

    elif connector_type in SDK_TYPES:
        # ===================== SDK flow =====================
        # Type-specific interactive prompts
        if not non_interactive:
            if connector_type == "kubernetes":
                if not kubeconfig_path:
                    kubeconfig_path = typer.prompt(
                        "Kubeconfig path", default="~/.kube/config"
                    )
                if not kubeconfig_context:
                    kubeconfig_context = typer.prompt(
                        "Kubeconfig context (leave empty for default)", default=""
                    ) or None

            elif connector_type == "vmware":
                if not url:
                    url = typer.prompt("vCenter host URL")
                if not auth_method:
                    auth_method = "basic"
                if not credential_name:
                    credential_name = typer.prompt("Credential name", default=f"{name}-creds")

            elif connector_type == "proxmox":
                if not url:
                    url = typer.prompt("Proxmox VE host URL")
                if not auth_method:
                    auth_method = typer.prompt(
                        "Auth method",
                        type=typer.Choice(["basic", "api_key"]),
                        default="basic",
                    )
                if not credential_name:
                    credential_name = typer.prompt("Credential name", default=f"{name}-creds")
                if not proxmox_token_id and auth_method == "api_key":
                    proxmox_token_id = typer.prompt("Proxmox token ID (user@realm!token)")

            elif connector_type == "gcp":
                if not project_id:
                    project_id = typer.prompt("GCP project ID")
                if not service_account_path:
                    sa_input = typer.prompt(
                        "Service account JSON path (leave empty for ADC)", default=""
                    )
                    service_account_path = sa_input or None

        # Validate required fields for SDK types
        if not name:
            output_error(
                "Missing required field: --name.",
                code="MISSING_FIELDS",
                suggestion="Provide --name flag.",
            )
            return

        if connector_type == "vmware" and not all([url, auth_method, credential_name]):
            output_error(
                "Missing required fields for VMware connector.",
                code="MISSING_FIELDS",
                suggestion="Required flags: --name, --url, --auth-method, --credential-name.",
            )
            return

        if connector_type == "proxmox" and not all([url, auth_method, credential_name]):
            output_error(
                "Missing required fields for Proxmox connector.",
                code="MISSING_FIELDS",
                suggestion="Required flags: --name, --url, --auth-method, --credential-name.",
            )
            return

        if connector_type == "gcp" and not project_id:
            output_error(
                "Missing required field: --project-id for GCP connector.",
                code="MISSING_FIELDS",
                suggestion="Required flags: --name, --project-id.",
            )
            return

        # Build type-specific ConnectorConfig
        auth_config = None
        if connector_type in ("vmware", "proxmox") and auth_method and credential_name:
            auth_config = AuthConfig(method=auth_method, credential_name=credential_name)

        config = ConnectorConfig(
            name=name,
            connector_type=connector_type,
            description="",
            base_url=url or "",
            auth=auth_config,
            kubeconfig_path=kubeconfig_path,
            kubeconfig_context=kubeconfig_context or None,
            project_id=project_id,
            service_account_path=service_account_path,
            proxmox_token_id=proxmox_token_id,
            verify_ssl=verify_ssl,
        )

        # Discover operations via SDK connector
        try:
            operations = _discover_sdk_operations(config)
        except Exception as exc:
            output_error(
                f"Failed to discover operations: {exc}",
                code="DISCOVERY_ERROR",
                suggestion=f"Check {connector_type} connector configuration.",
            )
            return

    else:
        output_error(
            f"Unknown connector type: {connector_type}",
            code="UNKNOWN_TYPE",
            suggestion=f"Supported types: {', '.join(sorted(SPEC_TYPES | SDK_TYPES))}.",
        )
        return

    # --- Common save pipeline (all types) ---

    # Build tag groups
    tag_counter: Counter = Counter()
    for op in operations:
        for tag in (op.tags if hasattr(op, "tags") else []):
            tag_counter[tag] += 1
    tag_groups = dict(tag_counter)

    # Determine auth_method display string for preview/skill
    display_auth = auth_method or "none"
    if connector_type == "kubernetes":
        display_auth = "kubeconfig"
    elif connector_type == "gcp":
        display_auth = "adc"

    # Dry-run preview and confirmation
    if not non_interactive:
        if not _confirm_save(len(operations), tag_groups, display_auth):
            output_response({"status": "cancelled"}, human=ctx.obj.human, start_time=start_time)
            return

    state_dir = settings.state_dir

    # 1. Save YAML config
    connectors_dir = state_dir / "connectors"
    connectors_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = save_connector_config(config, connectors_dir)

    # 2. Insert connector row into meho.db
    from meho_claude.core.database import get_connection

    connector_id = str(uuid.uuid4())
    db_path = state_dir / "meho.db"
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO connectors
               (id, name, connector_type, description, base_url, config_path, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (connector_id, name, connector_type, config.description, config.base_url, str(yaml_path), 1),
        )

        # 3. Insert operations into operations table
        for op in operations:
            tags_str = ",".join(op.tags) if hasattr(op, "tags") else ""
            input_schema_str = json.dumps(op.input_schema) if hasattr(op, "input_schema") else "{}"
            output_schema_str = json.dumps(op.output_schema) if hasattr(op, "output_schema") else "{}"
            example_params_str = (
                json.dumps(op.example_params) if hasattr(op, "example_params") else "{}"
            )
            related_ops_str = (
                json.dumps(op.related_operations)
                if hasattr(op, "related_operations")
                else "[]"
            )

            conn.execute(
                """INSERT OR REPLACE INTO operations
                   (connector_name, operation_id, display_name, description,
                    trust_tier, http_method, url_template,
                    input_schema, output_schema, tags, example_params, related_operations)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    op.connector_name,
                    op.operation_id,
                    op.display_name,
                    op.description or "",
                    op.trust_tier,
                    op.http_method or "",
                    op.url_template or "",
                    input_schema_str,
                    output_schema_str,
                    tags_str,
                    example_params_str,
                    related_ops_str,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    # 4. Store credentials (skip for kubernetes and gcp -- kubeconfig/ADC handle auth)
    if config.auth is not None and credential_name and auth_method:
        _store_credentials(state_dir, credential_name, auth_method, non_interactive)

    # 5. Generate and write skill file
    from meho_claude.core.skills import generate_skill_markdown, write_skill_file

    skill_content = generate_skill_markdown(
        connector_name=name,
        connector_type=connector_type,
        description=config.description,
        operation_count=len(operations),
        tag_groups=tag_groups,
        auth_method=display_auth,
        base_url=config.base_url,
    )
    skills_dir = state_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_path = write_skill_file(skills_dir, name, skill_content)

    # Output success
    output_response(
        {
            "status": "ok",
            "connector": name,
            "connector_type": connector_type,
            "operations": len(operations),
            "tag_groups": tag_groups,
            "skill_file": str(skill_path),
        },
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command("list")
def list_connectors(ctx: typer.Context) -> None:
    """List all configured connectors."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.database import get_connection
    from meho_claude.core.connectors.loader import load_all_configs

    connectors: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # Load from DB first
    db_path = state_dir / "meho.db"
    if db_path.exists():
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT name, connector_type, base_url, is_active FROM connectors ORDER BY name"
            ).fetchall()
            for row in rows:
                connectors.append(
                    {
                        "name": row["name"],
                        "type": row["connector_type"],
                        "base_url": row["base_url"] or "",
                        "is_active": bool(row["is_active"]),
                    }
                )
                seen_names.add(row["name"])
        finally:
            conn.close()

    # Merge YAML-only connectors not yet in DB
    connectors_dir = state_dir / "connectors"
    if connectors_dir.exists():
        yaml_configs = load_all_configs(connectors_dir)
        for cfg in yaml_configs:
            if cfg.name not in seen_names:
                connectors.append(
                    {
                        "name": cfg.name,
                        "type": cfg.connector_type,
                        "base_url": cfg.base_url,
                        "is_active": True,
                        "yaml_only": True,
                    }
                )

    output_response(
        {"status": "ok", "connectors": connectors, "count": len(connectors)},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def test(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Connector name to test."),
) -> None:
    """Test a connector connection."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.database import get_connection

    # Verify connector exists (DB or YAML)
    db_path = state_dir / "meho.db"
    found = False
    if db_path.exists():
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM connectors WHERE name = ?", (name,)
            ).fetchone()
            found = row is not None
        finally:
            conn.close()

    yaml_path = state_dir / "connectors" / f"{name}.yaml"
    if not found and not yaml_path.exists():
        output_error(
            f"Connector not found: {name}",
            code="CONNECTOR_NOT_FOUND",
            suggestion="Run 'meho connector list' to see available connectors.",
        )
        return

    # Test connection
    result = _test_connector_connection(state_dir, name)

    if result.get("status") == "error":
        output_error(
            f"Connection test failed for {name}: {result.get('message', 'unknown error')}",
            code="CONNECTION_FAILED",
            suggestion="Check the connector URL and credentials.",
        )
        return

    output_response(
        {
            "status": "ok",
            "connector": name,
            "status_code": result.get("status_code"),
            "response_time_ms": result.get("response_time_ms"),
        },
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command("search-ops")
def search_ops(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query for operations."),
    limit: int = typer.Option(10, "--limit", help="Maximum number of results."),
    connector: Optional[str] = typer.Option(
        None, "--connector", help="Filter by connector name."
    ),
) -> None:
    """Search available operations using hybrid search (BM25 + semantic)."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    from meho_claude.core.database import get_connection

    db_path = state_dir / "meho.db"
    conn = get_connection(db_path)
    try:
        results = _hybrid_search(
            conn, state_dir, query, limit=limit, connector_name=connector
        )
    finally:
        conn.close()

    # Format results for output
    formatted = []
    for r in results:
        formatted.append({
            "operation_id": r.get("operation_id", ""),
            "connector": r.get("connector_name", ""),
            "display_name": r.get("display_name", ""),
            "description": r.get("description", ""),
            "trust_tier": r.get("trust_tier", ""),
            "relevance_score": r.get("relevance_score", 0.0),
        })

    output_response(
        {
            "status": "ok",
            "query": query,
            "results": formatted,
            "count": len(formatted),
        },
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def call(
    ctx: typer.Context,
    connector_name: str = typer.Argument(..., help="Connector name."),
    operation_id: str = typer.Argument(..., help="Operation ID to call."),
    param: Optional[list[str]] = typer.Option(
        None, "--param", help="Parameter as key=value (repeatable)."
    ),
    confirmed: bool = typer.Option(False, "--confirmed", help="Confirm WRITE operations."),
    confirm: Optional[str] = typer.Option(
        None, "--confirm", help="Typed confirmation for DESTRUCTIVE operations."
    ),
) -> None:
    """Call a connector operation with trust model enforcement."""
    start_time = time.time()
    settings = _get_settings(ctx)
    state_dir = settings.state_dir

    # Parse --param key=value pairs into dict
    params: dict[str, str] = {}
    if param:
        for p in param:
            if "=" in p:
                key, value = p.split("=", 1)
                params[key] = value
            else:
                output_error(
                    f"Invalid param format: {p}. Expected key=value",
                    code="INVALID_PARAM",
                    suggestion="Use --param key=value format.",
                )
                return

    # Look up operation
    operation = _lookup_operation(state_dir, connector_name, operation_id)
    if operation is None:
        output_error(
            f"Operation not found: {connector_name}/{operation_id}",
            code="OPERATION_NOT_FOUND",
            suggestion="Run 'meho connector search-ops <query>' to find available operations.",
        )
        return

    # Check trust tier
    trust_result = enforce_trust(operation, params, confirmed=confirmed, confirm_text=confirm)
    if trust_result is not None:
        # Operation requires confirmation
        output_response(trust_result, human=ctx.obj.human, start_time=start_time)
        return

    # Execute the operation
    try:
        exec_result = _load_and_execute_operation(state_dir, connector_name, operation, params)
    except Exception as exc:
        # Log the failed attempt
        audit_log(
            state_dir / "audit.log",
            connector=connector_name,
            operation=operation_id,
            trust_tier=operation["trust_tier"],
            params=params,
            result_status="error",
        )
        # Context-aware suggestion based on error type
        err_lower = str(exc).lower()
        if any(kw in err_lower for kw in ("connection", "timeout", "refused")):
            suggestion = (
                "Check that the target system is reachable. "
                f"Run 'meho connector test {connector_name}' to verify connectivity."
            )
        elif any(kw in err_lower for kw in ("auth", "401", "403", "credential")):
            suggestion = (
                "Check credentials. "
                f"Run 'meho connector add --name {connector_name}' to reconfigure authentication."
            )
        else:
            suggestion = "Check connector config and credentials."

        output_error(
            f"Execution failed for {connector_name}/{operation_id}: {exc}",
            code="EXECUTION_ERROR",
            suggestion=suggestion,
        )
        return

    # Log successful execution
    audit_log(
        state_dir / "audit.log",
        connector=connector_name,
        operation=operation_id,
        trust_tier=operation["trust_tier"],
        params=params,
        result_status="success",
    )

    # Extract topology entities as side effect (silent -- never breaks flow)
    connector_type = _get_connector_type(state_dir, connector_name)
    if connector_type:
        _extract_topology(state_dir, connector_name, connector_type, operation_id, exec_result)

    # Check if response should be cached
    cache_summary = _check_and_cache(state_dir, connector_name, operation_id, exec_result)

    if cache_summary:
        output_response(cache_summary, human=ctx.obj.human, start_time=start_time)
    else:
        output_response(
            {"status": "ok", "result": exec_result},
            human=ctx.obj.human,
            start_time=start_time,
        )
