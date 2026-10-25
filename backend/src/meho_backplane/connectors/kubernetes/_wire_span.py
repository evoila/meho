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

Secret payloads are masked before capture. A JSON request or response body
is parsed here and passed through the connector's structural Secret
redactor (:func:`meho_backplane.connectors.kubernetes.redaction.redact_kubernetes_payload`),
so every ``data`` / ``stringData`` value of a ``kind: Secret`` object -- in a
``k8s.apply`` manifest, a single read, or the items of a ``SecretList`` --
reaches the recorder only as a key name plus a digest. The shared redaction
path then runs over that masked body as usual. The metadata summary is
built from the body **after** redaction, so a body-path rule or the
credential-shape scrub applies to the summary too.

A response body over the span body cap (64 KiB) is not recorded at all. The
shared redactor cuts an oversized body at the cap, and a cut body can still
parse as JSON (for example when only a trailing newline was cut), so Secret
data in it would be stored. The hook therefore hands over no body, and the
recorder stores the omission marker, the reason, and marks the trace
uncertain (operator-only). Request bodies are masked whole before the cap.

When the response body cannot be read (a timeout or a broken payload while
reading a raw ``aiohttp`` response), the span is still recorded with the
request, the HTTP status, and the response headers; only the body is left
out. The trace must not lose the request when an apply's outcome is unclear.

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

from meho_backplane.connectors.kubernetes.redaction import redact_kubernetes_payload
from meho_backplane.flight_recorder._capture_util import MAX_SPAN_BODY_BYTES
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

#: The one non-``json`` content type the SDK still sends as JSON text
#: (server-side apply). Bodies of this type are parsed for Secret masking.
_APPLY_PATCH_CONTENT_TYPE: Final[str] = "application/apply-patch+yaml"

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
            raw = _as_bytes(response_body)
            omitted = _oversize_reason(raw)
            record_sdk_vendor_call(
                self.start,
                method=self.method,
                url=self.url,
                status=status,
                request_headers=self.headers,
                request_body=_masked_body(self.body, _content_type(self.headers)),
                response_headers=response_headers,
                response_body=(
                    None if omitted else _masked_bytes(raw, _content_type(response_headers))
                ),
                response_body_omitted=omitted,
                query=self.query,
                safe_query_names=SAFE_QUERY_NAMES,
                response_summary=_response_object_summary,
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
            headers = response.headers
        except Exception:
            _log.warning("kubernetes_wire_span_failed", exc_info=True)
            return
        # Raw ``aiohttp.ClientResponse`` (``_preload_content=False``, as
        # ``DynamicClient`` uses). ``read()`` caches the body, so the
        # caller's later ``json()`` / ``read()`` gets the same bytes. A
        # watch / follow stream is never read here: it would not end.
        body = None
        if not _is_stream(query):
            try:
                body = await response.read()
            except Exception:
                # The caller's own read raises the same error. The span
                # still gets the request, status, and headers.
                _log.warning("kubernetes_wire_span_body_unread", exc_info=True)
        self.record(status, headers, body)


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


def _content_type(headers: Any) -> str | None:
    """Case-insensitive ``content-type`` read from any header mapping; never raises."""
    try:
        for key, value in headers.items():
            if str(key).lower() == "content-type" and isinstance(value, str):
                return value
    except Exception:
        return None
    return None


def _is_json_like(content_type: str | None) -> bool:
    """``True`` when the body is JSON text the Secret masking pass can parse.

    An absent content type counts as JSON-like, as in the shared redactor,
    which also attempts a JSON parse then.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base in (
        "",
        "application/json",
        "text/json",
        _APPLY_PATCH_CONTENT_TYPE,
    ) or base.endswith("+json")


def _masked_body(body: Any, content_type: str | None) -> Any:
    """Mask Kubernetes Secret data in a request body before it is recorded.

    The SDK hands the transport a parsed ``dict`` (typed and dynamic calls
    alike); JSON text is parsed first. Any other shape passes through
    unchanged and the shared redactor decides what to do with it.
    """
    if isinstance(body, (dict, list)):
        return redact_kubernetes_payload(body)
    if isinstance(body, (str, bytes, bytearray)) and _is_json_like(content_type):
        parsed = _parse_json(body)
        if isinstance(parsed, (dict, list)):
            return redact_kubernetes_payload(parsed)
    return body


def _oversize_reason(body: bytes | None) -> str | None:
    """The omission reason for a response body over the span cap, else ``None``.

    An oversized body is never handed to the recorder: cut at the cap it may
    still parse as JSON, and the Secret masking below did not run over it.
    """
    if body is None or len(body) <= MAX_SPAN_BODY_BYTES:
        return None
    return (
        f"response body not recorded: {len(body)} bytes is over the "
        f"{MAX_SPAN_BODY_BYTES}-byte span cap, so Secret masking could not be "
        "proven (fail-closed)"
    )


def _masked_bytes(body: bytes | None, content_type: str | None) -> bytes | None:
    """Mask Kubernetes Secret data in a JSON response body; keep it as bytes.

    A body that is not JSON passes through unchanged. Oversized bodies never
    reach here (see :func:`_oversize_reason`).
    """
    if body is None or not _is_json_like(content_type):
        return body
    parsed = _parse_json(body)
    if not isinstance(parsed, (dict, list)):
        return body
    masked = redact_kubernetes_payload(parsed)
    return json.dumps(masked, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_json(body: str | bytes | bytearray) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _response_object_summary(body: Any) -> dict[str, Any] | None:
    """Summarise the metadata of a **redacted** response object. Never raises.

    *body* is what the recorder will store as ``response_body``: a parsed
    structure, ``None``, or an omission marker. Only a JSON object gives a
    summary; whatever a redaction rule replaced in it is replaced here too.
    """
    if not isinstance(body, dict):
        return None
    summary: dict[str, Any] = {
        key: body[key] for key in ("kind", "apiVersion") if isinstance(body.get(key), str)
    }
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        summary.update(_metadata_summary(metadata))
    items = body.get("items")
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
