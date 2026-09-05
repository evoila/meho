# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.19-T1 (#1479, widened by #3370) grep gate: agent- and operator-facing
text must not point at a phantom result-handle read-back surface, nor
describe SQL / filter / aggregate guidance against the paging-only
``result_query`` meta-tool.

Background
----------
When this gate was first written (#1479) *no* result-handle read-back
meta-tool existed, so every ``result_*`` reference was a phantom. Since
then ``result_query`` has shipped (#1507 / #3179) as a **paging-only**
tool -- ``result_query(handle_id, offset, limit)`` reads a window of a
set-shaped response spilled to the Valkey-backed
:class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`.
Two drift classes remain, and #3369 removed 36 live instances of them
across connector descriptors, dev docs, and CLI help; this widened gate
keeps them from returning:

1. **Phantom read-back names.** ``result_aggregate`` / ``result_describe``
   / ``result_export`` (and their hyphenated CLI-verb spellings
   ``result-aggregate`` etc.) were never registered. The name set is
   sourced from ``scripts/ci/check_consumer_tool_names.py``'s
   ``FORBIDDEN_NONEXISTENT`` (its ``result_*`` subset), so the two guards
   share one denylist rather than drifting apart.
2. **SQL / filter / aggregate guidance against paging-only
   ``result_query``.** ``SELECT ... FROM`` / ``WHERE`` / ``GROUP BY`` /
   ``filter on`` / ``count by`` / ``sum ... by`` / ``aggregate over the
   handle`` / a jq-style positional argument passed to ``result-query``
   all imply a query surface the tool does not have. The legal argument
   set is read from the *registered* tool's ``inputSchema`` (today
   ``handle_id`` / ``offset`` / ``limit``); if #3366 later adds a real
   query argument the paging-only drift rules relax automatically, with no
   change to this guard (issue #3370, "Out of scope").

The historical phantom **HandleStore** phrasing (``through the shared
HandleStore`` / ``via the HandleStore``) stays flagged too: the store that
shipped is ``ResultHandleStore``, and no descriptor should send the agent
at a bare "HandleStore".

Scope
-----
The surfaces where this guidance lives: connector descriptor source
(``backend/src/meho_backplane/connectors``), the ``docs/codebase`` +
``docs/cross-repo`` pages, and the Cobra ``Long`` help strings under
``cli/internal/cmd``. ``docs/decisions`` and ``docs/architecture`` are
deliberately out of scope -- their historical registers name the phantom
tools on purpose.

Negative mentions -- text that names a phantom in order to say it is *not*
callable -- stay legal via an explicit per-site ``(path, phrase)``
allow-list, mirroring ``check_consumer_tool_names.py``'s identifier
allow-list rather than a natural-language negation heuristic.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

from meho_backplane.mcp.registry import get_tool

#: Repo root, resolved from ``backend/tests/<this>`` -> ``parents[2]``.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _load_consumer_name_guard() -> ModuleType:
    """Import ``scripts/ci/check_consumer_tool_names.py`` as a module.

    The phantom read-back names live there (in ``FORBIDDEN_NONEXISTENT``);
    importing the script rather than restating the names keeps the two
    guards' denylist single-sourced.
    """
    path = _REPO_ROOT / "scripts" / "ci" / "check_consumer_tool_names.py"
    spec = importlib.util.spec_from_file_location("_meho_consumer_tool_name_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result_query_input_schema() -> dict[str, object]:
    """Return the *registered* ``result_query`` tool's ``inputSchema``.

    Reading it from the registry (rather than a hand-kept list) means the
    legal-argument set tracks the tool: when #3366 adds a query argument,
    the schema -- and this guard's view of it -- widens on its own.
    """
    registered = get_tool("result_query")
    if registered is None:
        # Not yet imported in this test session -- importing the module
        # runs its ``register_mcp_tool`` side effect.
        import meho_backplane.mcp.tools.result_query  # noqa: F401  (registers result_query)

        registered = get_tool("result_query")
    if registered is None:
        raise RuntimeError(
            "result_query MCP tool is not registered; cannot derive its legal argument set"
        )
    definition, _handler = registered
    return definition.inputSchema


_name_guard = _load_consumer_name_guard()

#: The result-handle read-back names that were never registered, sourced
#: from the sibling markdown guard's denylist (its ``result_*`` subset).
_PHANTOM_READBACK_NAMES: tuple[str, ...] = tuple(
    sorted(name for name in _name_guard.FORBIDDEN_NONEXISTENT if name.startswith("result_"))
)

#: ``result_query``'s legal argument names, derived from its registered
#: ``inputSchema``. Today: ``handle_id`` / ``offset`` / ``limit``.
_LEGAL_RESULT_QUERY_ARGS: frozenset[str] = frozenset(
    _result_query_input_schema()["properties"]  # type: ignore[arg-type]
)

#: The shipped v0.1-spec §4 paging contract (#1507 / #3179). This is a
#: fixed historical baseline, NOT a second copy of the legal-arg list: it
#: never grows. When the registered arg set (above) advertises anything
#: beyond it, ``result_query`` has gained a real query surface and the
#: SQL/aggregate/jq drift rules stop applying (issue #3370, "Out of scope"
#: -- Task A widens the derived set, no guard change needed).
_SHIPPED_PAGING_CONTRACT_ARGS: frozenset[str] = frozenset({"handle_id", "offset", "limit"})

#: True while ``result_query`` exposes only paging arguments. Gates the
#: SQL/aggregate/jq drift rules so they relax automatically once a query
#: surface ships.
_RESULT_QUERY_IS_PAGING_ONLY: bool = _LEGAL_RESULT_QUERY_ARGS <= _SHIPPED_PAGING_CONTRACT_ARGS

# --- rule patterns -----------------------------------------------------------

#: Rule A -- a phantom read-back **name** (underscore or hyphen spelling),
#: e.g. ``result_aggregate`` / ``result-export``. Built from the shared
#: denylist so it can never mention ``result_query`` (a real tool).
_PHANTOM_NAME_RE: re.Pattern[str] = re.compile(
    r"(?<![\w-])result[_-](?:"
    + "|".join(sorted(name.removeprefix("result_") for name in _PHANTOM_READBACK_NAMES))
    + r")(?![\w-])",
    re.IGNORECASE,
)

#: Rule H -- the phantom "HandleStore" spill surface. ``ResultHandleStore``
#: (the real class) does not match: the phrase requires ``the [shared]
#: handlestore`` with nothing between ``the`` and ``handlestore``.
_HANDLESTORE_RE: re.Pattern[str] = re.compile(
    r"(?:through|via)\s+the\s+(?:shared\s+)?handlestore", re.IGNORECASE
)

#: A result-handle context anchor -- Rule-B SQL phrases only count as drift
#: when one sits near them on the same line.
_HANDLE_ANCHOR_RE: re.Pattern[str] = re.compile(
    r"result[_-]query|result[_-]handle|result\s+handle|resulthandle|jsonflux\s+handle",
    re.IGNORECASE,
)

#: Rule B -- ``aggregate over a/the handle`` is self-anchored (it names the
#: handle itself), so it needs no separate anchor.
_AGG_OVER_HANDLE_RE: re.Pattern[str] = re.compile(
    r"aggregate\s+over\s+(?:a|the)\s+handle", re.IGNORECASE
)

#: A bare ``result_query`` / ``result-query`` mention -- the start of a
#: (possible) invocation whose trailing argument text is inspected.
_RESULT_QUERY_VERB_RE: re.Pattern[str] = re.compile(r"result[_-]query", re.IGNORECASE)

#: A jq-style positional expression: a quote immediately opening a filter
#: (``'.[]``, ``'.foo``, ``"[0]"``).
_JQ_POSITIONAL_RE: re.Pattern[str] = re.compile(r"""['"]\s*[.\[]""")

#: How close (in characters, same line) a SQL phrase must sit to a handle
#: anchor to count as drift -- tight enough that an unrelated "select"
#: dropdown a paragraph away from a "ResultHandle" mention is not flagged.
_PROXIMITY: int = 50

#: Rule B -- SQL / filter / aggregate phrases. Each fires only when a
#: handle anchor sits within ``_PROXIMITY`` characters on the same line.
_SQL_PHRASE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SELECT ... FROM", re.compile(r"\bSELECT\b.{0,80}?\bFROM\b", re.IGNORECASE)),
    ("WHERE", re.compile(r"\bWHERE\b", re.IGNORECASE)),
    ("GROUP BY", re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)),
    ("filter on", re.compile(r"\bfilter on\b", re.IGNORECASE)),
    ("count by", re.compile(r"\bcount by\b", re.IGNORECASE)),
    ("sum ... by", re.compile(r"\bsum\b.{0,30}?\bby\b", re.IGNORECASE)),
)

#: Files walked, as ``(root, glob)`` pairs, all relative to the repo root.
_SCAN_ROOTS: tuple[tuple[Path, str], ...] = (
    (_REPO_ROOT / "backend" / "src" / "meho_backplane" / "connectors", "*.py"),
    (_REPO_ROOT / "docs" / "codebase", "*.md"),
    (_REPO_ROOT / "docs" / "cross-repo", "*.md"),
    (_REPO_ROOT / "cli" / "internal" / "cmd", "*.go"),
)

#: Per-site negative-mention allow-list: ``(path suffix, line substring)``.
#: An offending line is legal when its path ends with the suffix AND the
#: line contains the substring -- both scoped tightly to the one sentence
#: that documents a phantom's *absence*. Re-confirmed against the tree on
#: 2026-09-05: ``docs/codebase/mcp.md`` L410-413 ("a nonexistent
#: ``search_connectors`` / ``result_aggregate``"). ``docs-site`` is not a
#: scan root here, so ``first-operations.md`` needs no entry.
_NEGATIVE_MENTION_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("docs/codebase/mcp.md", "reciprocal, not divergent"),
)


class Offense(NamedTuple):
    """One flagged line: 1-based line number, the rule id, the line text."""

    lineno: int
    rule: str
    text: str


def _invocation_window(line: str, start: int) -> str:
    """Text from ``start`` to the invocation's end (a backtick or ``;``).

    Bounds argument inspection to the ``result_query`` call itself so a
    sibling suggestion later on the line (``... ; or pass ``--json```) is
    not read as an argument to the tool.
    """
    end = len(line)
    for terminator in ("`", ";"):
        idx = line.find(terminator, start)
        if idx != -1:
            end = min(end, idx)
    return line[start:end]


def _near_anchor(line: str, span: tuple[int, int]) -> bool:
    """True when a handle anchor sits within ``_PROXIMITY`` of ``span``."""
    lo, hi = span
    return any(
        anchor.start() <= hi + _PROXIMITY and lo <= anchor.end() + _PROXIMITY
        for anchor in _HANDLE_ANCHOR_RE.finditer(line)
    )


def _scan_line(line: str) -> list[str]:
    """Return the rule ids a single line trips (empty when clean)."""
    rules: list[str] = []

    if _PHANTOM_NAME_RE.search(line):
        rules.append("phantom-readback-name")
    if _HANDLESTORE_RE.search(line):
        rules.append("phantom-handlestore")

    if not _RESULT_QUERY_IS_PAGING_ONLY:
        # A real query surface shipped -- SQL/aggregate/jq guidance is no
        # longer necessarily drift; leave those rules dormant.
        return rules

    if _AGG_OVER_HANDLE_RE.search(line):
        rules.append("aggregate-over-handle")

    for verb in _RESULT_QUERY_VERB_RE.finditer(line):
        if _JQ_POSITIONAL_RE.search(_invocation_window(line, verb.end())):
            rules.append("jq-argument-to-result_query")
            break

    for label, pattern in _SQL_PHRASE_RES:
        match = pattern.search(line)
        if match and _near_anchor(line, match.span()):
            rules.append(f"sql-guidance:{label}")

    return rules


def _is_allowed(rel_path: str, line: str) -> bool:
    """True when ``(rel_path, line)`` matches a negative-mention allow entry."""
    return any(
        rel_path.endswith(suffix) and substring.lower() in line.lower()
        for suffix, substring in _NEGATIVE_MENTION_ALLOWLIST
    )


def find_offenses(rel_path: str, text: str) -> list[Offense]:
    """Scan *text* (attributed to *rel_path*) for phantom read-back drift."""
    offenses: list[Offense] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        rules = _scan_line(line)
        if not rules or _is_allowed(rel_path, line):
            continue
        offenses.extend(Offense(lineno, rule, line.strip()) for rule in rules)
    return offenses


def _scan_tree() -> list[str]:
    """Scan every file under the scan roots; return ``path:line [rule]`` lines."""
    hits: list[str] = []
    for root, glob in _SCAN_ROOTS:
        for path in sorted(root.rglob(glob)):
            rel = str(path.relative_to(_REPO_ROOT))
            for offense in find_offenses(rel, path.read_text(encoding="utf-8")):
                hits.append(f"{rel}:{offense.lineno} [{offense.rule}] {offense.text}")
    return hits


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_agent_facing_string_points_at_a_phantom_readback_surface() -> None:
    """No scanned file points the agent at a phantom read-back surface.

    Fails when a connector descriptor, a ``docs/codebase`` / ``docs/cross-repo``
    page, or a CLI ``Long`` help string reintroduces a phantom read-back
    name, a phantom HandleStore reference, or SQL/aggregate/jq guidance
    against the paging-only ``result_query``.
    """
    offenders = _scan_tree()
    assert not offenders, (
        "agent-facing text must not name a phantom read-back tool / HandleStore, "
        "nor describe a query surface result_query does not have "
        f"(legal args: {sorted(_LEGAL_RESULT_QUERY_ARGS)}):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Guard-the-guard invariants
# ---------------------------------------------------------------------------


def test_phantom_name_set_is_sourced_from_the_shared_denylist() -> None:
    """The phantom-name set is the ``result_*`` subset of the sibling guard."""
    assert set(_PHANTOM_READBACK_NAMES) == {
        "result_aggregate",
        "result_describe",
        "result_export",
    }
    # Sourced, not restated: every name is in the sibling guard's denylist.
    for name in _PHANTOM_READBACK_NAMES:
        assert name in _name_guard.FORBIDDEN_NONEXISTENT


def test_legal_result_query_args_are_derived_from_the_registered_schema() -> None:
    """The legal-arg set comes from the registered tool; #3366 added `query`.

    Before #3366 the tool was paging-only (``handle_id`` / ``offset`` /
    ``limit``); the bounded structured-query surface (#3366) added the
    ``query`` argument, which widens the derived set beyond the shipped paging
    contract and flips ``_RESULT_QUERY_IS_PAGING_ONLY`` to ``False`` — exactly
    the automatic relaxation this guard was designed for (issue #3370, "Out of
    scope"). The SQL/aggregate/jq drift rules go dormant as a result.
    """
    assert "handle_id" in _LEGAL_RESULT_QUERY_ARGS
    assert sorted(_LEGAL_RESULT_QUERY_ARGS) == ["handle_id", "limit", "offset", "query"]
    assert _RESULT_QUERY_IS_PAGING_ONLY is False


def test_paging_only_gate_relaxes_when_a_query_arg_ships() -> None:
    """A simulated query-surface widening disables the SQL/aggregate rules."""
    widened = _LEGAL_RESULT_QUERY_ARGS | {"query"}
    assert not (widened <= _SHIPPED_PAGING_CONTRACT_ARGS)


def _force_paging_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the paging-only gate ON for the dormant-rule logic fixtures.

    #3366 shipped the ``query`` argument, so live ``_RESULT_QUERY_IS_PAGING_ONLY``
    is now ``False`` and the SQL/aggregate/jq drift rules are dormant (that is
    the guard's designed auto-relaxation). The fixtures below still verify the
    rule *logic* — should the surface ever be removed and the tool revert to
    paging-only — by forcing the gate back ON.
    """
    monkeypatch.setattr(sys.modules[__name__], "_RESULT_QUERY_IS_PAGING_ONLY", True)


# ---------------------------------------------------------------------------
# Negative fixtures -- one (or a family) per rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "result_aggregate",
        "result_describe",
        "result_export",
        "result-aggregate",
        "result-describe",
        "result-export",
    ],
)
def test_phantom_readback_name_is_flagged(name: str) -> None:
    """Each phantom name (underscore and hyphen spelling) trips Rule A.

    Rule A (and Rule H) is unconditional — it fires regardless of the
    paging-only gate — so this needs no gate override.
    """
    offenses = find_offenses("connectors/x.py", f"drill in with {name} for the rows")
    assert [o.rule for o in offenses] == ["phantom-readback-name"]


@pytest.mark.parametrize(
    ("label", "text"),
    [
        (
            "SELECT ... FROM",
            "page with result_query, e.g. SELECT id, severity FROM result WHERE id = 'x'",
        ),
        ("WHERE", "a ``result_query`` ``WHERE id = ...`` away."),
        ("GROUP BY", "against the result handle, GROUP BY guestOs to flag OSes"),
        ("filter on", "drill in with result_query (e.g. filter on used.storage)."),
        ("count by", "narrow with result_query (e.g. count by projectId or status)."),
        ("sum ... by", "reduced to a result handle; sum memoryMB by org."),
    ],
)
def test_sql_guidance_near_a_handle_anchor_is_flagged(
    label: str, text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each SQL/aggregate phrase near a handle anchor trips Rule B (when paging-only)."""
    _force_paging_only(monkeypatch)
    offenses = find_offenses("connectors/x.py", text)
    assert any(o.rule == f"sql-guidance:{label}" for o in offenses), offenses


def test_aggregate_over_the_handle_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-anchored ``aggregate over the handle`` phrase trips Rule B (when paging-only)."""
    _force_paging_only(monkeypatch)
    offenses = find_offenses(
        "connectors/x.py", "aggregate over the handle for host/version counts."
    )
    assert [o.rule for o in offenses] == ["aggregate-over-handle"]


def test_jq_positional_argument_to_result_query_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A jq-style positional passed to ``result-query`` trips Rule B (when paging-only)."""
    _force_paging_only(monkeypatch)
    offenses = find_offenses(
        "docs/cross-repo/x.md",
        "meho operation result-query <handle_id> '.[] | .server.server_ip'",
    )
    assert any(o.rule == "jq-argument-to-result_query" for o in offenses), offenses


def test_sql_guidance_is_dormant_now_that_query_surface_shipped() -> None:
    """Live (post-#3366) the SQL/aggregate/jq rules are dormant, not firing.

    ``result_query`` now has a real query surface, so guidance describing a
    filter / group / aggregate over the handle is accurate, not drift. The
    live gate must therefore let those phrases through — only the phantom-name
    and HandleStore rules stay active.
    """
    assert _RESULT_QUERY_IS_PAGING_ONLY is False
    text = "narrow with result_query (e.g. count by projectId); GROUP BY guestOs"
    assert find_offenses("connectors/x.py", text) == []
    # But a phantom read-back name is still caught, gate or no gate.
    assert find_offenses("connectors/x.py", "use result_aggregate") == [
        Offense(1, "phantom-readback-name", "use result_aggregate")
    ]


def test_phantom_handlestore_phrase_is_flagged() -> None:
    """A ``through the shared HandleStore`` reference trips Rule H."""
    offenses = find_offenses(
        "cli/internal/cmd/x.go",
        '"through the shared HandleStore — page the result handle"',
    )
    assert any(o.rule == "phantom-handlestore" for o in offenses), offenses


# ---------------------------------------------------------------------------
# Positive controls -- shipped-contract phrasing must NOT be flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "page it with result_query(handle_id, offset, limit) and read rows per row.",
        "the result handle the agent drills into via `result_query`.",
        "meho operation result-query <handle_id> --offset 0 --limit 50",
        "Optional. The owner name to filter on. Omit to return every record.",  # no anchor
        "a ResultHandle metadata field, plus a Write scope select for the mode",  # UI select
    ],
)
def test_shipped_contract_phrasing_is_not_flagged(text: str) -> None:
    """Correct paging phrasing and unrelated ``select``/``filter on`` stay clean."""
    assert find_offenses("connectors/x.py", text) == []


# ---------------------------------------------------------------------------
# Allow-list is path-scoped and load-bearing
# ---------------------------------------------------------------------------


def test_negative_mention_is_allowed_only_at_its_registered_path() -> None:
    """The mcp.md negative mention is legal there, flagged anywhere else."""
    line = (
        "a nonexistent `search_connectors` / `result_aggregate`). "
        "The two are reciprocal, not divergent — names are"
    )
    assert find_offenses("docs/codebase/mcp.md", line) == []
    # Same text under any other path is a real offense (proves scoping).
    elsewhere = find_offenses("docs/cross-repo/other.md", line)
    assert [o.rule for o in elsewhere] == ["phantom-readback-name"]
