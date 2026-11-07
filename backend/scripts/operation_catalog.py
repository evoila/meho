#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the operation catalog file of a release, and the file of its changes.

The release workflow ``.github/workflows/operation-catalog.yml`` runs this
script on every ``v*`` tag. It writes two JSON files and attaches them, with
a cosign signature for each, to the GitHub release:

* ``operation-catalog.json`` lists every operation of every built-in
  connector: connector id, op id, parameter schema, a short result shape,
  risk level, approval flag and the product versions the connector supports.
* ``operation-catalog-changes.json`` lists what changed since the catalog of
  the previous release: added, removed, and changed (parameter schema, risk
  level or approval flag).

The operations come from the same code a MEHO server runs at start: the
script runs every connector's registrar into a new SQLite database and reads
the built-in rows back. So the parameter schema in the file is the stored
schema that ``validate_params`` checks each call against. The script needs
no running MEHO, no network and no model download. See
``docs/codebase/operation-catalog-file.md``.

Run from ``backend/``::

    uv run python -m scripts.operation_catalog export --release v1.2.3 --out operation-catalog.json
    uv run python -m scripts.operation_catalog diff --new operation-catalog.json \\
        --old previous/operation-catalog.json --out operation-catalog-changes.json

``diff`` without ``--old`` (the first release with a catalog) lists every
operation as added.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

#: Raise this when a field changes its meaning or is removed. Adding a
#: field does not raise it.
FORMAT_VERSION: Final[int] = 1

#: Operation fields whose change is listed under ``changed`` in the change file.
COMPARED_FIELDS: Final[tuple[str, ...]] = ("parameter_schema", "safety_level", "requires_approval")

#: Settings MEHO needs at start. The export reads none of them; it sets an
#: unusable value only when the variable is not set.
_PLACEHOLDER_ENV: Final[dict[str, str]] = {
    "KEYCLOAK_ISSUER_URL": "https://keycloak.invalid/realms/meho",
    "KEYCLOAK_AUDIENCE": "meho-backplane",
    "VAULT_ADDR": "https://vault.invalid",
}


class _NoEmbeddings:
    """Stands in for the embedding model: the catalog file has no search vectors."""

    dimension = 384

    async def encode_one(self, text: str) -> list[float]:
        return [0.0] * self.dimension

    async def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def connector_id(impl_id: str, version: str, product: str) -> str:
    """The id ``meho_connector_list`` shows, for example ``k8s-1.x``."""
    base = impl_id or product
    return f"{base}-{version}" if version else base


def result_shape(response_schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """The type of the result and the names of its top-level fields.

    For a list, the fields are the fields of one item. ``None`` when the
    operation declares no response schema.
    """
    if not isinstance(response_schema, dict) or not response_schema:
        return None
    shape: dict[str, Any] = {"type": response_schema.get("type")}
    properties = response_schema.get("properties")
    items = response_schema.get("items")
    if isinstance(properties, dict):
        shape["fields"] = sorted(properties)
    elif isinstance(items, dict) and isinstance(items.get("properties"), dict):
        shape["item_fields"] = sorted(items["properties"])
    return shape


def supported_versions(product: str, version: str, impl_id: str, source_kind: str) -> str | None:
    """The PEP 440 range of target versions the operation's connector supports.

    ``None`` means any version. A multi-step operation uses the connector's
    range for multi-step operations when it has one.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    cls = all_connectors_v2().get((product, version, impl_id))
    if cls is None:
        return None
    if source_kind == "composite" and cls.composite_supported_version_range:
        return cls.composite_supported_version_range
    return cls.supported_version_range


async def collect_operations() -> list[dict[str, Any]]:
    """Run every registrar into the configured database and return the catalog entries.

    The database must have the current schema. Only built-in rows count:
    typed and multi-step rows with no tenant and no connector package.
    """
    from sqlalchemy import select

    from meho_backplane.connectors.registry import _eager_import_connectors
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations.typed_register import run_typed_op_registrars
    from meho_backplane.retrieval.embedding import EmbeddingService

    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=cast(EmbeddingService, _NoEmbeddings()))
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.owner_package.is_(None),
                    EndpointDescriptor.source_kind.in_(("typed", "composite")),
                )
            )
        ).scalars()
        entries = [
            {
                "connector_id": connector_id(row.impl_id, row.version, row.product),
                "op_id": row.op_id,
                "product": row.product,
                "supported_versions": supported_versions(
                    row.product, row.version, row.impl_id, row.source_kind
                ),
                "kind": row.source_kind,
                "summary": row.summary,
                "safety_level": row.safety_level,
                "requires_approval": row.requires_approval,
                "parameter_schema": row.parameter_schema or {},
                "result_shape": result_shape(row.response_schema),
            }
            for row in rows
        ]
    return sorted(entries, key=lambda e: (e["connector_id"], e["op_id"]))


def build_catalog(release: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """The catalog document of *release*."""
    return {"format_version": FORMAT_VERSION, "release": release, "operations": operations}


def _key(op: dict[str, Any]) -> tuple[str, str]:
    return (op["connector_id"], op["op_id"])


def diff_catalogs(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """What changed from catalog *old* to catalog *new*.

    *old* is ``None`` for the first release with a catalog: then every
    operation is added. A changed parameter schema lists only the field name;
    a changed risk level or approval flag lists the value before and after.
    """
    old_ops = {_key(op): op for op in (old or {}).get("operations", [])}
    new_ops = {_key(op): op for op in new["operations"]}
    changed = []
    for key in sorted(old_ops.keys() & new_ops.keys()):
        before, after = old_ops[key], new_ops[key]
        changes = []
        for name in COMPARED_FIELDS:
            if before.get(name) == after.get(name):
                continue
            if name == "parameter_schema":
                changes.append({"field": name})
            else:
                changes.append(
                    {"field": name, "before": before.get(name), "after": after.get(name)}
                )
        if changes:
            changed.append({"connector_id": key[0], "op_id": key[1], "changes": changes})

    def ids(keys: set[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"connector_id": c, "op_id": o} for c, o in sorted(keys)]

    return {
        "format_version": FORMAT_VERSION,
        "release": new["release"],
        "previous_release": old["release"] if old else None,
        "added": ids(new_ops.keys() - old_ops.keys()),
        "removed": ids(old_ops.keys() - new_ops.keys()),
        "changed": changed,
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _migrate(url: str) -> None:
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


async def _collect_and_close() -> list[dict[str, Any]]:
    from meho_backplane.db.engine import dispose_engine

    try:
        return await collect_operations()
    finally:
        await dispose_engine()


def export(release: str, out: Path) -> int:
    """Write the catalog of *release* to *out*; return the number of operations."""
    from meho_backplane.settings import get_settings

    for name, value in _PLACEHOLDER_ENV.items():
        os.environ.setdefault(name, value)
    with tempfile.TemporaryDirectory(prefix="meho-op-catalog-") as tmp:
        url = f"sqlite+aiosqlite:///{Path(tmp) / 'catalog.db'}"
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        _migrate(url)
        operations = asyncio.run(_collect_and_close())
    if not operations:
        raise SystemExit("no built-in operation was registered")
    _write_json(out, build_catalog(release, operations))
    return len(operations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write the operation catalog of this code")
    exp.add_argument("--release", required=True, help="the release tag, for example v1.2.3")
    exp.add_argument("--out", required=True, type=Path)
    dif = sub.add_parser("diff", help="write the changes since the previous catalog")
    dif.add_argument("--new", required=True, type=Path)
    dif.add_argument("--old", type=Path, help="the previous release's catalog; omit for none")
    dif.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "export":
        count = export(args.release, args.out)
        print(f"wrote {count} operations to {args.out}")
        return 0
    new = json.loads(args.new.read_text(encoding="utf-8"))
    old = json.loads(args.old.read_text(encoding="utf-8")) if args.old else None
    changes = diff_catalogs(old, new)
    _write_json(args.out, changes)
    print(
        f"wrote {args.out}: {len(changes['added'])} added, {len(changes['removed'])} removed, "
        f"{len(changes['changed'])} changed since {changes['previous_release'] or 'no catalog'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
