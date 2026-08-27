#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reject consumer-facing docs/skills that name a nonexistent MCP tool.

Field-test finding F4 (#3143): consumer-facing docs and the Claude Code
plugin's skills named MCP tools **that do not exist under those names**
on the deployed server — bare ``broadcast_announce`` where the registered
tool is ``meho_broadcast_announce``, plus never-registered names like
``search_connectors`` / ``result_aggregate``. A skill or template that
instructs Claude to call a nonexistent tool erodes exactly the
first-reflex behaviour the plugin exists to build.

This is the CI gate that keeps consumer-facing text honest against the
registered surface. It is the doc-side sibling of the plugin's
``hooks-matcher-conformance.test.mjs`` (#3147/#3164, which guards the
hook *matchers*) and of ``backend/tests/test_mcp_surface_conformance.py``
(#3157, which pins the ``tools/list`` wire listing). The
human-readable inventory those tests anchor lives in
``docs/codebase/mcp.md`` ("Dual-surface tool inventory").

Ground truth
------------

The set of legal tool names is derived from source, the same way the
deploy sees it — never from a hand-maintained list that could drift:

* Registered tools: every ``register_mcp_tool(definition=ToolDefinition(
  ... name=X ...))`` under ``backend/src/meho_backplane/mcp/tools/``,
  where ``X`` is a string literal or a module-level ``Final[str]``
  constant (audit.py, topology.py, targets_register.py, ... register by
  constant). Both forms are resolved.
* Human-only verbs: the three decision verbs in
  ``backend/src/meho_backplane/mcp/human_only.py`` that carry no MCP
  registration under any claim set (#3155). Consumer docs legitimately
  *name* them to say "this has no MCP path", so they are legal to
  reference even though they never appear on ``tools/list``.
* A short allow-list of ``meho_``-prefixed identifiers that are package
  / module names, not tools (``meho_backplane``, ``meho_mcp_server``).

Detection
---------

Each scanned Markdown line is checked three ways:

1. **Forbidden nonexistent names** (exact token): ``search_connectors``,
   ``list_connectors``, ``result_aggregate``, ``result_export``,
   ``result_describe`` (never registered) and ``operation_id`` (the
   ``call_operation`` argument is ``op_id``). These have no legitimate
   use in consumer-facing MEHO docs; each carries a fix hint.
2. **Bare broadcast names**: ``broadcast_announce`` / ``broadcast_recent``
   / ``broadcast_watch`` not carrying the ``meho_`` prefix. The negative
   look-behind means ``meho_broadcast_announce`` and the plugin-scoped
   ``mcp__plugin_meho_meho__meho_broadcast_announce`` are both accepted.
3. **Wide net — unregistered ``meho_`` references**: any backtick-quoted
   ``meho_<name>`` token, and any ``mcp__plugin_meho_meho__<tool>``
   plugin-scoped matcher, whose ``<name>`` / ``<tool>`` is not in the
   legal set. This catches future typos (``meho_broadcst_announce``)
   and stale operator-tool spellings without a hand-maintained list.

Scope is consumer-facing paths only — the onboarding template + guide,
the plugin skills + README, the cross-repo MCP client-setup doc, and the
docs-site client pages. ``docs/codebase/mcp.md`` is deliberately NOT
scanned: it is the inventory itself and legitimately shows bare / wrong
names as teaching examples.

Exit codes
----------

* 0 — no violations
* 1 — at least one violation (each printed as a GitHub ``::error``
  annotation plus a stderr summary)
* 2 — internal error (source unreadable, extraction produced an
  implausibly small set, or the denylist went stale because a
  "nonexistent" name is now registered)
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Iterator, Mapping

#: Repo-relative path to the MCP tool-registration source. The script is
#: invoked from the repo root by the ``consumer-tool-name-check`` workflow;
#: the pytest suite passes explicit paths so it can point the guard at
#: synthetic fixtures without monkeypatching module state.
DEFAULT_TOOLS_DIR: pathlib.Path = pathlib.Path("backend/src/meho_backplane/mcp/tools")

#: Repo-relative path to the human-only verb registry (#3155).
DEFAULT_HUMAN_ONLY: pathlib.Path = pathlib.Path("backend/src/meho_backplane/mcp/human_only.py")

#: Consumer-facing paths this guard scans. Files and directories are both
#: accepted; directories are walked for ``*.md``. These are the surfaces
#: an agent or operator reads to learn how to call MEHO — the onboarding
#: template + guide, the plugin skills + README, the cross-repo client
#: setup doc, and the docs-site client pages.
DEFAULT_SCAN_ROOTS: tuple[pathlib.Path, ...] = (
    pathlib.Path("docs/examples/consumer-onboarding"),
    pathlib.Path("clients/claude-code-plugin/skills"),
    pathlib.Path("clients/claude-code-plugin/README.md"),
    pathlib.Path("docs/cross-repo/mcp-client-setup.md"),
    pathlib.Path("docs-site/clients"),
)

#: ``meho_``-prefixed identifiers that are package / module names, not
#: tools. Legal to reference in a doc; must never be flagged by the
#: wide-net check.
NON_TOOL_MEHO_IDENTIFIERS: frozenset[str] = frozenset({"meho_backplane", "meho_mcp_server"})

#: Names that are never registered and must not appear as a tool / arg
#: reference in consumer docs, each mapped to its correct replacement.
#: ``search_connectors`` / ``list_connectors`` -> ``meho_connector_list``;
#: the ``result_*`` trio -> ``result_query`` (the only result-handle tool);
#: ``operation_id`` -> ``op_id`` (the ``call_operation`` argument).
FORBIDDEN_NONEXISTENT: Mapping[str, str] = {
    "search_connectors": "meho_connector_list",
    "list_connectors": "meho_connector_list",
    "result_aggregate": "result_query",
    "result_export": "result_query",
    "result_describe": "result_query",
    "operation_id": "op_id (the call_operation argument)",
}

#: ``NAME[: ann] = "literal"`` module-level string constants. Constant-case
#: keys (optional leading underscore) so a lowercase ``name=name`` kwarg is
#: never mistaken for a tool-name source.
_CONST_RE: re.Pattern[str] = re.compile(
    r'^\s*(_?[A-Z][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*"([^"]+)"', re.MULTILINE
)

#: ``name="<literal>"`` — the common direct registration. ``\bname``
#: excludes ``agent_name=`` / ``audit_agent_name=`` etc.
_NAME_LITERAL_RE: re.Pattern[str] = re.compile(r'\bname\s*=\s*"([a-z][a-z0-9_]*)"')

#: ``name=<CONST>`` — variable registration; resolved via the const map.
_NAME_CONST_RE: re.Pattern[str] = re.compile(r"\bname\s*=\s*(_?[A-Z][A-Za-z0-9_]*)\b")

#: ``"<verb>":`` dict keys in human_only.py (the ``HUMAN_ONLY_MCP_TOOLS``
#: mapping keys are the verb names).
_HUMAN_ONLY_RE: re.Pattern[str] = re.compile(r'"(meho_[a-z0-9_]+)"\s*:')

#: A backtick-quoted ``meho_<segment>`` token — the shape a doc uses to
#: name a tool inline (`` `meho_broadcast_recent` ``). At least one alnum
#: must follow the prefix, so a bare `` `meho_` `` (a prose mention of the
#: *prefix itself*) is not mistaken for a tool reference.
_BACKTICK_TOKEN_RE: re.Pattern[str] = re.compile(r"`(meho_[a-z0-9][a-z0-9_]*)`")

#: A plugin-scoped matcher ``mcp__plugin_meho_meho__<tool>``. The tool
#: character class stops at the first regex metachar, so the server-wide
#: wildcard (``...__.*``) captures nothing and is correctly ignored.
_SCOPED_MATCHER_RE: re.Pattern[str] = re.compile(r"mcp__plugin_meho_meho__([a-z][a-z0-9_]*)")

#: Bare broadcast names (no ``meho_`` prefix). The look-behind rejects any
#: preceding word char, so ``meho_broadcast_recent`` is accepted.
_BARE_BROADCAST_RE: re.Pattern[str] = re.compile(r"(?<!\w)broadcast_(announce|recent|watch)(?!\w)")


class Violation:
    """One flagged reference: file, 1-based line, and an operator message."""

    __slots__ = ("lineno", "message", "path")

    def __init__(self, path: pathlib.Path, lineno: int, message: str) -> None:
        self.path = path
        self.lineno = lineno
        self.message = message

    def as_annotation(self) -> str:
        """GitHub Actions ``::error`` annotation (renders inline on the diff)."""
        return f"::error file={self.path},line={self.lineno}::{self.message}"

    def as_line(self) -> str:
        """Human-readable ``<path>:<lineno>: <message>`` summary line."""
        return f"{self.path}:{self.lineno}: {self.message}"


def registered_tool_names(tools_dir: pathlib.Path) -> set[str]:
    """Derive the registered MCP tool-name set from the tool source files.

    Resolves both ``name="literal"`` and ``name=CONSTANT`` registrations
    (the latter via each module's own top-level string constants), so a
    tool registered by constant is not missed.
    """
    names: set[str] = set()
    for entry in sorted(tools_dir.glob("*.py")):
        src = entry.read_text()
        consts = dict(_CONST_RE.findall(src))
        for match in _NAME_LITERAL_RE.finditer(src):
            names.add(match.group(1))
        for match in _NAME_CONST_RE.finditer(src):
            resolved = consts.get(match.group(1))
            if resolved is not None:
                names.add(resolved)
    return names


def human_only_verbs(human_only_path: pathlib.Path) -> set[str]:
    """Derive the human-only verb names (no MCP registration) from source."""
    if not human_only_path.exists():
        return set()
    return set(_HUMAN_ONLY_RE.findall(human_only_path.read_text()))


def legal_names(
    tools_dir: pathlib.Path,
    human_only_path: pathlib.Path,
) -> set[str]:
    """The full set of names a consumer doc may legally reference."""
    return (
        registered_tool_names(tools_dir)
        | human_only_verbs(human_only_path)
        | set(NON_TOOL_MEHO_IDENTIFIERS)
    )


def iter_markdown_files(roots: tuple[pathlib.Path, ...]) -> Iterator[pathlib.Path]:
    """Yield every Markdown file under the scan roots (files or dirs)."""
    for root in roots:
        if root.is_file():
            if root.suffix == ".md":
                yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.md"))


def scan_line(
    path: pathlib.Path,
    lineno: int,
    line: str,
    legal: frozenset[str],
) -> list[Violation]:
    """Return every tool-name violation on a single line."""
    found: list[Violation] = []

    for token, replacement in FORBIDDEN_NONEXISTENT.items():
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", line):
            found.append(
                Violation(
                    path,
                    lineno,
                    f"names `{token}`, which is not a registered MCP tool "
                    f"name/arg — use `{replacement}`.",
                )
            )

    for match in _BARE_BROADCAST_RE.finditer(line):
        bare = match.group(0)
        found.append(
            Violation(
                path,
                lineno,
                f"names the bare `{bare}`; the registered tool is "
                f"`meho_{bare}` (mind the `meho_` prefix).",
            )
        )

    candidates: set[str] = set(_BACKTICK_TOKEN_RE.findall(line))
    candidates.update(_SCOPED_MATCHER_RE.findall(line))
    for token in candidates:
        if token not in legal:
            found.append(
                Violation(
                    path,
                    lineno,
                    f"references `{token}`, which is not registered under "
                    f"backend/src/meho_backplane/mcp/tools/ — check the "
                    f"spelling against docs/codebase/mcp.md's inventory.",
                )
            )
    return found


def check_paths(
    roots: tuple[pathlib.Path, ...],
    legal: frozenset[str],
) -> list[Violation]:
    """Scan every Markdown file under the roots for violations."""
    violations: list[Violation] = []
    for path in iter_markdown_files(roots):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            violations.extend(scan_line(path, lineno, line, legal))
    return violations


def _parse_argv(
    args: list[str],
) -> tuple[pathlib.Path, pathlib.Path, tuple[pathlib.Path, ...]] | None:
    """Parse CLI args into ``(tools_dir, human_only, scan_roots)``.

    Positional args override the scan roots (the pytest suite points the
    guard at synthetic fixtures this way); ``--tools-dir`` / ``--human-only``
    override the registration sources. Returns ``None`` on an unknown flag.
    """
    tools_dir = DEFAULT_TOOLS_DIR
    human_only = DEFAULT_HUMAN_ONLY
    roots: list[pathlib.Path] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--tools-dir":
            i += 1
            tools_dir = pathlib.Path(args[i])
        elif arg == "--human-only":
            i += 1
            human_only = pathlib.Path(args[i])
        elif arg.startswith("--"):
            print(f"unknown flag: {arg}", file=sys.stderr)
            return None
        else:
            roots.append(pathlib.Path(arg))
        i += 1
    return tools_dir, human_only, tuple(roots) if roots else DEFAULT_SCAN_ROOTS


def _report(violations: list[Violation]) -> None:
    """Emit GitHub ``::error`` annotations plus a stderr summary."""
    for violation in violations:
        print(violation.as_annotation())
    print(
        f"\nConsumer tool-name conformance FAILED ({len(violations)} violation(s)):",
        file=sys.stderr,
    )
    for violation in violations:
        print(f"  - {violation.as_line()}", file=sys.stderr)
    print(
        "\nThe authoritative tool inventory is docs/codebase/mcp.md (Dual-surface tool inventory).",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. See the module docstring for exit-code semantics."""
    parsed = _parse_argv(list(sys.argv[1:] if argv is None else argv))
    if parsed is None:
        return 2
    tools_dir, human_only, scan_roots = parsed

    try:
        registered = registered_tool_names(tools_dir)
    except OSError as exc:
        print(f"check_consumer_tool_names: cannot read {tools_dir}: {exc}", file=sys.stderr)
        return 2

    # Guard the guard: a broken path or a format change must fail loudly,
    # not vacuously pass by producing an empty (permissive) legal set.
    if len(registered) < 40:
        print(
            f"check_consumer_tool_names: derived only {len(registered)} tool "
            f"names from {tools_dir}; extraction is likely broken.",
            file=sys.stderr,
        )
        return 2

    # The denylist must stay honest: if a "nonexistent" name ever becomes
    # registered, the denylist entry is now wrong and must be revisited.
    stale = {tok for tok in FORBIDDEN_NONEXISTENT if tok in registered}
    if stale:
        print(
            "check_consumer_tool_names: denylist is stale — these are now "
            f"registered tools and must be removed from FORBIDDEN_NONEXISTENT: "
            f"{sorted(stale)}",
            file=sys.stderr,
        )
        return 2

    legal = frozenset(registered | human_only_verbs(human_only) | NON_TOOL_MEHO_IDENTIFIERS)

    try:
        violations = check_paths(scan_roots, legal)
    except OSError as exc:
        print(f"check_consumer_tool_names: cannot read a scan path: {exc}", file=sys.stderr)
        return 2

    if violations:
        _report(violations)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
