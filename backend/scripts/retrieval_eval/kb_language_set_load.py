# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Load the made-up German and English page set into a test MEHO.

One ``POST /api/v1/kb/import`` sends every page under ``kb_language_set/pages/``.
The path of a page, relative to that folder (for example ``de/page.md``), is
stored as ``provenance.relative_path``. The runner
(:mod:`scripts.retrieval_eval.agent_search`) matches a store hit by that
path, and the question files use the same paths in ``expect``.

Run it from ``backend/``, against a test tenant only::

    MEHO_TOKEN=... uv run python -m scripts.retrieval_eval.kb_language_set_load \\
        --url https://meho.example [--dry-run]

The token is read from an environment variable (``--token-env``, default
``MEHO_TOKEN``). It is never printed or logged. The run is safe to repeat:
a second run reports every page as unchanged.
Exit code: 0 when no page was skipped, 1 when a call failed or MEHO skipped
a page, 2 when the arguments are wrong.

Test tooling only. It adds no route, setting or operation to MEHO.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Final

import httpx

PAGES_DIR: Final[Path] = Path(__file__).parent / "kb_language_set" / "pages"
IMPORT_PATH: Final[str] = "/api/v1/kb/import"


def read_pages(folder: Path) -> list[dict[str, str]]:
    """Return ``{"path", "content"}`` for each Markdown page, sorted by path."""
    return [
        {"path": page.relative_to(folder).as_posix(), "content": page.read_text(encoding="utf-8")}
        for page in sorted(folder.rglob("*.md"))
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="address of the test MEHO")
    parser.add_argument("--token-env", default="MEHO_TOKEN", help="variable that holds the token")
    parser.add_argument("--pages", type=Path, default=PAGES_DIR, help="folder with the pages")
    parser.add_argument("--dry-run", action="store_true", help="show the result, write nothing")
    return parser


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        print(f"environment variable {args.token_env} is not set", file=sys.stderr)
        return 2
    pages = read_pages(args.pages)
    if not pages:
        print(f"no .md pages found in {args.pages}", file=sys.stderr)
        return 2
    body: dict[str, Any] = {"pages": pages}
    if args.dry_run:
        body["dry_run"] = True
    try:
        with httpx.Client(base_url=args.url, transport=transport, timeout=120.0) as client:
            response = client.post(
                IMPORT_PATH, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPStatusError as exc:
        print(f"the import failed: HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, ValueError) as exc:
        print(f"the import failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    prefix = "dry run: " if result.get("dry_run") else ""
    print(
        f"{prefix}created {result['created_count']}, updated {result['updated_count']},"
        f" unchanged {result['unchanged_count']}, skipped {result['skipped_count']}"
    )
    for row in result["pages"]:
        if row["action"] == "skip":
            print(f"skipped {row['path']}: {row.get('reason')}")
    return 1 if result["skipped_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
