# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI exercise for the R4 local-Claude-as-triage example
(:mod:`examples/r4-local-claude`).

The example ships as docs + runnable JSON payloads under
``examples/r4-local-claude/`` (Initiative G11.6 #807 reference
pattern R4, Task G11.6-T4 #1083). Two failure modes the example
must guard against:

1. **Schema drift.** The runnable JSON payloads
   (:file:`agent.alert-triage.json`, :file:`scheduler.cron.json`,
   :file:`mcp.json.example`) hard-code field names that mirror
   the live Pydantic schemas
   (:class:`meho_backplane.agents.schemas.AgentDefinitionCreate`,
   :class:`meho_backplane.scheduler.schemas.ScheduledTriggerCreate`).
   If the schemas rename / drop / retype a field, the example
   silently rots until an operator copy-pastes it and the backend
   422s. This test validates the JSON against the live schemas so
   the rot is loud at PR time.

2. **Link rot.** The guide docs (:file:`README.md`, :file:`GUIDE.md`)
   reference siblings in ``docs/``, ``backend/src/``, ``cli/``, and
   ``deploy/`` via relative paths. If a referenced file moves /
   renames, the example points at vapour. This test walks every
   relative link in the example's two markdown files and asserts
   the target path exists on disk.

The test is **runnable as a plain pytest** with no DB / network /
secrets — it operates entirely on files in the repo tree, so it
runs in the same lane as :mod:`test_agents_schemas` and
:mod:`test_alembic_seed_*`. The CI workflow ``.github/workflows/ci.yml``
runs the full ``backend/tests/`` directory unconditionally on
every PR, which catches a schema drift / link rot regression the
same day it lands.

The example's *integration* smoke (the end-to-end handoff against
a live MEHO backplane + Keycloak + local Claude) requires the
integration stack and out-of-band credentials; running it from
this file would land as a vacuous-skip in CI (per the slim
skill's vacuous-skip rule). The schema + link checks here are
the deterministic always-on gate; the end-to-end verification
chain is in :file:`examples/r4-local-claude/GUIDE.md` Step 5
for the operator to walk by hand.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

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
    # The cron expression is part of the GUIDE's recommended cadence
    # tuning -- changing it here changes the operator's expected
    # firing rhythm. Pin to detect drift.
    assert parsed.cron_expr == "* * * * *"


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
