# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the target record shape as a JSON Schema file.

The toolkit repos check their ``targets.yaml`` against this file and do not
need to import backplane code. ``tests/test_targets_file_schema.py`` fails
when the model changes and the file was not made again.

Run from ``backend/``::

    uv run python scripts/generate_targets_file_schema.py
"""

from __future__ import annotations

import json
from pathlib import Path

from meho_backplane.targets.file_schema import (
    TARGET_RECORD_SCHEMA_VERSION,
    target_record_json_schema,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PATH = f"docs/schemas/target-record-v{TARGET_RECORD_SCHEMA_VERSION}.schema.json"


def render_schema() -> str:
    """The file text: the dialect first, sorted keys, two-space indent."""
    return (
        json.dumps({"$schema": _DIALECT, **target_record_json_schema()}, indent=2, sort_keys=True)
        + "\n"
    )


def main() -> None:
    """Write the schema file."""
    target = _REPO_ROOT / SCHEMA_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_schema(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
