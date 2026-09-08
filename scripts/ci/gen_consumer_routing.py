#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render the public MEHO-first routing vehicles from one source contract.

The routing discipline ("prefer MEHO before any work") is authored once, in
``docs/examples/consumer-onboarding/contract/meho-first-routing.md``. This
generator renders it into the two public delivery vehicles so they can never
drift from each other or from the source:

* the Claude Code plugin skills
  (``clients/claude-code-plugin/skills/{prefer-meho,operations,knowledge,
  memory,broadcast}/SKILL.md``), and
* the Layer-2 copy-merge starter
  (``docs/examples/consumer-onboarding/CLAUDE.md``), authoritative for
  non-plugin clients.

The source carries the routing PROSE as labelled fragments; this script
carries the per-vehicle FRAMING (frontmatter, headers, the copy-merge
banner) and the assembly order. It is the versioned "ESLint shareable
config" published once and rendered per vehicle, never copied by hand.

Usage::

    python scripts/ci/gen_consumer_routing.py            # write the files
    python scripts/ci/gen_consumer_routing.py --check     # CI: fail on drift

``--check`` renders in memory and byte-compares against the committed files,
exiting non-zero (and naming the stale files) when any rendering has been
hand-edited. The pinned drift test
``backend/tests/test_consumer_routing_render.py`` drives the same comparison
so the guard runs in the unit lane.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Repo-relative path to the single source contract.
SOURCE_REL: str = "docs/examples/consumer-onboarding/contract/meho-first-routing.md"

#: Repo root resolved from this file (``scripts/ci/<this>`` -> ``parents[2]``).
REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]

_FRAGMENT_RE: re.Pattern[str] = re.compile(
    r"<!-- fragment:START (?P<id>[a-z0-9-]+) -->\n(?P<body>.*?)\n<!-- fragment:END (?P=id) -->",
    re.DOTALL,
)

_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{1,6})(\s.*)$")
_FENCE_RE: re.Pattern[str] = re.compile(r"^\s*```")

_GENERATED_NOTE: str = (
    "GENERATED FILE — DO NOT EDIT.\n"
    "Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md\n"
    "by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run\n"
    "the generator; backend/tests/test_consumer_routing_render.py fails CI on drift."
)


def parse_fragments(source_text: str) -> dict[str, str]:
    """Return ``{fragment_id: body}`` parsed from the source contract.

    Bodies are stripped of surrounding blank lines; headings stay authored
    at their source level (the recipe shifts them where a vehicle nests a
    fragment under a parent section).
    """
    fragments: dict[str, str] = {}
    for match in _FRAGMENT_RE.finditer(source_text):
        fragments[match.group("id")] = match.group("body").strip()
    if not fragments:
        raise ValueError(f"no fragments parsed from {SOURCE_REL}; source format changed")
    return fragments


def shift_headings(markdown: str, by: int) -> str:
    """Deepen every ATX heading by *by* levels, skipping fenced code blocks.

    A ``#`` that opens a bash comment inside a ```` ``` ```` fence (the
    day-0 recipe's numbered steps) is never treated as a heading.
    """
    if by == 0:
        return markdown
    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        match = _HEADING_RE.match(line)
        if match and not in_fence:
            level = min(len(match.group(1)) + by, 6)
            out.append("#" * level + match.group(2))
        else:
            out.append(line)
    return "\n".join(out)


@dataclass(frozen=True)
class Block:
    """One body block in a vehicle recipe: a source fragment or a literal."""

    fragment: str | None = None
    literal: str | None = None
    shift: int = 0

    def render(self, fragments: dict[str, str]) -> str:
        if self.literal is not None:
            return self.literal.strip()
        if self.fragment is None:
            raise ValueError("Block needs either a fragment id or a literal")
        if self.fragment not in fragments:
            raise KeyError(f"unknown fragment id: {self.fragment!r}")
        return shift_headings(fragments[self.fragment], self.shift).strip()


@dataclass(frozen=True)
class Vehicle:
    """A rendered output: its repo-relative path, header, and body blocks."""

    path: str
    header: str
    blocks: Sequence[Block] = field(default_factory=tuple)

    def render(self, fragments: dict[str, str]) -> str:
        rendered = [self.header.strip()]
        rendered.extend(block.render(fragments) for block in self.blocks)
        return "\n\n".join(rendered).rstrip() + "\n"


def _skill_header(name: str, description: str, title: str, intro: str) -> str:
    """Frontmatter + generated banner + H1 + intro for a plugin skill."""
    return (
        f"---\nname: {name}\ndescription: >\n{description}\n---\n\n"
        f"<!--\n{_GENERATED_NOTE}\n-->\n\n"
        f"# {title}\n\n{intro}"
    )


def _wrap_description(text: str) -> str:
    """Indent a one-paragraph skill description under the folded-scalar key."""
    return "\n".join(f"  {line}" for line in text.split("\n"))


_PREFER_DESC = _wrap_description(
    "MEHO-first routing for any work in a repo wired to a MEHO backplane:\n"
    "which governed surface serves each evidence or execution need, and how\n"
    "to fall back safely. Use at the start of every session and whenever you\n"
    "are about to read, change, look up, remember, coordinate, or run a\n"
    "procedure — prefer MEHO MCP tools and CLI verbs over local script\n"
    "wrappers, raw API calls, or local files unless explicitly told otherwise."
)

_OPERATIONS_DESC = _wrap_description(
    "Prefer MEHO surfaces to inspect and operate against infrastructure in a\n"
    "MEHO-wired repo. Use when acting on any target, reading live state,\n"
    "querying inventory or topology, or reviewing operational history —\n"
    "reach for `search_operations` / `call_operation`, per-connector\n"
    "`meho <connector> …` verbs, `list_targets` / `query_topology`, and\n"
    "`meho audit …` over local wrappers, raw API calls, or local files."
)

_KNOWLEDGE_DESC = _wrap_description(
    "Prefer MEHO knowledge and the capability-gated vendor-docs corpus for\n"
    "finding or recording facts in a MEHO-wired repo. Use when searching for\n"
    "operational facts or prior findings, recording a new fact, or answering\n"
    "a vendor- or version-specific question — reach for `search_knowledge` /\n"
    "`add_to_knowledge` and `list_doc_collections` / `search_docs` /\n"
    "`ask_docs` instead of `grep`-ing a local `kb/` or answering from memory."
)

_MEMORY_DESC = _wrap_description(
    "Prefer MEHO memory for operator preferences and durable notes in a\n"
    "MEHO-wired repo. Use when recording a behavioural preference, an\n"
    "operator note, or team-shared knowledge that should follow the operator\n"
    "or tenant across machines — reach for `add_to_memory` / `meho remember`\n"
    "/ `meho memory …` instead of writing to a local memory file."
)

_BROADCAST_DESC = _wrap_description(
    "Cross-operator awareness discipline for a MEHO-wired repo. Use before,\n"
    "during, and after working on any target: check the live broadcast feed\n"
    "for conflicting activity before starting, announce intent, check in\n"
    "during long work, and report on completion so other operators watching\n"
    "the feed see your work in real time."
)

_CLAUDE_HEADER = f"""<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group

{_GENERATED_NOTE}

This file is the **MEHO Layer-2 starter template**. Copy it into your
consumer repo's root as `CLAUDE.md` (or merge it with an existing
CLAUDE.md). It tells any local Claude Code session that opens your repo to
prefer MEHO surfaces over per-machine fallbacks.

Claude Code users: install the versioned plugin instead of copy-merging —
`claude plugin marketplace add evoila/meho` then `/plugin install meho@meho`
carries the same routing discipline as skills, refreshed on upgrade. This
template remains authoritative for non-plugin clients (Cline, Continue, CI
bots) that read a `CLAUDE.md`.

Source of truth:
  https://github.com/evoila/meho/blob/main/docs/examples/consumer-onboarding/contract/meho-first-routing.md
The onboarding guide next to it (`ONBOARDING.md`) walks the install + verify
path.
-->

# CLAUDE.md — MEHO-first operations

This repo uses [MEHO](https://github.com/evoila/meho) for infrastructure
operations. When you (Claude Code or another local agent) operate here,
**prefer MEHO surfaces over local fallbacks** unless explicitly told
otherwise. The routing discipline below is rendered from the
[MEHO-first routing contract](./contract/meho-first-routing.md); edit the
contract and regenerate, never hand-edit this file."""

_PREFERRED_SURFACES_INTRO = """## Preferred MEHO surfaces

The routing table above keys each need to a governed surface. The sections
below give the concrete MCP tools and CLI verbs per area."""

_VERSIONING_TEMPLATE = """## Versioning

This template is rendered from a versioned contract that rides MEHO
releases. After upgrading the CLI (`meho version` reports the client
version, `meho status` the backplane version), re-pull this file from
upstream and merge the diff against your tenant-specific customisations
below the marker. The
[onboarding guide](https://github.com/evoila/meho/blob/main/docs/examples/consumer-onboarding/ONBOARDING.md)
walks the refresh procedure."""

_TENANT_MARKER = """<!-- Add tenant-specific or repo-specific rules below this marker.
     Keep the canonical Layer-2 routing rules above untouched so
     diffs against upstream stay clean. -->"""

_PLUGIN_VERSIONING = """## Versioning

This plugin's version rides MEHO releases (see `.claude-plugin/plugin.json`).
After a MEHO upgrade, re-install the plugin (`/plugin install meho@meho`) to
pick up refreshed routing rules — this replaces the copy-and-merge template
refresh for Claude Code consumers."""


def _skill_vehicles() -> tuple[Vehicle, ...]:
    """The prefer-meho / operations / knowledge plugin skill renderings."""
    return (
        Vehicle(
            path="clients/claude-code-plugin/skills/prefer-meho/SKILL.md",
            header=_skill_header(
                "prefer-meho",
                _PREFER_DESC,
                "MEHO-first operations",
                "This repo operates infrastructure **through** a MEHO backplane. Prefer "
                "MEHO surfaces over local fallbacks unless the operator explicitly says "
                "otherwise. Each area has its own skill (`meho:operations`, "
                "`meho:knowledge`, `meho:memory`, `meho:broadcast`) with the concrete "
                "verbs; this skill is the routing spine they share.",
            ),
            blocks=(
                Block(fragment="why"),
                Block(fragment="discovery"),
                Block(fragment="route-table"),
                Block(fragment="route-notes"),
                Block(fragment="evidence-quality"),
                Block(fragment="close-loop"),
                Block(fragment="constraints"),
                Block(fragment="break-glass"),
                Block(fragment="stays-local"),
                Block(literal=_PLUGIN_VERSIONING),
            ),
        ),
        Vehicle(
            path="clients/claude-code-plugin/skills/operations/SKILL.md",
            header=_skill_header(
                "operations",
                _OPERATIONS_DESC,
                "Operations — prefer MEHO verbs",
                "Every operation through MEHO is authenticated, policy-checked, audited, "
                "and broadcast. Prefer MEHO verbs over local script wrappers and raw API "
                "calls. See `meho:prefer-meho` for the full route-by-evidence-need table.",
            ),
            blocks=(
                Block(fragment="ops-connectors"),
                Block(fragment="ops-generic"),
                Block(fragment="ops-targets-topology"),
                Block(fragment="ops-audit"),
                Block(fragment="break-glass"),
            ),
        ),
        Vehicle(
            path="clients/claude-code-plugin/skills/knowledge/SKILL.md",
            header=_skill_header(
                "knowledge",
                _KNOWLEDGE_DESC,
                "Knowledge and vendor docs — prefer MEHO",
                "Prefer the MEHO knowledge base and the capability-gated vendor-docs "
                "corpus over local `kb/` files and training-data recall. See "
                "`meho:prefer-meho` for the full route-by-evidence-need table.",
            ),
            blocks=(
                Block(fragment="kb-find"),
                Block(fragment="kb-record"),
                Block(fragment="kb-docs"),
                Block(fragment="evidence-quality"),
            ),
        ),
    )


def _more_skill_vehicles() -> tuple[Vehicle, ...]:
    """The memory + broadcast plugin skill renderings."""
    return (
        Vehicle(
            path="clients/claude-code-plugin/skills/memory/SKILL.md",
            header=_skill_header(
                "memory",
                _MEMORY_DESC,
                "Memory — prefer MEHO",
                "MEHO memory carries operator preferences and durable notes across "
                "machines and scopes them correctly. Prefer it over per-laptop local "
                "memory files. See `meho:prefer-meho` for the full "
                "route-by-evidence-need table.",
            ),
            blocks=(
                Block(fragment="mem-record"),
                Block(fragment="mem-manage"),
                Block(fragment="close-loop"),
            ),
        ),
        Vehicle(
            path="clients/claude-code-plugin/skills/broadcast/SKILL.md",
            header=_skill_header(
                "broadcast",
                _BROADCAST_DESC,
                "Broadcast — cross-operator awareness",
                "MEHO carries a per-tenant live feed of operator activity; other "
                "operators may be watching it and will see your work in real time. "
                "Follow the discipline below on every session. See `meho:prefer-meho` "
                "for the full route-by-evidence-need table.",
            ),
            blocks=(
                Block(fragment="bcast-discipline"),
                Block(fragment="bcast-read"),
                Block(fragment="bcast-trust"),
            ),
        ),
    )


def _vehicles() -> tuple[Vehicle, ...]:
    """All six renderings: the plugin skills plus the Layer-2 template."""
    return (
        *_skill_vehicles(),
        *_more_skill_vehicles(),
        Vehicle(
            path="docs/examples/consumer-onboarding/CLAUDE.md",
            header=_CLAUDE_HEADER,
            blocks=(
                Block(fragment="why"),
                Block(fragment="discovery"),
                Block(fragment="route-table"),
                Block(fragment="route-notes"),
                Block(literal=_PREFERRED_SURFACES_INTRO),
                Block(fragment="kb-find", shift=1),
                Block(fragment="kb-record", shift=1),
                Block(fragment="kb-docs", shift=1),
                Block(fragment="mem-record", shift=1),
                Block(fragment="mem-manage", shift=1),
                Block(fragment="ops-connectors", shift=1),
                Block(fragment="ops-generic", shift=1),
                Block(fragment="ops-targets-topology", shift=1),
                Block(fragment="linux-day0", shift=1),
                Block(fragment="ops-audit", shift=1),
                Block(fragment="bcast-discipline", shift=1),
                Block(fragment="bcast-read", shift=1),
                Block(fragment="bcast-trust", shift=1),
                Block(fragment="evidence-quality"),
                Block(fragment="close-loop"),
                Block(fragment="constraints"),
                Block(fragment="break-glass"),
                Block(fragment="stays-local"),
                Block(literal=_VERSIONING_TEMPLATE),
                Block(literal=_TENANT_MARKER),
            ),
        ),
    )


def render_all(repo_root: pathlib.Path = REPO_ROOT) -> dict[pathlib.Path, str]:
    """Render every vehicle in memory. Returns ``{absolute_path: content}``."""
    source_text = (repo_root / SOURCE_REL).read_text(encoding="utf-8")
    fragments = parse_fragments(source_text)
    return {repo_root / v.path: v.render(fragments) for v in _vehicles()}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. ``--check`` compares; default writes the files."""
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    rendered = render_all()

    if check:
        stale: list[pathlib.Path] = [
            path
            for path, content in rendered.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            print(
                "consumer routing renderings are stale — run "
                "`python scripts/ci/gen_consumer_routing.py`:",
                file=sys.stderr,
            )
            for path in stale:
                print(f"  - {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            return 1
        return 0

    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
