# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared catalog and request paths, for the vRA 8 and VCFA reconcile lanes.

Both connectors send the calls of
:mod:`meho_backplane.connectors.vmware_shared.vra_catalog`. Each lane adds
:func:`catalog_op_ids` to its declared set, so each lane checks them against
the pinned ``vra-catalog-deployment.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from meho_backplane.connectors.vmware_shared import vra_catalog

#: The Swagger 2.0 path-item keys that are HTTP operations (its fixed
#: verb vocabulary); everything else on a path item (``parameters``,
#: vendor extensions) is not a served operation.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

#: HTTP method(s) per path constant of the shared module. A new constant
#: missing here fails the sweep, so its method is mapped on purpose.
CATALOG_PATH_METHODS: Final[dict[str, frozenset[str]]] = {
    "CATALOG_ITEMS_PATH": frozenset({"GET"}),
    "CATALOG_ITEM_PATH": frozenset({"GET"}),
    "CATALOG_ITEM_VERSION_PATH": frozenset({"GET"}),
    "CATALOG_ITEM_REQUEST_PATH": frozenset({"POST"}),
    "DEPLOYMENTS_PATH": frozenset({"GET"}),
    "DEPLOYMENT_PATH": frozenset({"GET"}),
    "REQUEST_PATH": frozenset({"GET"}),
    "REQUEST_EVENTS_PATH": frozenset({"GET"}),
    "REQUEST_EVENT_LOGS_PATH": frozenset({"GET"}),
}

#: The pinned manifest of the shared calls.
EXPECTED_CATALOG_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        "GET:/catalog/api/items",
        "GET:/catalog/api/items/{id}",
        "GET:/catalog/api/items/{id}/versions/{versionId}",
        "POST:/catalog/api/items/{id}/request",
        "GET:/deployment/api/deployments",
        "GET:/deployment/api/deployments/{deploymentId}",
        "GET:/deployment/api/requests/{requestId}",
        "GET:/deployment/api/requests/{requestId}/events",
        "GET:/deployment/api/requests/{requestId}/events/{eventId}/logs",
    }
)


def catalog_path_constants() -> dict[str, str]:
    """The live ``*_PATH`` constants of the shared module."""
    return {
        name: value
        for name, value in vars(vra_catalog).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def catalog_op_ids() -> set[str]:
    """Every ``METHOD:/path`` the shared catalog handlers send."""
    declared: set[str] = set()
    for name, path in catalog_path_constants().items():
        methods = CATALOG_PATH_METHODS.get(name)
        if methods is None:
            raise AssertionError(
                f"vra_catalog.{name} has no entry in CATALOG_PATH_METHODS; map its "
                "HTTP method there so the reconcile lanes keep covering it."
            )
        declared.update(f"{method}:{path}" for method in methods)
    return declared


def swagger2_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/path`` set from a Swagger 2.0 document.

    The pinned tenant spec is Swagger 2.0, which the ingest parser
    rejects by decision (#2090) — so this lane extracts directly instead
    of routing through :func:`tests._spec_shelf.openapi_served_op_ids`
    (the standard's non-OpenAPI-artifact clause). Served op_ids are the
    ``basePath``-folded path keys crossed with their operation-verb
    keys; ``vra-iaas.json`` pins ``basePath: /``, so its paths fold
    unchanged. Format drift on the shelf (a re-pin that is no longer
    Swagger 2.0, or an empty path map) fails loudly rather than
    regressing the lane to a vacuous compare.
    """
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(
            f"{spec_path.name}: expected a Swagger 2.0 document "
            f"(got swagger={document.get('swagger')!r} / "
            f"openapi={document.get('openapi')!r}) — the shelf artifact "
            "format moved; update this lane's extraction."
        )
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        pytest.fail(
            f"{spec_path.name}: extracted zero served operations — the "
            "shelf artifact layout moved; update this lane's extraction."
        )
    return served
