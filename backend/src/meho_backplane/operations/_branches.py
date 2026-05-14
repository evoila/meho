# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Source-kind branch handlers for the G0.6 dispatcher.

The dispatcher (T5, #396) branches on ``EndpointDescriptor.source_kind``
to execute the op. This module hosts the three branches:

* :func:`dispatch_ingested` -- ``source_kind='ingested'``. Substitutes
  path vars via the ``x-meho-param-loc`` extension; calls the
  connector's HTTP transport (:meth:`HttpConnector._request_json` /
  :meth:`HttpConnector._post_json`).
* :func:`dispatch_typed` -- ``source_kind='typed'``. Inspects the
  handler's signature and invokes it with either
  ``(operator, target, params)`` or ``(target, params)`` per the
  parameter names.
* :func:`dispatch_composite` -- ``source_kind='composite'``. Invokes
  the handler with the dispatcher seam so the handler can recurse.

The branch handlers are wire-format-agnostic at this layer; the
:func:`~meho_backplane.operations.dispatcher.dispatch` function owns
the audit + broadcast + result wrapping that surrounds these calls.

Path-template substitution for ingested ops lives in
:func:`_substitute_path`; param-bucket splitting per
``x-meho-param-loc`` lives in :func:`_split_ingested_params`. Both are
module-internal; the public surface is the three ``dispatch_*``
functions.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "dispatch_composite",
    "dispatch_ingested",
    "dispatch_typed",
]


# JSON Schema extension key carrying the OpenAPI parameter location
# (``"path"`` / ``"query"`` / ``"header"`` / ``"body"``) per property
# of an ingested op's parameter_schema. Set by the G0.7 ingestion
# pipeline; the dispatcher reads it here to split incoming params
# into URL path substitution / query-string / request body.
_PARAM_LOC_KEY = "x-meho-param-loc"

# Path-template substitution pattern. ``"/api/vcenter/cluster/{cluster}"``
# matches ``{cluster}``; the substituted value is :func:`urllib.parse.quote`-d
# at substitution time (RFC 3986 reserved chars only).
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")


def _split_ingested_params(
    parameter_schema: dict[str, Any],
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Split *params* by ``x-meho-param-loc`` into (path, query, header, body) buckets.

    Algorithm: walk ``parameter_schema["properties"]``; for each
    property, read the ``x-meho-param-loc`` extension (default
    ``"query"`` -- the most common OpenAPI shape) and route the
    matching params dict entry to the corresponding bucket. Params
    not declared in the schema fall through to the **body** bucket --
    the OpenAPI convention for free-form request bodies.

    The four buckets are returned even when empty so the caller's
    request-building branch can pass them positionally without
    defensive ``or {}`` shims.
    """
    props: dict[str, Any] = (parameter_schema or {}).get("properties") or {}
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    header_params: dict[str, Any] = {}
    body_params: dict[str, Any] = {}
    for name, value in params.items():
        prop_schema = props.get(name) or {}
        loc = prop_schema.get(_PARAM_LOC_KEY, "query")
        if loc == "path":
            path_params[name] = value
        elif loc == "header":
            header_params[name] = value
        elif loc == "body":
            body_params[name] = value
        else:
            query_params[name] = value
    return path_params, query_params, header_params, body_params


def _substitute_path(path_template: str, path_params: dict[str, Any]) -> str:
    """Substitute ``{var}`` placeholders in *path_template* from *path_params*.

    Missing path vars raise :class:`KeyError` so the dispatcher's
    caller surfaces them as ``invalid_params`` rather than producing
    a request with literal ``{var}`` in the URL. RFC 3986 path
    reserved chars in the substituted value are percent-encoded by
    :func:`urllib.parse.quote` (safe set: empty, so ``/`` in a value
    is also encoded -- matches OpenAPI's default path-style).
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in path_params:
            raise KeyError(f"path template requires {name!r} but it was not supplied")
        return quote(str(path_params[name]), safe="")

    return _PATH_VAR_RE.sub(_replace, path_template)


async def dispatch_ingested(
    *,
    connector: Connector,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> Any:
    """Execute an ``source_kind='ingested'`` op via the connector's HTTP transport.

    v0.2 routes through :meth:`HttpConnector._request_json` for idempotent
    verbs (GET / HEAD / OPTIONS) -- the only verbs the ingest pipeline
    declares as safe-to-retry. POST / PUT / DELETE / PATCH route through
    :meth:`HttpConnector._post_json` (the same client pool, no retry).
    The connector instance MUST be an :class:`HttpConnector` -- the
    dispatcher type-checks via ``hasattr(connector, "_request_json")``
    rather than an :func:`isinstance` import to avoid a circular
    dependency on the adapters package.

    The v0.2 ingest pipeline (G0.7, not yet shipped) does not exist;
    consequently no ``source_kind='ingested'`` rows live in the DB
    today. The branch ships anyway so:

    * G0.7 can land a single ingested descriptor and the dispatcher
      routes it without code change.
    * The mock-based test asserting "GET /api/test/{id} -> the right
      httpx call" can pin the contract before G0.7 lands.
    """
    method = (descriptor.method or "").upper()
    path_template = descriptor.path or ""
    if not method or not path_template:
        raise RuntimeError(
            f"ingested descriptor {descriptor.op_id!r} missing method or path "
            f"(method={descriptor.method!r}, path={descriptor.path!r})"
        )
    path_params, query_params, _header_params, body_params = _split_ingested_params(
        descriptor.parameter_schema,
        params,
    )
    substituted = _substitute_path(path_template, path_params)
    raw_jwt = operator.raw_jwt
    if method in ("GET", "HEAD", "OPTIONS"):
        request_json = getattr(connector, "_request_json", None)
        if request_json is None:
            raise RuntimeError(
                f"connector {type(connector).__name__} has no _request_json "
                f"(ingested dispatch requires HttpConnector)"
            )
        return await request_json(
            target,
            method,
            substituted,
            raw_jwt=raw_jwt,
            params=query_params or None,
            json=body_params or None,
        )
    # Non-idempotent verb -- POST / PUT / PATCH / DELETE. v0.2 routes
    # through ``_post_json``-shaped httpx call (no retry). The connector
    # owns the auth header injection + per-target client pool.
    post_json = getattr(connector, "_post_json", None)
    if post_json is None:
        raise RuntimeError(
            f"connector {type(connector).__name__} has no _post_json "
            f"(ingested non-idempotent dispatch requires HttpConnector)"
        )
    return await post_json(
        target,
        substituted,
        raw_jwt=raw_jwt,
        json=body_params or None,
    )


async def dispatch_typed(
    *,
    handler: Callable[..., Awaitable[Any]],
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> Any:
    """Invoke a ``source_kind='typed'`` handler with the per-handler signature.

    Two handler signatures land in production today:

    * **Module-level function** -- ``async def foo(operator, target,
      params)``. The dispatcher passes all three by keyword.
    * **Bound method** -- ``async def
      ConnectorCls.foo(self, target, params)``. The caller is expected
      to have rebound the handler against the connector instance the
      resolver chose; the signature introspection then sees ``self``
      already absorbed and routes by parameter name.

    The signature introspection is keyed on parameter **names** rather
    than count -- ``"operator"`` in the signature -> pass it; otherwise
    invoke with ``(target, params)``. This matches the two registered
    shapes in the v0.2 codebase (module-level helpers accept operator;
    connector-bound methods don't).
    """
    sig = inspect.signature(handler)
    param_names = list(sig.parameters.keys())
    # Drop ``self`` if present (unbound-method shape that wasn't rebound).
    if param_names and param_names[0] == "self":
        param_names = param_names[1:]
    if "operator" in param_names:
        return await handler(operator=operator, target=target, params=params)
    return await handler(target=target, params=params)


async def dispatch_composite(
    *,
    handler: Callable[..., Awaitable[Any]],
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: Callable[..., Awaitable[Any]],
) -> Any:
    """Invoke a ``source_kind='composite'`` handler with the dispatcher seam.

    Composite handlers receive ``(operator, target, params,
    dispatch_child)``; they call ``dispatch_child(...)`` recursively for
    sub-ops. The ``dispatch_child`` callable is built by
    :func:`~meho_backplane.operations.composite.get_dispatch_child` and
    wraps :func:`~meho_backplane.operations.dispatcher.dispatch` so that
    the audit-tree linkage (``parent_audit_id_var`` contextvar -> real
    ``audit_log.parent_audit_id`` column) and bounded-recursion guard
    (``composite_depth_var`` contextvar checked against
    :attr:`~meho_backplane.settings.Settings.composite_max_depth`) are
    applied automatically -- the handler reads as plain business logic.

    The keyword the handler sees is ``dispatch_child`` (not raw
    ``dispatch``); composite handlers annotate the parameter against
    the :class:`~meho_backplane.operations.composite.DispatchChild`
    Protocol for static type checking.
    """
    return await handler(
        operator=operator,
        target=target,
        params=params,
        dispatch_child=dispatch_child,
    )
