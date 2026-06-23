# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Helpers for introspecting the FastAPI 0.137+ route tree in tests.

FastAPI 0.137 (`PR #15745 <https://github.com/fastapi/fastapi/pull/15745>`_)
stopped flattening included routers into a single ``router.routes`` list.
``include_router()`` now leaves an ``_IncludedRouter`` wrapper whose nested
routes live under ``original_router.routes``, forming a tree. Tests that
depended on a flat ``routes`` list — for route-presence or
first-match-ordering assertions — must walk that tree.

:func:`iter_routes` does a depth-first walk in registration order, so the
literal-before-param ordering assertions these tests make (first-match-wins
routing) still hold. Pure route-*presence* tests prefer
``app.openapi()["paths"]``; reach for this helper only when the assertion
needs route ordering or a route attribute OpenAPI does not expose (e.g. the
handler ``name``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


def iter_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Yield leaf routes from a FastAPI 0.137+ route tree, in registration order.

    ``_IncludedRouter`` wrappers (produced by ``include_router()`` under
    0.137) carry no ``path``/``methods`` of their own; their real routes
    hang off ``original_router.routes``. Descend through those so callers
    see the same flat, ordered sequence the pre-0.137 ``routes`` list gave.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from iter_routes(original.routes)
        else:
            yield route
