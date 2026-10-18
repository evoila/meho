#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the Claude Code plugin version (#4004).

Claude Code gives users a new copy of a plugin only when its ``version``
changes. The MEHO plugin stayed at 0.30.0 for many plugin changes, so users
never got those fixes through ``claude plugin update``. This script stops
that from happening again. It checks two rules:

1. **The version is the same in every place.** ``plugin.json`` and the two
   ``version`` fields in the repo-root ``marketplace.json`` must agree.
   Claude Code uses the ``plugin.json`` value when they differ, and
   ``claude plugin validate`` reports the mismatch.
2. **A plugin change raises the version** (only with ``--base``). When a
   file under ``clients/claude-code-plugin/`` changed between ``--base``
   and ``HEAD``, the ``plugin.json`` version must be higher than it was at
   ``--base``. Files under ``test/`` do not count: the plugin does not use
   them at run time.

Usage::

    python scripts/ci/check_plugin_version.py                  # rule 1 only
    python scripts/ci/check_plugin_version.py --base HEAD^1    # rules 1 and 2

CI (``.github/workflows/plugin-version-check.yml``) passes ``--base HEAD^1``.
On a pull request and in the merge queue, ``HEAD`` is the merge commit and
its first parent is the commit it is built on, so the diff is exactly the
change of this one pull request. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

PLUGIN_DIR = "clients/claude-code-plugin/"
PLUGIN_MANIFEST = PLUGIN_DIR + ".claude-plugin/plugin.json"
MARKETPLACE = ".claude-plugin/marketplace.json"
PLUGIN_NAME = "meho"
#: Paths under PLUGIN_DIR that do not change what users get.
NOT_SHIPPED = (PLUGIN_DIR + "test/",)

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse a plain ``X.Y.Z`` version. Anything else is an error."""
    match = _VERSION_RE.match(text)
    if match is None:
        raise ValueError(f"version {text!r} is not of the form X.Y.Z")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def read_versions(root: pathlib.Path) -> dict[str, str]:
    """Return every place the plugin version is written, keyed by a label."""
    plugin = json.loads((root / PLUGIN_MANIFEST).read_text(encoding="utf-8"))
    market = json.loads((root / MARKETPLACE).read_text(encoding="utf-8"))
    entries = [p for p in market.get("plugins", []) if p.get("name") == PLUGIN_NAME]
    if len(entries) != 1:
        raise ValueError(f"{MARKETPLACE} must list the plugin {PLUGIN_NAME!r} exactly once")
    return {
        f"{PLUGIN_MANIFEST} version": str(plugin.get("version")),
        f"{MARKETPLACE} plugins[{PLUGIN_NAME}].version": str(entries[0].get("version")),
        f"{MARKETPLACE} metadata.version": str(market.get("metadata", {}).get("version")),
    }


def check_agree(versions: dict[str, str]) -> list[str]:
    """Rule 1: every place shows the same version."""
    if len(set(versions.values())) == 1:
        return []
    lines = [f"  {label}: {value}" for label, value in versions.items()]
    return ["The plugin version is not the same in every place:", *lines]


def shipped_changes(root: pathlib.Path, base: str) -> list[str]:
    """Files under the plugin directory that changed since ``base``."""
    changed = _git(root, "diff", "--name-only", base, "HEAD").splitlines()
    return [
        path for path in changed if path.startswith(PLUGIN_DIR) and not path.startswith(NOT_SHIPPED)
    ]


def check_raised(root: pathlib.Path, base: str, head_version: str) -> list[str]:
    """Rule 2: a change to the plugin raises the ``plugin.json`` version."""
    changed = shipped_changes(root, base)
    if not changed:
        return []
    try:
        base_text = _git(root, "show", f"{base}:{PLUGIN_MANIFEST}")
    except subprocess.CalledProcessError:
        return []  # the plugin is new at HEAD; there is nothing to compare with
    base_version = str(json.loads(base_text).get("version"))
    if parse_version(head_version) > parse_version(base_version):
        return []
    shown = [f"  {path}" for path in changed[:20]]
    if len(changed) > 20:
        shown.append(f"  ... and {len(changed) - 20} more")
    return [
        f"This change touches the Claude Code plugin, but the plugin version is "
        f"still {head_version} (it was {base_version}).",
        "Claude Code updates a plugin only when its version goes up, so users "
        "would not get this change.",
        f"Raise the version in {PLUGIN_MANIFEST} and in both version fields of "
        f"{MARKETPLACE}. Changed files:",
        *shown,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base",
        help="git ref to compare with; when set, a plugin change must raise the version",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[2],
        help="repository root (default: the checkout this script lives in)",
    )
    args = parser.parse_args(argv)

    try:
        versions = read_versions(args.root)
        errors = check_agree(versions)
        head_version = versions[f"{PLUGIN_MANIFEST} version"]
        parse_version(head_version)
        if not errors and args.base:
            errors = check_raised(args.root, args.base, head_version)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        errors = [f"Could not check the plugin version: {exc}"]

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"OK: the plugin version is {head_version} in every place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
