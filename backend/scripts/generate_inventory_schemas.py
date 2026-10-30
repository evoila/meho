# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the estate inventory and calculation result shapes as JSON Schema files.

A tool outside the backplane checks an inventory or a result against these
files and does not need to import backplane code. The shape version is in
each file name. ``tests/test_inventory_shapes.py`` fails when a model
changes and the files were not made again.

Run from ``backend/``::

    uv run python scripts/generate_inventory_schemas.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meho_backplane.connectors.migration.contracts import (
    CALCULATION_RESULT_SCHEMA_VERSION,
    INVENTORY_SCHEMA_VERSION,
    calculation_result_json_schema,
    inventory_json_schema,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: Repository path of each file, and the function that makes its schema.
SCHEMA_FILES: dict[str, Callable[[], dict[str, Any]]] = {
    f"docs/schemas/inventory-v{INVENTORY_SCHEMA_VERSION}.schema.json": inventory_json_schema,
    (
        f"docs/schemas/calculation-result-v{CALCULATION_RESULT_SCHEMA_VERSION}.schema.json"
    ): calculation_result_json_schema,
}


def render_schema(make: Callable[[], dict[str, Any]]) -> str:
    """The file text of one schema: the dialect first, sorted keys, two-space indent."""
    return json.dumps({"$schema": _DIALECT, **make()}, indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Write every file in :data:`SCHEMA_FILES`."""
    for relpath, make in SCHEMA_FILES.items():
        target = _REPO_ROOT / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_schema(make), encoding="utf-8")
        print(f"wrote {relpath}")


if __name__ == "__main__":
    main()
