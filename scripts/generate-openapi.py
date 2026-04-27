#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generate OpenAPI specification from the live FastAPI application.

Extracts the complete OpenAPI spec by importing the FastAPI app and calling
app.openapi(). No running server or Docker required -- just a Python
environment with project dependencies installed.

The generated spec is used by the MkDocs API reference page via the
neoteroi-mkdocs OAD renderer.

Usage:
    cd /path/to/MEHO.X
    source .venv/bin/activate

    # Generate to default location (docs/openapi.json)
    python scripts/generate-openapi.py

    # Generate to a custom path
    python scripts/generate-openapi.py --output /tmp/openapi.json

Environment:
    If .env exists in the project root, pydantic-settings loads it
    automatically.  When no .env is present (CI, fresh checkout), the
    script sets minimal dummy values for required config variables so
    that the FastAPI app can be constructed without connecting to any
    external services.
"""

import argparse
import json
import os
import sys

# Ensure project root is on the import path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

# ---------------------------------------------------------------------------
# Set dummy environment variables BEFORE importing meho_app.
#
# validate_startup_config() in meho_app.core.health creates a Config()
# instance which requires these fields (no defaults in the Pydantic model):
#   DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY,
#   CREDENTIAL_ENCRYPTION_KEY (>= 32 chars)
# VOYAGE_API_KEY is optional (None = community mode with TEI embeddings).
#
# os.environ.setdefault only sets them if not already present, so a real
# .env or pre-set env vars take precedence.
# ---------------------------------------------------------------------------
_DUMMY_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://dummy:dummy@localhost:5432/dummy",
    "REDIS_URL": "redis://localhost:6379/0",
    "ANTHROPIC_API_KEY": "sk-ant-dummy-key-for-openapi-generation",
    "VOYAGE_API_KEY": "pa-dummy-key-for-openapi-generation",
    "CREDENTIAL_ENCRYPTION_KEY": "dummy-encryption-key-for-openapi-spec-generation-only",
}

for key, value in _DUMMY_ENV.items():
    os.environ.setdefault(key, value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate OpenAPI spec from the live FastAPI application"
    )
    parser.add_argument(
        "--output",
        "-o",
        default=os.path.join(_project_root, "docs", "openapi.json"),
        help="Output file path (default: docs/openapi.json)",
    )
    args = parser.parse_args()

    # Importing triggers create_app() which builds all routes and schemas.
    # Stderr output from validate_startup_config() is expected and harmless.
    from meho_app.main import app

    spec = app.openapi()

    # Write the spec
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)

    # Summary
    path_count = len(spec.get("paths", {}))
    schema_count = len(spec.get("components", {}).get("schemas", {}))

    print(f"OpenAPI spec written to {args.output}")
    print(f"  Paths:   {path_count}")
    print(f"  Schemas: {schema_count}")


if __name__ == "__main__":
    main()
