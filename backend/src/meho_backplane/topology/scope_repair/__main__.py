# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``python -m meho_backplane.topology.scope_repair`` -- the operator entry point.

Commands:

* ``stage`` -- ask every target again and save a manifest. Prints the
  manifest id, its status and a summary. Exit ``0`` when the manifest is
  ``staged`` (ready to cut over), ``2`` when it is ``incomplete``.
* ``show <manifest-id>`` -- print the full manifest as JSON (keys,
  scopes, ids and reasons; no properties). Use it to review the plan.
* ``cutover <manifest-id>`` -- apply the manifest. Exit ``0`` on
  success, ``1`` when the cut-over refused (the reason is printed; the
  graph did not change).

It reads the same settings as the backplane (``DATABASE_URL`` and the
connector credentials), so run it inside the backplane image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from typing import Any

from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.topology.scope_repair.cutover import CutoverRefusedError, cut_over
from meho_backplane.topology.scope_repair.stage import AlreadyScopedError, stage_scope_repair
from meho_backplane.topology.scope_repair.store import StoredManifest, load_manifest


def _summary(manifest: StoredManifest) -> dict[str, Any]:
    plan = manifest.plan
    return {
        "manifest_id": str(manifest.id),
        "status": manifest.status,
        "snapshot_version": manifest.snapshot_version,
        "targets": {
            "answered": sum(1 for t in plan.targets if t.available),
            "unavailable": [str(t.target_id) for t in plan.targets if not t.available],
        },
        "nodes": dict(Counter(n.action for n in plan.nodes)),
        "edges": dict(Counter(e.action for e in plan.edges)),
        "problems": [p.model_dump(mode="json") for p in plan.problems],
    }


async def _stage() -> int:
    _eager_import_connectors()
    try:
        manifest = await stage_scope_repair()
    except AlreadyScopedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(_summary(manifest), indent=2))
    return 0 if manifest.status == "staged" else 2


async def _show(manifest_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        manifest = await load_manifest(session, manifest_id)
    if manifest is None:
        print(f"manifest {manifest_id} not found", file=sys.stderr)
        return 1
    body = {**_summary(manifest), "plan": manifest.plan.model_dump(mode="json")}
    print(json.dumps(body, indent=2))
    return 0


async def _cutover(manifest_id: uuid.UUID) -> int:
    try:
        result = await cut_over(manifest_id)
    except CutoverRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one command; return the exit code."""
    parser = argparse.ArgumentParser(prog="python -m meho_backplane.topology.scope_repair")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stage", help="ask every target again and save a manifest")
    show = sub.add_parser("show", help="print a manifest as JSON")
    show.add_argument("manifest_id", type=uuid.UUID)
    cut = sub.add_parser("cutover", help="apply a staged manifest")
    cut.add_argument("manifest_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    if args.command == "stage":
        return asyncio.run(_stage())
    if args.command == "show":
        return asyncio.run(_show(args.manifest_id))
    return asyncio.run(_cutover(args.manifest_id))


if __name__ == "__main__":
    sys.exit(main())
