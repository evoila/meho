# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural test for the ``TargetCreate.product`` OpenAPI enum hook.

G0.14-T3 (#1144). The ``build_openapi_schema`` override in
:mod:`meho_backplane.main` injects a JSON Schema ``enum`` populated from
the live connector registry into the ``TargetCreate.product`` property
of the generated OpenAPI document. Operators using Swagger UI or
OpenAPI-driven generator tooling see the valid product tokens before
they submit a typo — the *discoverability* path the task body calls
**Option A**. The runtime 422 in
:func:`~meho_backplane.api.v1.targets.create_target` (covered in
``tests/test_api_v1_targets.py``) is the *recovery* path (**Option C**);
both share the same source of truth.

This test pins the three properties of the hook that matter for the
committed contract:

* The injected ``enum`` matches :func:`registered_product_tokens`
  exactly — the source-of-truth invariant. If the two ever drifted,
  Swagger UI would advertise a product the runtime 422 then rejected.
* The injected ``description`` points at the discoverability route
  and the convention doc, in line with the T11 message-shape
  convention.
* The override's cache invariant (re-using FastAPI's
  ``app.openapi_schema``) — subsequent calls are O(1) identity hits.

Module-level eager-import note
------------------------------

The conftest autouse fixture :func:`_isolate_global_registries`
snapshots ``_REGISTRY_V2`` at test setup and restores it at teardown,
so the registry state a test sees is whatever was registered *before*
the test's autouse fixtures ran. If the eager-import call lived inside
the test body (or its fixture), the populated state would not survive
into subsequent tests in the same module — each test would have to
re-import, but :func:`_eager_import_connectors` is idempotent
(``sys.modules``-guarded), so the second test would see an empty
registry and the assertion would fail.

The pattern this file uses: call :func:`_eager_import_connectors` at
**module import time** (before any conftest fixture runs), so by the
time pytest collects the first test the registry is populated and the
isolate fixture's snapshot captures the full set. Subsequent tests
restore to that populated snapshot too. This is the same pattern
``test_alembic_*`` and several connector tests use.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.openapi.utils import get_openapi

from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    registered_product_tokens,
)
from meho_backplane.main import app

# Module-level eager-import so the conftest isolate fixture's snapshot
# captures the populated registry — see the module docstring above for
# why this lives at import time and not in a fixture body.
_eager_import_connectors()


@pytest.fixture
def fresh_schema() -> dict[str, object]:
    """Drop the cached schema and return a freshly-generated one.

    The override caches on ``app.openapi_schema`` (FastAPI's own
    contract); test setup busts the cache so each test runs against a
    schema generated against the *current* registry.
    """
    app.openapi_schema = None
    return app.openapi()


def _product_field(schema: dict[str, object]) -> dict[str, object]:
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    target_create = schemas["TargetCreate"]
    assert isinstance(target_create, dict)
    properties = target_create["properties"]
    assert isinstance(properties, dict)
    field = properties["product"]
    assert isinstance(field, dict)
    return field


def test_openapi_product_enum_matches_registered_tokens(
    fresh_schema: dict[str, object],
) -> None:
    """The generated enum matches the live registry's product set, sorted.

    Pins the source-of-truth invariant: the OpenAPI enum and the
    runtime ``unknown_product`` 422 (in
    :func:`~meho_backplane.api.v1.targets.create_target`) read from
    the same :func:`registered_product_tokens` helper, so they cannot
    advertise a product the runtime then rejects.
    """
    field = _product_field(fresh_schema)
    expected = sorted(registered_product_tokens())
    assert field["enum"] == expected
    # Sanity: the live set is non-empty (otherwise the enum hook is a
    # no-op and the test would pass vacuously).
    assert expected
    # A well-known token from the real registry — guards against a
    # future change that empties the enum while keeping it non-empty
    # via stub entries.
    assert "k8s" in expected


def test_openapi_product_field_carries_discoverability_description(
    fresh_schema: dict[str, object],
) -> None:
    """The injected description names the discovery route + the convention doc.

    Both clauses come from the T11 message-shape convention
    (``docs/codebase/error-message-shape.md``): the operator gets a
    GET endpoint to enumerate the live set and a doc to read the
    422 contract. Pin them both as substring matches so a future
    rewording does not silently drop either reference.
    """
    field = _product_field(fresh_schema)
    description = field["description"]
    assert isinstance(description, str)
    assert "GET /api/v1/connectors" in description
    assert "docs/codebase/error-message-shape.md" in description


def test_openapi_hook_caches_result() -> None:
    """``app.openapi()`` returns the same dict object across calls.

    FastAPI's default caches on ``app.openapi_schema``; the override
    preserves the same caching contract so subsequent ``/openapi.json``
    requests don't re-run ``get_openapi`` + the enum injection on
    every hit. The same identity invariant is what the FastAPI docs
    recommend (https://fastapi.tiangolo.com/how-to/extending-openapi/).
    """
    app.openapi_schema = None
    first = app.openapi()
    second = app.openapi()
    assert first is second


def test_openapi_default_schema_lacks_enum_without_hook() -> None:
    """Sanity baseline: the raw ``get_openapi`` output has no enum.

    Pins that the enum is exclusively the responsibility of the
    ``build_openapi_schema`` override — if a future ``TargetCreate``
    Pydantic model added a static ``Literal[...]`` for ``product``
    the hook would become redundant and this test would catch it
    so the override could be removed cleanly (rather than silently
    doing duplicate work).
    """
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        terms_of_service=app.terms_of_service,
        contact=app.contact,
        license_info=app.license_info,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    field = _product_field(schema)
    assert "enum" not in field


# Source for the subprocess regression test below. Runs in a *fresh*
# interpreter so the side-effect import is genuinely the first thing to
# touch the connector registry — the only faithful way to reproduce the
# production import ordering (see the test docstring).
_PARTIAL_REGISTRY_REPRO = """
import json
import sys

# 1. Import a module that reaches connectors.vault first. tenant_paths
#    imports connectors.vault.ops at module top, which triggers the
#    connectors.vault package __init__ to register VaultConnector at
#    import time -- exactly the #1723 chain (api/v1/targets.py ->
#    tenant_paths -> connectors.vault.ops -> connectors.vault.__init__).
import meho_backplane.connectors.vault.tenant_paths  # noqa: F401

from meho_backplane.connectors.registry import registered_product_tokens

# 2. Before the OpenAPI override runs, the registry is partially
#    populated: only the side-effect-registered connector is present.
partial = sorted(registered_product_tokens())

# 3. Build the schema. The override must eager-load the remaining
#    connector subpackages before injecting the TargetCreate.product
#    enum, regardless of the already-non-empty registry.
from meho_backplane.main import app

app.openapi_schema = None
schema = app.openapi()
field = schema["components"]["schemas"]["TargetCreate"]["properties"]["product"]
full = sorted(registered_product_tokens())

# Importing main.py boots logging/migrations that write to stdout, so
# emit the result on its own sentinel-prefixed line the parent greps for.
print(
    "__REPRO_RESULT__"
    + json.dumps({"partial": partial, "enum": field.get("enum"), "full": full})
)
"""


def test_openapi_product_enum_full_when_registry_partially_populated() -> None:
    """The enum is the full connector set even if one connector pre-registered.

    Regression for #1723. ``connectors/vault/tenant_paths.py`` imports
    ``connectors.vault.ops`` at module top, which triggers the
    ``connectors.vault`` package ``__init__`` to register
    ``VaultConnector`` at *import* time. Any module reaching
    ``tenant_paths`` (e.g. ``api/v1/targets.py``) therefore leaves the
    registry holding exactly ``{"vault"}`` before the OpenAPI override
    runs. The override previously guarded its eager-import behind
    ``if not registered_product_tokens()``; with the registry already
    non-empty the guard short-circuited, the other 17 connector
    subpackages never loaded, and ``TargetCreate.product`` collapsed
    from 18 entries to ``["vault"]`` — turning the
    ``CLI API snapshot freshness`` CI gate red (it regenerates the
    snapshot by calling ``app.openapi()`` in a fresh interpreter).

    This pins the post-fix contract: ``build_openapi_schema`` calls
    :func:`_eager_import_connectors` **unconditionally**, so a
    partially-populated registry is fully loaded before the enum is
    injected.

    Why a subprocess: the import ordering is load-bearing and only
    reproducible in a *fresh* interpreter. This module already calls
    :func:`_eager_import_connectors` at its own import time, so within
    the test process every connector subpackage is warm in
    ``sys.modules``; clearing the registry and re-importing registers
    nothing (cached modules don't re-run their top-level
    ``register_connector_v2`` calls), which would model the harness
    rather than production. The snapshot script
    (``cli/api/snapshot-openapi.py``) hits exactly this fresh-process
    path, so the subprocess is the truest reproduction of the CI gate.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _PARTIAL_REGISTRY_REPRO],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"repro subprocess failed:\n{proc.stderr}"
    # main.py boots logging/migrations onto stdout; pull the one
    # sentinel-prefixed line the child emitted for us.
    marker = "__REPRO_RESULT__"
    payload = next(
        line[len(marker) :] for line in proc.stdout.splitlines() if line.startswith(marker)
    )
    result = json.loads(payload)

    # Precondition: the side-effect import leaves the registry holding
    # exactly the one connector whose package self-registered.
    assert result["partial"] == ["vault"], (
        "precondition: only the side-effect-registered connector is present "
        f"before the override runs; got {result['partial']}"
    )

    # Post-fix contract: the enum is the full registered set, not the
    # truncated lone token.
    enum = result["enum"]
    assert enum == result["full"]
    assert isinstance(enum, list)
    assert {"vault", "k8s", "keycloak", "argocd"}.issubset(enum)
    # Pin the exact count so a future re-truncation that still leaves
    # the enum multi-valued cannot pass: the eager import must surface
    # every distinct registered product token.
    assert len(enum) == len(result["full"]) > 1
