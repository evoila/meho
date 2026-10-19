# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder span for every Kubernetes API HTTP round-trip (#3841).

Every REST call the connector makes through ``kubernetes_asyncio`` -- the typed
``CoreV1Api`` / ``AppsV1Api`` calls and the ``DynamicClient`` used by
``k8s.apply`` -- ends in one method:
``kubernetes_asyncio.client.rest.RESTClientObject.request`` (36.1.0). Its
``GET`` / ``POST`` / ``PATCH`` / ... helpers all call ``self.request``, so
replacing ``request`` on the client instance covers them all.

:func:`instrument_api_client` does that once, when the connector builds a
cached ``ApiClient``. The wrapper records a ``vendor_call`` span through
:func:`meho_backplane.flight_recorder.sdk_http.record_sdk_vendor_call`:
method, path, redacted query, status, redacted request/response headers,
redacted bodies, and the response object's metadata (``uid``,
``resourceVersion``, ``creationTimestamp``, ``generation``, a
``managedFields`` summary). Annotations, labels, and ``managedFields``
``fieldsV1`` are never copied into the metadata summary: annotations can hold
``last-applied-configuration`` with secret data, and ``fieldsV1`` lists field
names.

When capture is off the wrapper costs one contextvar read. It never changes
what the SDK returns or raises: an ``ApiException`` and any transport error
are re-raised unchanged after the span is recorded.

Not covered: the websocket client (``WsApiClient``) used by ``k8s.exec``. It
connects through ``aiohttp`` websockets, not ``RESTClientObject.request``.
"""

from __future__ import annotations

import json
from typing import Any, Final

import structlog
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from meho_backplane.flight_recorder.capture import SpanStart, span_start
from meho_backplane.flight_recorder.sdk_http import record_sdk_vendor_call

__all__ = ["SAFE_QUERY_NAMES", "instrument_api_client"]

_log = structlog.get_logger(__name__)

#: Marker attribute so a client is never wrapped twice.
_INSTRUMENTED_ATTR: Final[str] = "_meho_flight_recorder_wrapped"

#: Kubernetes API query parameters whose values are safe to record. They
#: are request switches and selectors, not credentials. ``continue`` (a
#: pagination token) is left out on purpose.
SAFE_QUERY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "dryRun",
        "fieldManager",
        "fieldValidation",
        "force",
        "pretty",
        "propagationPolicy",
        "gracePeriodSeconds",
        "orphanDependents",
        "limit",
        "labelSelector",
        "fieldSelector",
        "resourceVersion",
        "resourceVersionMatch",
        "timeoutSeconds",
        "watch",
        "allowWatchBookmarks",
        "container",
        "follow",
        "previous",
        "sinceSeconds",
        "sinceTime",
        "tailLines",
        "timestamps",
        "limitBytes",
    }
)

#: Above this size the response body is not parsed for the metadata
#: summary, so a huge list never costs a second full JSON parse.
_MAX_METADATA_PARSE_BYTES: Final[int] = 2 * 1024 * 1024

#: Upper bound on ``managedFields`` entries copied into the summary.
_MAX_MANAGED_FIELDS: Final[int] = 20

_METADATA_SCALARS: Final[tuple[str, ...]] = (
    "name",
    "namespace",
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "deletionTimestamp",
    "generation",
)
_MANAGED_FIELD_KEYS: Final[tuple[str, ...]] = (
    "manager",
    "operation",
    "apiVersion",
    "time",
    "subresource",
)


def instrument_api_client(api_client: client.ApiClient) -> client.ApiClient:
    """Wrap *api_client*'s REST transport so each request records a span.

    Safe to call twice (the second call does nothing). Returns *api_client*.
    """
    rest = api_client.rest_client
    if getattr(rest, _INSTRUMENTED_ATTR, False):
        return api_client
    original = rest.request

    async def _recorded_request(
        method: str,
        url: str,
        query_params: Any = None,
        headers: Any = None,
        body: Any = None,
        post_params: Any = None,
        _preload_content: bool = True,
        _request_timeout: Any = None,
    ) -> Any:
        start = span_start()
        if start is None:
            return await original(
                method,
                url,
                query_params=query_params,
                headers=headers,
                body=body,
                post_params=post_params,
                _preload_content=_preload_content,
                _request_timeout=_request_timeout,
            )
        call = _Call(start, method, url, query_params, headers, body)
        try:
            response = await original(
                method,
                url,
                query_params=query_params,
                headers=headers,
                body=body,
                post_params=post_params,
                _preload_content=_preload_content,
                _request_timeout=_request_timeout,
            )
        except ApiException as exc:
            call.record(str(exc.status), exc.headers, exc.body)
            raise
        except BaseException as exc:
            call.record(f"error:{type(exc).__name__}", None, None)
            raise
        await call.record_response(response, query_params)
        return response

    rest.request = _recorded_request  # type: ignore[method-assign]
    setattr(rest, _INSTRUMENTED_ATTR, True)
    return api_client


class _Call:
    """The request half of one round-trip, kept until the response arrives."""

    __slots__ = ("body", "headers", "method", "query", "start", "url")

    def __init__(
        self, start: SpanStart, method: str, url: str, query: Any, headers: Any, body: Any
    ) -> None:
        self.start = start
        self.method = str(method).upper()
        self.url = url
        self.query = query
        self.headers = headers
        self.body = body

    def record(self, status: str, response_headers: Any, response_body: Any) -> None:
        """Hand the round-trip to the recorder. Never raises (F7)."""
        try:
            body = _as_bytes(response_body)
            record_sdk_vendor_call(
                self.start,
                method=self.method,
                url=self.url,
                status=status,
                request_headers=self.headers,
                request_body=self.body,
                response_headers=response_headers,
                response_body=body,
                query=self.query,
                safe_query_names=SAFE_QUERY_NAMES,
                extra=_response_object_extra(body),
            )
        except Exception:
            _log.warning("kubernetes_wire_span_failed", exc_info=True)

    async def record_response(self, response: Any, query: Any) -> None:
        """Record a returned response (preloaded or raw ``aiohttp``). Never raises."""
        try:
            status = str(getattr(response, "status", ""))
            if hasattr(response, "aiohttp_response"):
                # ``RESTResponse``: body already read by the SDK.
                self.record(status, response.getheaders(), response.data)
                return
            # Raw ``aiohttp.ClientResponse`` (``_preload_content=False``, as
            # ``DynamicClient`` uses). ``read()`` caches the body, so the
            # caller's later ``json()`` / ``read()`` gets the same bytes. A
            # watch / follow stream is never read here: it would not end.
            body = None if _is_stream(query) else await response.read()
            self.record(status, response.headers, body)
        except Exception:
            _log.warning("kubernetes_wire_span_failed", exc_info=True)


def _is_stream(query: Any) -> bool:
    try:
        pairs = query.items() if isinstance(query, dict) else (query or ())
        return any(
            str(name) in {"watch", "follow"} and str(value).lower() in {"true", "1"}
            for name, value in pairs
        )
    except Exception:
        return True


def _as_bytes(body: Any) -> bytes | None:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8", "replace")
    return None


def _response_object_extra(body: bytes | None) -> dict[str, Any] | None:
    """Summarise the response object's metadata. Never raises."""
    if not body or len(body) > _MAX_METADATA_PARSE_BYTES:
        return None
    try:
        obj = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    summary: dict[str, Any] = {
        key: obj[key] for key in ("kind", "apiVersion") if isinstance(obj.get(key), str)
    }
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        summary.update(_metadata_summary(metadata))
    items = obj.get("items")
    if isinstance(items, list):
        summary["item_count"] = len(items)
    return {"response_object": summary} if summary else None


def _metadata_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    """Copy only the listed scalar fields and a ``managedFields`` summary."""
    summary: dict[str, Any] = {}
    for key in _METADATA_SCALARS:
        value = metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            summary[key] = value
    managed = metadata.get("managedFields")
    if isinstance(managed, list):
        summary["managedFields"] = [
            {k: entry[k] for k in _MANAGED_FIELD_KEYS if isinstance(entry.get(k), str)}
            for entry in managed[:_MAX_MANAGED_FIELDS]
            if isinstance(entry, dict)
        ]
    return summary
