"""Skill markdown generation per connector and MEHO skill definitions.

Skill files are instruction manuals (NOT operation dumps) per CONTEXT.md
critical decision. They teach Claude Code how to use the CLI for that system.

MEHO skill definitions generate SKILL.md files for Claude Code slash command
registration via `meho init`. Five skills: /meho:diagnose, /meho:connect,
/meho:topology, /meho:knowledge, /meho:memory.
"""

from __future__ import annotations

from pathlib import Path


def generate_skill_markdown(
    connector_name: str,
    connector_type: str,
    description: str,
    operation_count: int,
    tag_groups: dict[str, int],
    auth_method: str,
    base_url: str,
) -> str:
    """Generate a skill markdown file for a connector.

    The skill file is an instruction manual teaching Claude Code how to
    interact with this connector via the meho CLI.

    Args:
        connector_name: Name of the connector.
        connector_type: Type (e.g., "rest", "kubernetes").
        description: Human-readable description of the connector.
        operation_count: Total number of discovered operations.
        tag_groups: Dict mapping tag name -> operation count.
        auth_method: Authentication method (e.g., "bearer", "basic").
        base_url: Base URL of the connector.

    Returns:
        Markdown string.
    """
    tag_lines = "\n".join(
        f"  - **{tag}**: {count} operations" for tag, count in sorted(tag_groups.items())
    )

    return f"""# {connector_name} Connector Skill

## Quick Reference

| Property | Value |
|----------|-------|
| **Name** | {connector_name} |
| **Type** | {connector_type} |
| **Base URL** | {base_url} |
| **Auth** | {auth_method} |
| **Operations** | {operation_count} |
| **Description** | {description} |

### Tag Groups
{tag_lines}

## How to Use

### Search for Operations

Find available operations by keyword:

```bash
meho connector search-ops "list pods" --connector {connector_name}
```

### Call an Operation

Execute a discovered operation:

```bash
meho connector call {connector_name} <operation_id> --params '{{"key": "value"}}'
```

### Query Cached Results

After a call that caches data, query with SQL:

```bash
meho data query "SELECT * FROM {connector_name}_<operation> LIMIT 10"
```

## Trust Model

Operations are classified into three trust tiers:

- **READ** (auto-execute): GET/HEAD/OPTIONS requests return results directly
- **WRITE** (confirmation required): POST/PUT/PATCH requests require user confirmation before execution
- **DESTRUCTIVE** (explicit confirmation): DELETE requests require the user to type the exact resource name

Trust tiers can be overridden per-operation in the connector YAML config.

## Common Workflows

1. **Discover what is available**: `meho connector search-ops "<query>" --connector {connector_name}`
2. **Read data**: `meho connector call {connector_name} <read_operation>`
3. **Modify resources**: `meho connector call {connector_name} <write_operation> --params '...'` (will prompt for confirmation)
4. **Analyze results**: `meho data query "SELECT ..."` (when results are cached in DuckDB)
"""


def write_skill_file(skills_dir: Path, connector_name: str, content: str) -> Path:
    """Write skill markdown to the skills directory.

    Args:
        skills_dir: Directory to write the skill file to (e.g., ~/.meho/skills/).
        connector_name: Name of the connector (used as filename).
        content: Markdown content to write.

    Returns:
        Path to the written file.
    """
    file_path = skills_dir / f"{connector_name}.md"
    file_path.write_text(content)
    return file_path


# ---------------------------------------------------------------------------
# MEHO skill definitions for Claude Code slash command registration
# ---------------------------------------------------------------------------


def _diagnose_skill() -> str:
    """Return SKILL.md content for /meho:diagnose."""
    return """\
---
name: meho:diagnose
description: >-
  Diagnose infrastructure problems across connected systems (Kubernetes, VMware,
  Proxmox, GCP, SOAP/REST APIs). Use when investigating errors, performance issues,
  outages, pod restarts, VM problems, or any infrastructure question that might
  span multiple systems.
argument-hint: "[describe the problem or entity to investigate]"
---

# MEHO Cross-System Diagnosis

You are running a structured diagnostic investigation across connected infrastructure systems.

## Visual Input

If the problem is visible -- a dashboard showing errors, an error page in a browser,
log output in a terminal, a monitoring alert, or an architecture diagram -- provide a
screenshot or document now. Paste with Cmd+V (macOS) or drag the file into this
conversation. Visual context helps identify entity names, error codes, and affected
systems faster.

You can also provide architecture docs, runbooks, or other documents for investigation
context.

If the user provides visual input, extract entity names, error codes, IP addresses,
and status indicators. Use these as starting points for your investigation.

## Connected Systems

!`meho connector list 2>/dev/null || echo "No connectors configured. Run /meho:connect to add one."`

## Investigation Workflow

Follow the diagnosis workflow template step by step:

!`meho workflow run diagnose 2>/dev/null || echo "Workflow template not found. Run meho init to install workflows."`

## Investigation Target

$ARGUMENTS

## Critical Rules

1. ONLY execute READ operations -- never run WRITE or DESTRUCTIVE operations during diagnosis
2. Narrate each investigation step: "Checking X..." "Found Y..." "Following SAME_AS edge to Z..."
3. Use `meho topology lookup` to find entities and traverse relationships, including SAME_AS correlations
4. Use `meho connector call` to gather evidence from each system
5. Follow SAME_AS correlations to trace problems across system boundaries
6. Include confidence levels (high/medium/low) for each finding
7. Recommend actions as exact `meho connector call` commands but DO NOT execute WRITE or DESTRUCTIVE actions
"""


def _connect_skill() -> str:
    """Return SKILL.md content for /meho:connect."""
    return """\
---
name: meho:connect
description: >-
  Add a new infrastructure connector (Kubernetes, VMware vSphere, Proxmox,
  GCP, REST API, SOAP/WSDL). Use when the user wants to connect a new system,
  add a cluster, or set up API access.
argument-hint: "[connector type or system name]"
---

# Add MEHO Connector

Help the user add a new infrastructure connector.

## Currently Connected

!`meho connector list 2>/dev/null || echo "No connectors configured yet."`

## Add a Connector

Run the interactive connector wizard:

```bash
meho connector add
```

Or add via flags for non-interactive setup:

```bash
meho connector add --type <rest|soap|kubernetes|vmware|proxmox|gcp> --name <name> --url <url> --spec <openapi-spec> --auth-method <bearer|basic|api_key>
```

### Connector Types

- **rest**: REST/OpenAPI API -- needs base URL + OpenAPI spec + auth credentials
- **soap**: SOAP/WSDL service -- needs WSDL URL + auth credentials
- **kubernetes**: Kubernetes cluster -- needs kubeconfig path
- **vmware**: VMware vSphere -- needs vCenter URL + credentials
- **proxmox**: Proxmox VE -- needs cluster URL + API token or credentials
- **gcp**: Google Cloud Platform -- needs project ID + service account or ADC

After adding, test the connection:

```bash
meho connector test <name>
```

$ARGUMENTS
"""


def _topology_skill() -> str:
    """Return SKILL.md content for /meho:topology."""
    return """\
---
name: meho:topology
description: >-
  Look up infrastructure entities and their relationships across connected
  systems. Use when investigating topology, relationships, or cross-system
  correlations.
argument-hint: "[entity name or description]"
---

# MEHO Topology Lookup

Look up entities and explore relationships across your connected infrastructure.

## Usage

Search for an entity by name or description:

```bash
meho topology lookup "<entity name or keyword>"
```

### Depth Parameter

Explore relationships to a given depth (default 1):

```bash
meho topology lookup "<entity>" --depth 2
```

Depth 2+ shows cross-system SAME_AS correlations and indirect relationships.

### Correlation Display

The output includes:
- **Entity details**: type, connector, scope
- **Relationships**: parent/child, runs_on, contains, exposes
- **SAME_AS correlations**: cross-system entity mappings (confirmed and pending)

### Correlations

View and manage cross-system SAME_AS correlations:

```bash
meho topology correlate
```

Confirm or reject pending correlations:

```bash
meho topology correlate --confirm <correlation-id>
meho topology correlate --reject <correlation-id>
```

$ARGUMENTS
"""


def _knowledge_skill() -> str:
    """Return SKILL.md content for /meho:knowledge."""
    return """\
---
name: meho:knowledge
description: >-
  Search and manage the infrastructure knowledge base. Ingest documents (PDF, HTML,
  Markdown), search ingested knowledge with hybrid BM25+semantic search, and manage
  knowledge sources. Use when the user wants to add documentation, runbooks, or
  architecture docs to the knowledge base, or search for relevant information.
argument-hint: "[search query or file to ingest]"
---

# MEHO Knowledge Base

Manage and search your infrastructure knowledge base. Ingested documents are chunked,
indexed for BM25 full-text search, and embedded for semantic similarity search.

## Current Knowledge State

!`meho knowledge stats 2>/dev/null || echo "No knowledge sources ingested yet."`

## Usage

### Ingest a Document

Add a PDF, HTML, or Markdown file to the knowledge base:

```bash
meho knowledge ingest <file-path> --connector <connector-name>
```

Omit `--connector` for global (cross-system) knowledge.

Supported formats: `.md`, `.html`, `.htm`, `.pdf`

### Search Knowledge

Search for relevant knowledge using natural language:

```bash
meho knowledge search "<query>" --connector <connector-name> --limit 5
```

Results are ranked by hybrid BM25 + semantic similarity (Reciprocal Rank Fusion).
Omit `--connector` to search across all connectors.

### Remove a Source

Remove a previously ingested file:

```bash
meho knowledge remove <filename> --connector <connector-name>
```

### Rebuild Index

Re-embed all chunks into ChromaDB (useful after upgrades):

```bash
meho knowledge rebuild
```

### View Statistics

See ingested source and chunk counts:

```bash
meho knowledge stats
```

## When to Use

- User asks about architecture, runbooks, or operational procedures
- User wants to add documentation for a connected system
- During diagnosis, search for known issues or troubleshooting guides
- User wants to check what documentation is already ingested

$ARGUMENTS
"""


def _memory_skill() -> str:
    """Return SKILL.md content for /meho:memory."""
    return """\
---
name: meho:memory
description: >-
  Store, search, and manage connector-scoped memories. Memories capture patterns,
  resolutions, and investigation findings that persist across sessions. Use when
  the user wants to remember something, recall a previous finding, or search
  for past investigation context.
argument-hint: "[search query or memory text to store]"
---

# MEHO Memory

Store and recall investigation findings, patterns, and resolutions across sessions.
Memories are indexed for BM25 full-text search and embedded for semantic similarity.

## Recent Memories

!`meho memory list 2>/dev/null | head -20 || echo "No memories stored yet."`

## Usage

### Store a Memory

Save a finding, pattern, or resolution:

```bash
meho memory store "<text>" --connector <connector-name> --tags "tag1,tag2"
```

Omit `--connector` for global (cross-system) memories.
Tags are optional comma-separated labels for organization.

### Search Memories

Search for relevant past findings:

```bash
meho memory search "<query>" --connector <connector-name> --limit 5
```

Results are ranked by hybrid BM25 + semantic similarity.
Omit `--connector` to search across all connectors.

### List Memories

View all stored memories:

```bash
meho memory list --connector <connector-name>
```

### Forget a Memory

Remove a specific memory by ID:

```bash
meho memory forget <memory-id>
```

## When to Use

- After resolving an issue: store the root cause and resolution
- During diagnosis: search for similar past investigations
- When spotting a pattern: store it for future reference
- User explicitly asks to remember or recall something

## Best Practices for Storing Memories

- Include the **problem**, **root cause**, and **resolution** in one memory
- Tag with categories: `pattern`, `resolution`, `gotcha`, `architecture`
- Scope to a connector when the memory is system-specific
- Keep memories concise but complete (1-3 sentences)

$ARGUMENTS
"""


def get_meho_skill_definitions() -> list[tuple[str, str]]:
    """Return MEHO skill definitions for Claude Code slash command registration.

    Each tuple is (directory_name, skill_content) where directory_name is the
    subdirectory under .claude/skills/ and skill_content is the full SKILL.md
    file content with YAML frontmatter.

    Returns:
        List of 5 (dir_name, content) tuples for all MEHO skills.
    """
    return [
        ("meho-diagnose", _diagnose_skill()),
        ("meho-connect", _connect_skill()),
        ("meho-topology", _topology_skill()),
        ("meho-knowledge", _knowledge_skill()),
        ("meho-memory", _memory_skill()),
    ]


def write_meho_skills(
    skills_dir: Path, force: bool = False
) -> list[dict[str, str]]:
    """Write MEHO SKILL.md files to the target directory.

    Creates subdirectories under skills_dir for each skill and writes
    SKILL.md files. Skips existing files unless force=True.

    Args:
        skills_dir: Target directory (e.g., .claude/skills/).
        force: If True, overwrite existing SKILL.md files.

    Returns:
        List of result dicts with name, status (created/skipped/overwritten),
        and path for each skill.
    """
    results: list[dict[str, str]] = []

    for dir_name, content in get_meho_skill_definitions():
        skill_dir = skills_dir / dir_name
        skill_file = skill_dir / "SKILL.md"

        if skill_file.exists() and not force:
            results.append({
                "name": dir_name,
                "status": "skipped",
                "path": str(skill_file),
            })
            continue

        status = "overwritten" if skill_file.exists() else "created"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content)
        results.append({
            "name": dir_name,
            "status": status,
            "path": str(skill_file),
        })

    return results
