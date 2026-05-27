# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI exercise for the R4 local-Claude-as-triage example
(:mod:`examples/r4-local-claude`).

The example ships as docs + runnable JSON payloads under
``examples/r4-local-claude/`` (Initiative G11.6 #807 reference
pattern R4, Task G11.6-T4 #1083). Three failure modes the example
must guard against:

1. **Schema drift.** The runnable JSON payloads
   (:file:`agent.alert-triage.json`, :file:`scheduler.cron.json`,
   :file:`mcp.json.example`, :file:`toolset.json`, :file:`inputs.json`)
   hard-code field names that mirror the live Pydantic schemas
   (:class:`meho_backplane.agents.schemas.AgentDefinitionCreate`,
   :class:`meho_backplane.scheduler.schemas.ScheduledTriggerCreate`).
   If the schemas rename / drop / retype a field, the example
   silently rots until an operator copy-pastes it and the backend
   422s. The schema tests validate the JSON against the live
   schemas so the rot is loud at PR time.

2. **Prompt-encoded MCP call drift.** The agent's system prompt
   names an ``add_to_memory`` call by field; the live MCP tool's
   ``inputSchema`` is the wire contract the dispatcher enforces
   in :mod:`meho_backplane.mcp.handlers`. The prompt-vs-schema
   drift test extracts the prompt's documented call envelope and
   runs it through :func:`jsonschema.validate` against the live
   ``add_to_memory`` ``inputSchema`` -- a rename, retype, or enum
   tightening on the tool side fails the prompt example here at
   PR time rather than on the operator's first cron tick.

3. **Link rot.** The guide docs (:file:`README.md`, :file:`GUIDE.md`)
   reference siblings in ``docs/``, ``backend/src/``, ``cli/``, and
   ``deploy/`` via relative paths. If a referenced file moves /
   renames, the example points at vapour. The link test walks
   every relative link in the example's two markdown files and
   asserts the target path exists on disk.

The test is **runnable as a plain pytest** with no DB / network /
secrets -- it operates entirely on files in the repo tree, so it
runs in the same lane as :mod:`test_agents_schemas` and
:mod:`test_alembic_seed_*`. The CI workflow ``.github/workflows/ci.yml``
runs the full ``backend/tests/`` directory unconditionally on
every PR, which catches a schema drift / link rot regression the
same day it lands.

The example's *integration* smoke (the end-to-end handoff against
a live MEHO backplane + Keycloak + local Claude) requires the
integration stack and out-of-band credentials; running it from
this file would land as a vacuous-skip in CI (per the slim
skill's vacuous-skip rule). The schema + link + prompt-shape
checks here are the deterministic always-on gate; the end-to-end
verification chain is in :file:`examples/r4-local-claude/GUIDE.md`
Step 5 for the operator to walk by hand.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest

from meho_backplane.agents.schemas import AgentDefinitionCreate
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate


def _repo_root(start: Path) -> Path:
    """Walk up from the test file to the repo root.

    The R4 example lives at ``examples/r4-local-claude/`` under the
    repo root; the test file lives at ``backend/tests/`` under the
    same root. Locating the root via a stable marker keeps the
    test working under worktrees / out-of-tree builds where
    ``__file__`` cannot be assumed to share a prefix with the
    example.
    """
    here = start.resolve()
    for parent in (here, *here.parents):
        if (parent / "examples" / "r4-local-claude" / "README.md").exists():
            return parent
    raise RuntimeError("could not find repo root containing examples/r4-local-claude/README.md")


REPO_ROOT = _repo_root(Path(__file__))
EXAMPLE_DIR = REPO_ROOT / "examples" / "r4-local-claude"

#: Regex for a markdown link target: extracts the path inside ``](...)``.
#: Anchored on the closing bracket so it doesn't match image alt-text or
#: HTML-style anchors. Includes both link and image syntax.
_LINK_RE: re.Pattern[str] = re.compile(r"\]\((?P<target>[^)]+)\)")


def test_agent_definition_payload_parses_against_live_schema() -> None:
    """:file:`agent.alert-triage.json` validates against the current
    :class:`AgentDefinitionCreate` schema.

    The schema is the wire contract ``meho agent create`` and
    ``POST /api/v1/agents`` consume; a drift between this example
    and the schema means the operator's copy-paste 422s. The test
    asserts ``model_validate`` returns the typed object so a field
    rename in the schema flags the example at PR time, not at
    consumer-onboarding time.
    """
    payload_path = EXAMPLE_DIR / "agent.alert-triage.json"
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    parsed = AgentDefinitionCreate.model_validate(raw)
    # Tier is the load-bearing identifying property of the example
    # (Initiative #807 R4: "paired with a hosted cheap-tier"). Pin
    # it so a future "tweak the example to be more illustrative" PR
    # doesn't silently drop the tier hint.
    assert parsed.model_tier.value == "fast", (
        "R4 hosted agent must stay on the cheap (fast) tier; "
        "see Initiative #807 R4 -- the pairing's whole point is "
        "'cheap-tier triages, deep-tier on demand'."
    )
    # Three tool names are the contract the agent's system prompt
    # references. If a future PR drops one from the toolset but the
    # prompt still calls it, the runtime errors at fire time. Pin
    # the trio.
    allowed_tools = parsed.toolset.get("allowed", [])
    assert set(allowed_tools) == {
        "meho.broadcast.recent",
        "add_to_memory",
        "search_memory",
    }, (
        "R4 toolset must contain exactly the three handoff-channel "
        "tools the system prompt names; widening it grants the "
        "cheap-tier scopes it does not need; narrowing it breaks "
        "the prompt's named calls."
    )


def test_toolset_split_file_matches_agent_definition() -> None:
    """:file:`toolset.json` is the split-out subobject for ``--toolset @<path>``.

    GUIDE.md Step 1's ``meho agent create --toolset @examples/r4-local-claude/toolset.json``
    invocation relies on the CLI's ``loadJSONObjectFlag`` reading
    the file verbatim as the toolset value. If this file drifts
    from the embedded ``toolset`` object in
    ``agent.alert-triage.json``, the cron-fired agent's allowed
    tools no longer match what the prompt references.

    See ``cli/internal/cmd/agent/agent.go`` ``loadJSONObjectFlag``
    for the ``@<path>`` -> file-contents path; the JSON is parsed
    as the flag value, not the path string.
    """
    toolset_path = EXAMPLE_DIR / "toolset.json"
    agent_path = EXAMPLE_DIR / "agent.alert-triage.json"
    toolset_split = json.loads(toolset_path.read_text(encoding="utf-8"))
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    assert toolset_split == agent["toolset"], (
        "toolset.json must equal agent.alert-triage.json's toolset "
        "subobject byte-for-byte; the split file is what the GUIDE "
        "tells operators to pass to --toolset @<path>."
    )


def test_inputs_split_file_matches_scheduler_payload() -> None:
    """:file:`inputs.json` is the split-out subobject for ``--inputs @<path>``.

    GUIDE.md Step 2's ``meho scheduler create --inputs @examples/r4-local-claude/inputs.json``
    relies on the CLI's ``loadJSONObjectFlag`` reading the file
    verbatim as the inputs value. The file must equal the inputs
    subobject of ``scheduler.cron.json`` so the two payloads stay
    in lockstep.
    """
    inputs_path = EXAMPLE_DIR / "inputs.json"
    scheduler_path = EXAMPLE_DIR / "scheduler.cron.json"
    inputs_split = json.loads(inputs_path.read_text(encoding="utf-8"))
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    assert inputs_split == scheduler["inputs"], (
        "inputs.json must equal scheduler.cron.json's inputs "
        "subobject byte-for-byte; the split file is what the GUIDE "
        "tells operators to pass to --inputs @<path>."
    )


def test_scheduler_payload_parses_against_live_schema() -> None:
    """:file:`scheduler.cron.json` validates against
    :class:`ScheduledTriggerCreate` (with a placeholder UUID).

    The example ships the placeholder UUID ``00000000-...`` for
    ``agent_definition_id`` because the real id is bound at install
    time (by the operator's ``meho agent show ... | jq -r .id``).
    The schema accepts any UUID at parse time -- the FK check that
    rejects an orphan id is at the repository layer, which this
    test deliberately doesn't exercise.
    """
    payload_path = EXAMPLE_DIR / "scheduler.cron.json"
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    # Ensure the placeholder id is recognisably a placeholder so
    # operators don't think it's a real id to use verbatim.
    assert raw["agent_definition_id"] == "00000000-0000-0000-0000-000000000000"
    # Replace it with a fresh real UUID so the schema's UUID
    # validator passes -- the placeholder happens to be a valid
    # UUID, but pin the parse path with a non-degenerate value too.
    raw["agent_definition_id"] = str(uuid.uuid4())
    parsed = ScheduledTriggerCreate.model_validate(raw)
    assert parsed.kind.value == "cron", (
        "R4 trigger ships as cron because the kind=event dispatcher "
        "is not yet wired (see backend/src/meho_backplane/events/drain.py "
        "-- v0.2 no-op subscriber match). If a future PR flips this "
        "to event, update GUIDE.md Step 2 too."
    )
    # The shipped cron expression is the cheap-tier-friendly cadence
    # (every 15 minutes). Per-minute (``* * * * *``) burns one
    # cheap-tier round trip every minute on noise-free tenants and
    # contradicts the README framing -- pin to detect drift back to
    # the old, too-aggressive default.
    assert parsed.cron_expr == "*/15 * * * *"


def test_mcp_json_example_is_parseable_json_with_two_named_variants() -> None:
    """:file:`mcp.json.example` is valid JSON and documents both
    transport variants the GUIDE narrates.

    Claude Code's ``.mcp.json`` schema accepts top-level
    ``mcpServers`` keyed by server name; each entry is either a
    ``type=http`` direct-HTTP server or a stdio-shim spawn.
    Bot-readable schema-shape correctness here is the cheap proof
    that the example survives a JSON parse on a freshly-cloned
    consumer repo.
    """
    payload_path = EXAMPLE_DIR / "mcp.json.example"
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    assert "mcpServers" in raw, "Claude Code's .mcp.json requires a top-level mcpServers"
    servers = raw["mcpServers"]
    # The example deliberately ships two named variants so the
    # operator picks one and deletes the other. Pin both names.
    assert "_variant_a_direct_http" in servers
    assert "_variant_b_mcp_remote_shim" in servers
    variant_a = servers["_variant_a_direct_http"]
    assert variant_a.get("type") == "http"
    assert variant_a.get("url", "").endswith("/mcp"), (
        "Direct-HTTP variant must point at the canonical /mcp mount "
        "the backend serves from meho_backplane.mcp.server.router"
    )
    variant_b = servers["_variant_b_mcp_remote_shim"]
    assert variant_b.get("command") == "npx"
    # mcp-remote is the upstream stdio<->HTTP MCP shim; pin so a
    # future "replace with our own shim" PR has to update the doc
    # together.
    assert "mcp-remote" in variant_b.get("args", [])


def test_prompt_encoded_add_to_memory_call_validates_against_live_input_schema() -> None:
    """The agent prompt's documented ``add_to_memory`` call envelope
    validates against the live MCP tool ``inputSchema``.

    The R4 hosted agent fires every 15 minutes (per
    :file:`scheduler.cron.json`); each fire issues an
    ``add_to_memory`` call for each interesting event. The wire
    contract is enforced by ``meho_backplane.mcp.handlers`` via
    ``jsonschema.validate`` against the tool definition's
    ``inputSchema`` (registered in
    ``meho_backplane.mcp.tools.memory``). A drift between what the
    system prompt tells the agent to send and what the dispatcher
    accepts surfaces at the operator's first cron tick as
    ``-32602`` INVALID_PARAMS -- by which point the agent has
    burned the cheap-tier round trip on every event in the batch.

    This test reconstructs the prompt's documented call envelope
    as a Python dict literal (kept in lockstep with the prompt
    string in :file:`agent.alert-triage.json` by maintenance) and
    runs it through the same validator the dispatcher uses. The
    fixture mirrors the exact field set the prompt enumerates --
    scope/slug/body/ttl/tags -- so a rename or retype on the tool
    side (B1's failure mode: ``ttl_seconds`` instead of ``ttl``,
    ``r4-triage-handoff`` not in scope enum) fails the test at PR
    time.

    Importing :mod:`meho_backplane.mcp.tools.memory` runs its
    top-level ``register_mcp_tool`` calls; the tool registry is
    populated by the import side effect. ``get_tool`` then returns
    the live definition with its ``inputSchema`` -- no fixture
    duplication, no risk of the test schema drifting from the
    runtime schema.
    """
    # Importing the tools subpackage triggers the registry-population
    # side effect for every tool, including add_to_memory. The same
    # side-effect path the production lifespan walks via
    # ``eager_import_mcp_modules``.
    import meho_backplane.mcp.tools.memory  # noqa: F401  -- import for side effect
    from meho_backplane.mcp.registry import get_tool

    entry = get_tool("add_to_memory")
    assert entry is not None, (
        "add_to_memory tool is not registered; the prompt cannot "
        "rely on it. Did the tool module rename or move?"
    )
    defn, _handler = entry
    input_schema = defn.inputSchema

    # The fixture below MUST stay in lockstep with the field set the
    # prompt enumerates in agent.alert-triage.json's `system_prompt`.
    # If the prompt names a new field or drops one, update both
    # places together.
    prompt_call_envelope = {
        "scope": "tenant",
        "slug": "r4-handoff-evt-12345678-1234-1234-1234-123456789abc",
        "body": (
            "2026-05-27T14:22:01Z op=vsphere.vm.create who=op-a "
            "what=created prod VM cluster-7 why=prod write op "
            "next=verify ownership against tenant policy."
        ),
        "ttl": "P7D",
        "tags": ["r4-triage-handoff"],
    }
    # Pinned to Draft202012Validator to mirror the dispatcher's pin
    # at ``mcp/handlers.py`` -- the test fails identically to how
    # the production call would fail if a field drifts.
    jsonschema.validate(
        instance=prompt_call_envelope,
        schema=input_schema,
        cls=jsonschema.Draft202012Validator,
    )

    # Additionally pin the load-bearing constraints so a schema
    # widening (e.g. accepting an integer ttl) doesn't silently
    # accept a prompt regression -- the explicit assertions catch
    # the B1 failure shapes loudly.
    scope_enum = input_schema["properties"]["scope"]["enum"]
    assert "tenant" in scope_enum, (
        "add_to_memory.inputSchema scope enum dropped 'tenant'; "
        "the R4 prompt's chosen scope is no longer valid -- this "
        "PR must either pick a different scope or restore the enum."
    )
    assert prompt_call_envelope["scope"] in scope_enum, (
        "Prompt scope must be in the live scope enum; B1's failure "
        "mode was 'r4-triage-handoff' which is not a MemoryScope."
    )
    # The tool's TTL field is named `ttl` (ISO 8601). Asserting the
    # presence pins B1's other regression -- the original prompt
    # said `ttl_seconds: 604800` which the schema rejects.
    assert "ttl" in input_schema["properties"], (
        "add_to_memory.inputSchema must carry a 'ttl' property; "
        "the prompt encodes 'ttl: \"P7D\"' verbatim. If the field "
        "renames, update the prompt in lockstep."
    )
    assert "ttl_seconds" not in input_schema["properties"], (
        "add_to_memory.inputSchema must NOT carry 'ttl_seconds' -- "
        "if it does, the tool grew a second TTL field and the "
        "prompt's contract is ambiguous; decide which one wins and "
        "update the prompt."
    )


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
        # Skip anchor-only links (``#section``) and absolute URLs.
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # Skip GitHub-issue-style references inside link text.
        if target.startswith("javascript:"):
            continue
        # Strip a fragment so ``../foo.md#bar`` resolves to ``../foo.md``.
        target_without_fragment = target.split("#", 1)[0]
        if not target_without_fragment:
            # Pure-fragment link after the split (anchor-only).
            continue
        targets.append(target_without_fragment)
    return targets


@pytest.mark.parametrize(
    "doc_filename",
    ["README.md", "GUIDE.md"],
)
def test_example_doc_relative_links_resolve(doc_filename: str) -> None:
    """Every relative link in the R4 docs resolves to an on-disk path.

    Walks the doc's body, extracts ``[text](relative/path)`` link
    targets, and asserts each path exists relative to the doc's
    directory. Fragment-only links (``#section``) and absolute
    URLs are skipped -- this test catches *link rot* (the file
    moved or renamed) not *anchor drift* (the section heading
    changed). Anchor drift is rarer and a per-doc concern; this
    sweep is the always-on file-existence floor.
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
