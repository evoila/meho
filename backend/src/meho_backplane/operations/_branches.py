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
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "IngestedRequest",
    "dispatch_composite",
    "dispatch_ingested",
    "dispatch_typed",
    "resolve_ingested_request",
]


# JSON Schema extension key carrying the OpenAPI parameter location
# (``"path"`` / ``"query"`` / ``"header"`` / ``"body"``) per property
# of an ingested op's parameter_schema. Set by the G0.7 ingestion
# pipeline; the dispatcher reads it here to split incoming params
# into URL path substitution / query-string / request body.
_PARAM_LOC_KEY = "x-meho-param-loc"

# Path-template substitution pattern. Captures an optional leading
# RFC6570 expression operator and the bare expression body:
#
# * ``{cluster}``      -> operator ``""``,  name ``"cluster"``     (simple
#   expansion, RFC6570 §3.2.2 -- reserved chars in the value are encoded)
# * ``{+constraints}`` -> operator ``"+"``, name ``"constraints"`` (reserved
#   expansion, RFC6570 §3.2.3 -- reserved/gen-delim chars pass through literal)
#
# The operator is stripped from the captured name so the param lookup
# keys on the bare variable name the ingest pipeline registers (``path``,
# not ``+path``) -- without the strip a literal ``{+path}`` spec would
# ``KeyError`` even though the param is supplied. Only ``+`` (and ``#``)
# change the encoding safe set; the remaining operator chars are captured
# so the regex never mis-includes a leading operator in the name, even for
# operators this substituter does not (yet) special-case.
_PATH_VAR_RE = re.compile(r"\{([+#./;?&]?)([^{}]+)\}")

# RFC6570 §3.2.3 reserved-expansion safe set: the gen-delims + sub-delims
# from RFC3986 §2.2 that a ``{+var}`` / ``{#var}`` expression leaves
# unencoded so they keep their structural meaning in the URL. Genuinely
# unsafe characters (space, control chars) are still percent-encoded by
# :func:`urllib.parse.quote` because they are absent from this set.
_RFC6570_RESERVED_SAFE = ":/?#[]@!$&'()*+,;="


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


def _unwrap_body(body_params: dict[str, Any]) -> Any:
    """Return the request body to send for the ``loc=="body"`` bucket.

    An ingested op models its OpenAPI ``requestBody`` as a *single*
    parameter (named ``body``, tagged ``x-meho-param-loc: "body"`` by the
    G0.7 ingester -- see ``ingest.openapi``'s parameter-schema builder).
    :func:`_split_ingested_params` collects it into ``body_params`` keyed
    by that parameter name. The HTTP request body is that param's
    **value** -- not a ``{name: value}`` wrapper. Returning the wrapper
    would serialize ``{"body": {"title": "X"}}`` onto the wire, so an
    upstream that expects the requestBody schema at the top level (GitHub's
    issue-create wants ``{"title": "X"}``) rejects it as malformed (422).

    Empty bucket -> ``None`` (no body; httpx omits the request body).
    Exactly one entry -> its value, unwrapped. The single-entry shape is
    an ingest invariant; should a malformed descriptor ever carry more
    than one ``loc=="body"`` property, fail loudly rather than silently
    pick one and send a body the caller never asked for.
    """
    if not body_params:
        return None
    if len(body_params) > 1:
        raise RuntimeError(
            "ingested op declares multiple 'body' params "
            f"{sorted(body_params)!r}; requestBody must be a single container "
            "param (x-meho-param-loc='body'). This is an ingest-modelling fault."
        )
    return next(iter(body_params.values()))


def _substitute_path(path_template: str, path_params: dict[str, Any]) -> str:
    """Substitute ``{var}`` / ``{+var}`` placeholders in *path_template*.

    Honours RFC6570 expression operators so an ingested op's path
    template expands with the encoding the spec author intended:

    * **Simple expansion** ``{var}`` (RFC6570 §3.2.2) -- the value is
      percent-encoded with an empty safe set, so reserved chars like
      ``/`` become ``%2F`` (OpenAPI's default ``style: simple`` path
      parameter behaviour: a value cannot leak a path separator).
    * **Reserved expansion** ``{+var}`` / ``{#var}`` (RFC6570 §3.2.3) --
      the value is encoded with the reserved/gen-delim safe set
      (:data:`_RFC6570_RESERVED_SAFE`), so structural chars (``/``,
      ``:``, ``,`` ...) pass through literal. This is the form a path
      that carries a *sub-path* expression uses, e.g. vRLI's
      ``/api/v2/events/{+constraints}`` where the constraint is itself a
      slash-delimited segment chain (``text/CONTAINS error/hostname/...``).

    In both forms a genuinely-unsafe character (space, control chars) is
    still percent-encoded -- only the reserved structural chars differ.

    Missing path vars raise :class:`KeyError` so the dispatcher's caller
    surfaces them as ``invalid_params`` rather than producing a request
    with a literal ``{var}`` in the URL. The lookup is keyed on the bare
    variable name (operator stripped), so ``{+constraints}`` resolves the
    param named ``constraints``.
    """

    def _replace(match: re.Match[str]) -> str:
        operator, name = match.group(1), match.group(2)
        if name not in path_params:
            raise KeyError(f"path template requires {name!r} but it was not supplied")
        safe = _RFC6570_RESERVED_SAFE if operator in ("+", "#") else ""
        return quote(str(path_params[name]), safe=safe)

    return _PATH_VAR_RE.sub(_replace, path_template)


@dataclass(frozen=True)
class IngestedRequest:
    """The literal HTTP request an ingested-op dispatch would put on the wire.

    G0.24 follow-up (#1683). The same four artefacts
    :func:`dispatch_ingested` hands to the connector's HTTP transport,
    captured as data so they can be *returned* (the read-only dispatch
    preview) instead of only being sent. The body here is the **raw**
    unwrapped request body -- redaction is the dispatcher's
    connector-boundary concern (it owns the policy resolution), so this
    layer stays free of a redaction import and the preview path runs the
    raw body through the exact same
    :func:`~meho_backplane.redaction.middleware.apply_connector_boundary_redaction`
    pipeline the response path uses.

    Attributes:
        method: The upper-cased HTTP verb (``GET`` / ``POST`` / ...).
        path: The fully resolved request path -- ``{var}`` placeholders
            substituted *and* the connector's ``mount_op_path`` prefix
            applied, exactly as the wire call receives it.
        query: The query-string params bucket (``loc=="query"``), or
            ``None`` when empty -- the same value passed as httpx's
            ``params=`` on the GET path.
        body: The raw, unwrapped JSON request body (``loc=="body"``), or
            ``None`` when the op declares no body param -- the same value
            passed as httpx's ``json=``.
        headers: The header-located params bucket (``loc=="header"``), or
            ``None`` when the op declares no header param -- merged onto the
            connector's :meth:`~meho_backplane.connectors.adapters.http.HttpConnector.auth_headers`
            as the transport's ``extra_headers=``.
    """

    method: str
    path: str
    query: dict[str, Any] | None
    body: Any
    headers: dict[str, Any] | None


async def resolve_ingested_request(
    *,
    connector: Connector,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> IngestedRequest:
    """Resolve an ingested op + params to the literal HTTP request, without sending.

    The single source of truth for "what method / path / query / body
    does an ``source_kind='ingested'`` dispatch put on the wire" -- shared
    verbatim by :func:`dispatch_ingested` (which then *sends* it) and the
    read-only dispatch preview (#1683, which *returns* it). Keeping the
    resolution in one place means the preview can never drift from the
    real request: the path substitution, the ``mount_op_path`` prefix
    application, the requestBody unwrap (#1656) all run identically.

    Raises the same :class:`RuntimeError` (missing method/path) and
    :class:`KeyError` (unsubstituted path var) the dispatch path raised
    inline, so both the execute and the preview surface them through the
    dispatcher's structured-error mapping unchanged.
    """
    method = (descriptor.method or "").upper()
    path_template = descriptor.path or ""
    if not method or not path_template:
        raise RuntimeError(
            f"ingested descriptor {descriptor.op_id!r} missing method or path "
            f"(method={descriptor.method!r}, path={descriptor.path!r})"
        )
    path_params, query_params, header_params, body_params = _split_ingested_params(
        descriptor.parameter_schema,
        params,
    )
    substituted = _substitute_path(path_template, path_params)
    # Vendor connectors may expose ingested ops under a mount prefix
    # the spec omits (vCenter REST: ``/api`` modern / ``/rest`` legacy).
    # ``mount_op_path`` defaults to identity on HttpConnector; the
    # getattr keeps this branch tolerant of a connector that predates
    # the hook rather than hard-failing on a missing attribute.
    mount_op_path = getattr(connector, "mount_op_path", None)
    if mount_op_path is not None:
        substituted = await mount_op_path(target, substituted, operator)
    return IngestedRequest(
        method=method,
        path=substituted,
        query=query_params or None,
        body=_unwrap_body(body_params),
        headers=header_params or None,
    )


def _profile_pagination(connector: Connector) -> Any:
    """Return the connector's profiled ``cursor_token`` pagination, or ``None``.

    Reads ``connector.profile.pagination`` (present only on a
    :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` that
    carries a stamped :class:`~meho_backplane.connectors.profile.ExecutionProfile`)
    via ``getattr`` so the ingested-dispatch branch stays tolerant of a
    plain :class:`HttpConnector` with no profile. Returns the
    :class:`~meho_backplane.connectors.profile.PaginationSpec` only when its
    strategy is ``cursor_token`` (the one strategy that drives a loop);
    ``strategy='none'`` and a profile-less connector both yield ``None`` so
    the caller falls through to the single-request path.
    """
    profile = getattr(connector, "profile", None)
    if profile is None:
        return None
    pagination = getattr(profile, "pagination", None)
    if pagination is None or pagination.strategy != "cursor_token":
        return None
    return pagination


async def _dispatch_ingested_cursor_token(
    *,
    request_json: Any,
    target: Any,
    request: IngestedRequest,
    operator: Operator,
    pagination: Any,
) -> dict[str, Any]:
    """Follow a ``cursor_token`` pagination loop, concatenating each page's rows.

    Each iteration GETs the same path with the next cursor merged into the
    query under ``pagination.cursor.req_param``, unwraps the rows from the
    literal top-level ``pagination.items_key``, and reads the next cursor
    from the literal top-level ``pagination.cursor.resp_field``. The loop
    stops when that field is falsy (absent / empty). Returns the assembled
    set under the same ``items_key`` plus a ``total`` count — the unwrapped,
    cursor-free shape the reducer / agent consumes, mirroring the hand-coded
    gcloud paginators (``{rows, total}``).

    A page whose ``items_key`` is missing or not a list is treated as a
    zero-row page (a vendor that signals "no more" with an empty body),
    keeping the loop robust against a trailing empty page.
    """
    items_key = pagination.items_key
    cursor = pagination.cursor
    rows: list[Any] = []
    page_token: str | None = None
    while True:
        query = dict(request.query or {})
        if page_token:
            query[cursor.req_param] = page_token
        payload = await request_json(
            target,
            request.method,
            request.path,
            operator=operator,
            params=query or None,
            json=request.body,
            extra_headers=request.headers,
        )
        page_rows = payload.get(items_key) if isinstance(payload, dict) else None
        if isinstance(page_rows, list):
            rows.extend(page_rows)
        next_token = payload.get(cursor.resp_field) if isinstance(payload, dict) else None
        if not next_token:
            break
        page_token = str(next_token)
    return {items_key: rows, "total": len(rows)}


async def dispatch_ingested(
    *,
    connector: Connector,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> Any:
    """Execute an ``source_kind='ingested'`` op via the connector's HTTP transport.

    Routes through :meth:`HttpConnector._request_json` for idempotent
    verbs (GET / HEAD / OPTIONS) -- the only verbs the ingest pipeline
    declares as safe-to-retry. POST / PUT / DELETE / PATCH route through
    :meth:`HttpConnector._post_json` (the same client pool, no retry),
    each carrying its *actual* declared verb -- a PUT/PATCH/DELETE is no
    longer silently downgraded to a POST. Header-located params are
    forwarded to both transport seams as ``extra_headers``.
    The connector instance MUST be an :class:`HttpConnector` -- the
    dispatcher type-checks via ``hasattr(connector, "_request_json")``
    rather than an :func:`isinstance` import to avoid a circular
    dependency on the adapters package.

    The literal method / path / query / body are resolved by
    :func:`resolve_ingested_request` -- the same resolver the read-only
    dispatch preview (#1683) calls, so the previewed request can never
    drift from what this branch actually sends.

    The v0.2 ingest pipeline (G0.7, not yet shipped) does not exist;
    consequently no ``source_kind='ingested'`` rows live in the DB
    today. The branch ships anyway so:

    * G0.7 can land a single ingested descriptor and the dispatcher
      routes it without code change.
    * The mock-based test asserting "GET /api/test/{id} -> the right
      httpx call" can pin the contract before G0.7 lands.
    """
    request = await resolve_ingested_request(
        connector=connector,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
    )
    # Thread the full Operator (not just operator.raw_jwt) to the HTTP
    # transport: the connector's credential loader resolves the per-target
    # secret under the operator's identity (operator-context Vault read).
    # See docs/architecture/connector-auth.md.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        request_json = getattr(connector, "_request_json", None)
        if request_json is None:
            raise RuntimeError(
                f"connector {type(connector).__name__} has no _request_json "
                f"(ingested dispatch requires HttpConnector)"
            )
        # A profiled connector whose declarative pagination strategy is
        # ``cursor_token`` drives a follow-the-cursor loop here, assembling
        # the full set the way the hand-coded gcloud paginators do; every
        # other connector (and ``strategy='none'``) makes a single request.
        pagination = _profile_pagination(connector)
        if pagination is not None:
            return await _dispatch_ingested_cursor_token(
                request_json=request_json,
                target=target,
                request=request,
                operator=operator,
                pagination=pagination,
            )
        return await request_json(
            target,
            request.method,
            request.path,
            operator=operator,
            params=request.query,
            json=request.body,
            extra_headers=request.headers,
        )
    # Non-idempotent verb -- POST / PUT / PATCH / DELETE. Routes through
    # ``_post_json`` (no retry), forwarding the *actual* declared verb so a
    # PUT/PATCH/DELETE reaches the wire with its real method rather than a
    # hardcoded POST. The connector owns the auth header injection + the
    # per-target client pool; header-located params ride along as
    # ``extra_headers``.
    post_json = getattr(connector, "_post_json", None)
    if post_json is None:
        raise RuntimeError(
            f"connector {type(connector).__name__} has no _post_json "
            f"(ingested non-idempotent dispatch requires HttpConnector)"
        )
    return await post_json(
        target,
        request.path,
        operator=operator,
        verb=request.method,
        json=request.body,
        extra_headers=request.headers,
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
    # A handler still carrying ``self`` here is an unbound connector
    # method the dispatcher failed to bind to an instance (resolver
    # miss / instance-cache miss). The previous code silently dropped
    # ``self`` and then called ``handler(target=, params=)`` anyway —
    # which always TypeErrors and was masked upstream as the misleading
    # ``handler_unreachable`` (#697, the green-but-hollow class). Fail
    # loud and accurately instead of mis-calling.
    if param_names and param_names[0] == "self":
        raise RuntimeError(
            f"typed handler {getattr(handler, '__qualname__', handler)!r} reached "
            f"dispatch still unbound (first parameter 'self'): the dispatcher could "
            f"not bind it to a connector instance. This is a connector-resolution / "
            f"instance-cache fault, not a missing handler — do not mask it as "
            f"handler_unreachable."
        )
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
