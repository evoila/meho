#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Refresh vendored Semgrep rule packs.

Replaces the registry pull at scan time (``--config p/python``, etc.) with a
deterministic, curated, git-diffable snapshot under ``.semgrep/rules/``. See
``docs/configuration/ci-quality-gates.md`` § Semgrep SAST for the rationale and
the refresh cadence.

Curation:

* ``p/python`` and ``p/typescript`` are vendored wholesale -- they already
  match MEHO's language stack.
* ``p/security-audit`` and ``p/owasp-top-ten`` are filtered: rules whose
  first-segment language prefix is in :data:`DROP_LANGS` are dropped, as are
  ``problem-based-packs.*`` rules whose path encodes an excluded sub-language
  (see :data:`DROP_SUBLANG_TOKENS`) and ``generic.<sub-namespace>.*`` rules
  for off-stack technologies (see :data:`DROP_GENERIC_SUBLANGS`). The rest
  are merged with deduplication by rule ID.
* Rules with an unrecognized first-segment prefix abort the script with a
  non-zero exit and a list of the offending IDs. Add the prefix to
  :data:`KEEP_LANGS`, :data:`DROP_LANGS`, or :data:`ROUTE` to resolve. Silent
  drops would let new registry rule families ghost without any maintainer
  signal.
* Output is split by category into three files for diff readability:
  ``python.yml`` (Python rules), ``frontend.yml`` (JS/TS/HTML), and
  ``cross-cutting.yml`` (generic + yaml + dockerfile + bash + json +
  problem-based-packs subset). Each bucket is sorted by rule ID so that
  registry-side ordering shifts cannot produce reorder-only refresh diffs.
* Pre-existing ``.semgrep/rules/*.yml`` files are deleted before writing the
  new snapshot so that renamed or retired files cannot linger as orphans CI
  would still load via ``--config .semgrep/rules/``.

Run on a maintainer's machine, commit the output:

.. code-block:: bash

    uv run python scripts/refresh-semgrep-rules.py
    git add .semgrep/rules/
    git diff --stat HEAD~1
"""

from __future__ import annotations

import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / ".semgrep" / "rules"

REGISTRY_URLS = {
    "python": "https://semgrep.dev/c/p/python",
    "typescript": "https://semgrep.dev/c/p/typescript",
    "security-audit": "https://semgrep.dev/c/p/security-audit",
    "owasp-top-ten": "https://semgrep.dev/c/p/owasp-top-ten",
}

# First-segment language prefixes for languages MEHO uses.
KEEP_LANGS: frozenset[str] = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "generic",
        "yaml",
        "dockerfile",
        "json",
        "html",
        "bash",
    }
)

# First-segment language prefixes to drop (languages MEHO does not use).
DROP_LANGS: frozenset[str] = frozenset(
    {
        "java",
        "go",
        "ruby",
        "scala",
        "kotlin",
        "swift",
        "c",
        "cpp",
        "csharp",
        "php",
        "terraform",
        "solidity",
        "clojure",
        "rust",
        "ocaml",
        "dart",
        "elixir",
    }
)

# Sub-language tokens that flag a `problem-based-packs.*` rule for drop --
# these packs encode language in path segments like
# `problem-based-packs.insecure-transport.java-stdlib.httpget-...`.
DROP_SUBLANG_TOKENS: frozenset[str] = frozenset(
    {
        "java-",
        "go-",
        "ruby-",
        "scala-",
        "kotlin-",
        "swift-",
        "c-",
        "cpp-",
        "csharp-",
        "php-",
    }
)

# `generic.*` is a meta-namespace covering many technologies (nginx, jinja,
# CI configs, secrets, visualforce, etc.). The first-segment KEEP filter
# retains all generic.* by default; this set narrows it by sub-namespace
# for off-stack technologies. Add entries when a refresh surfaces noise
# from a `generic.<token>.*` family MEHO does not use.
DROP_GENERIC_SUBLANGS: frozenset[str] = frozenset(
    {
        "visualforce",  # Salesforce; MEHO has no .page / .component files
    }
)

# Output file routing by first-segment language prefix.
ROUTE: dict[str, str] = {
    "python": "python.yml",
    "javascript": "frontend.yml",
    "typescript": "frontend.yml",
    "html": "frontend.yml",
    "generic": "cross-cutting.yml",
    "yaml": "cross-cutting.yml",
    "dockerfile": "cross-cutting.yml",
    "bash": "cross-cutting.yml",
    "json": "cross-cutting.yml",
}


def _download(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 -- semgrep.dev is trusted
        body: bytes = response.read()
        return body.decode("utf-8")


def _language_prefix(rule_id: str) -> str:
    return rule_id.split(".", 1)[0]


def _is_dropped(rule_id: str) -> bool:
    """Return True if the rule should be filtered out."""
    prefix = _language_prefix(rule_id)
    if prefix in DROP_LANGS:
        return True
    if prefix == "problem-based-packs" and any(t in rule_id for t in DROP_SUBLANG_TOKENS):
        return True
    # generic.<sublang>.* — narrows the broad "keep all generic" KEEP filter
    # for technologies MEHO does not use (e.g. generic.visualforce.*).
    if prefix == "generic":
        parts = rule_id.split(".", 2)
        if len(parts) >= 2 and parts[1] in DROP_GENERIC_SUBLANGS:
            return True
    return False


def _route_rule(rule_id: str) -> str | None:
    """Return the output file name for a rule, or None to skip."""
    prefix = _language_prefix(rule_id)
    if prefix in ROUTE:
        return ROUTE[prefix]
    if prefix == "problem-based-packs":
        # problem-based-packs rules are routed to cross-cutting; the drop
        # filter has already removed entries for excluded languages.
        return "cross-cutting.yml"
    return None


def _format_header(timestamp: str, source_packs: list[str], total_rules: int) -> str:
    return (
        "# Vendored from the Semgrep registry on "
        f"{timestamp}.\n"
        "# Source packs: " + ", ".join(source_packs) + "\n"
        f"# Rules in this file: {total_rules}\n"
        "# Rules sorted by ID so registry-side ordering shifts produce no diff.\n"
        "# Refreshed by `uv run python scripts/refresh-semgrep-rules.py`.\n"
        "# License: rules carry their own LGPL-2.1 / MIT terms from upstream;\n"
        "# see https://github.com/semgrep/semgrep-rules for license details.\n"
        "# Do NOT edit this file by hand -- changes will be lost on the next\n"
        "# refresh. To suppress a finding in our codebase, use `# nosemgrep`\n"
        "# on the matched line; to drop a rule family entirely, edit the\n"
        "# language-level filter constants in the refresh script\n"
        "# (KEEP_LANGS / DROP_LANGS / DROP_GENERIC_SUBLANGS / ROUTE) and\n"
        "# re-run; per-rule disables require curating a custom rule pack.\n"
    )


def classify_rules(
    raw_packs: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], int, list[str]]:
    """Filter, dedup, and route raw registry rules into output buckets.

    Returns ``(routed, dropped_count, unrecognized_ids)``. ``unrecognized_ids``
    is the sorted list of rule IDs whose first-segment prefix is not in
    :data:`KEEP_LANGS`, :data:`DROP_LANGS`, or ``problem-based-packs`` --
    surfacing these (rather than silently dropping) is what keeps the
    refresh honest as the registry adds new rule families.
    """
    seen: set[str] = set()
    routed: dict[str, list[dict[str, Any]]] = {
        "python.yml": [],
        "frontend.yml": [],
        "cross-cutting.yml": [],
    }
    dropped_count = 0
    unrecognized: set[str] = set()
    for pack_name in REGISTRY_URLS:
        for rule in raw_packs[pack_name]:
            rule_id = rule.get("id", "")
            if not rule_id or rule_id in seen:
                continue
            seen.add(rule_id)
            if _is_dropped(rule_id):
                dropped_count += 1
                continue
            target = _route_rule(rule_id)
            if target is None:
                unrecognized.add(rule_id)
                continue
            routed[target].append(rule)
    return routed, dropped_count, sorted(unrecognized)


def main() -> int:
    print("Downloading Semgrep registry packs...")
    raw_packs: dict[str, list[dict[str, Any]]] = {}
    for name, url in REGISTRY_URLS.items():
        data = yaml.safe_load(_download(url))
        rules = data.get("rules", [])
        raw_packs[name] = rules
        print(f"  p/{name}: {len(rules)} rules")

    routed, dropped_count, unrecognized = classify_rules(raw_packs)

    print(f"\nDropped (off-stack languages): {dropped_count} rules")
    print(f"Vendored: {sum(len(v) for v in routed.values())} rules")

    if unrecognized:
        print(
            f"\nERROR: {len(unrecognized)} rule(s) had unrecognized first-segment "
            "prefixes -- the registry has likely added a new rule family. "
            "Add the prefix to KEEP_LANGS, DROP_LANGS, or ROUTE in this script "
            "(or document why it should be skipped) and re-run:",
            file=sys.stderr,
        )
        for rule_id in unrecognized:
            print(f"  - {rule_id}", file=sys.stderr)
        return 1

    RULES_DIR.mkdir(parents=True, exist_ok=True)

    # Clear pre-existing vendored YAML files so renames or retired buckets
    # cannot linger as orphans. CI loads `--config .semgrep/rules/` -- any
    # stale .yml in this directory would still be applied silently.
    for stale in RULES_DIR.glob("*.yml"):
        stale.unlink()

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    pack_list = list(REGISTRY_URLS.keys())
    written: list[tuple[str, int, int]] = []
    for filename, rules in routed.items():
        # Sort by rule ID so the registry's own ordering can't produce
        # large reorder-only diffs on the next refresh.
        rules.sort(key=lambda rule: rule["id"])
        target_path = RULES_DIR / filename
        body = yaml.safe_dump({"rules": rules}, sort_keys=False, default_flow_style=False)
        content = _format_header(timestamp, pack_list, len(rules)) + body
        target_path.write_text(content, encoding="utf-8")
        written.append((filename, len(rules), len(content.splitlines())))

    print()
    for filename, count, lines in written:
        print(f"  {filename}: {count} rules, {lines} lines")

    return 0


if __name__ == "__main__":
    sys.exit(main())
