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
