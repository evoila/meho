#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add SPDX license headers to all source files."""
import os
import sys

PY_HEADER = "# SPDX-License-Identifier: AGPL-3.0-only\n# Copyright (c) 2026 evoila Group\n"
TS_HEADER = "// SPDX-License-Identifier: AGPL-3.0-only\n// Copyright (c) 2026 evoila Group\n"

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".mypy_cache", "dist",
    ".venv", "venv", "env", ".tox", ".eggs", "htmlcov", "site",
    "alembic",  # migration files
}


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS


def add_header_to_file(filepath: str, header: str) -> bool:
    """Add SPDX header to a file. Returns True if modified."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, PermissionError):
        return False

    # Skip empty files
    if not content.strip():
        return False

    # Skip if already has SPDX header
    if "SPDX-License-Identifier" in content:
        return False

    # For Python files: handle shebang and encoding lines
    if filepath.endswith(".py"):
        lines = content.split("\n", 2)
        prefix_lines = []

        # Check for shebang
        if lines and lines[0].startswith("#!"):
            prefix_lines.append(lines[0])
            lines = lines[1:] if len(lines) > 1 else []
            rest = "\n".join(lines) if lines else ""
        else:
            rest = content

        # Check for encoding declaration
        first_line = rest.split("\n", 1)[0] if rest else ""
        if first_line.startswith("# -*-") or first_line.startswith("# coding"):
            prefix_lines.append(first_line)
            rest = rest.split("\n", 1)[1] if "\n" in rest else ""

        if prefix_lines:
            new_content = "\n".join(prefix_lines) + "\n" + header + rest
        else:
            new_content = header + content
    else:
        # TypeScript/TSX: header at top
        new_content = header + content

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def process_directory(base_dir: str, extensions: set, header: str) -> tuple[int, int]:
    """Walk directory and add headers. Returns (processed, modified)."""
    processed = 0
    modified = 0
    for root, dirs, files in os.walk(base_dir):
        # Filter out skip directories
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for filename in files:
            ext = os.path.splitext(filename)[1]
            if ext not in extensions:
                continue
            filepath = os.path.join(root, filename)
            processed += 1
            if add_header_to_file(filepath, header):
                modified += 1
    return processed, modified


def main():
    total_processed = 0
    total_modified = 0

    # Python directories
    py_dirs = ["meho_app", "meho_mcp_server", "tests"]
    for d in py_dirs:
        if os.path.exists(d):
            p, m = process_directory(d, {".py"}, PY_HEADER)
            total_processed += p
            total_modified += m
            print(f"  {d}: {p} files found, {m} modified")

    # Python scripts
    scripts_dir = "scripts"
    if os.path.exists(scripts_dir):
        for f in os.listdir(scripts_dir):
            if f.endswith(".py"):
                filepath = os.path.join(scripts_dir, f)
                total_processed += 1
                if add_header_to_file(filepath, PY_HEADER):
                    total_modified += 1
                    print(f"  scripts/{f}: modified")

    # TypeScript/TSX
    ts_dir = "meho_frontend/src"
    if os.path.exists(ts_dir):
        p, m = process_directory(ts_dir, {".ts", ".tsx"}, TS_HEADER)
        total_processed += p
        total_modified += m
        print(f"  {ts_dir}: {p} files found, {m} modified")

    print(f"\nTotal: {total_processed} files processed, {total_modified} modified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
