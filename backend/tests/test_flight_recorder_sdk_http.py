# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vendor-call spans for SDK-based typed connectors (#3841).

Covers the shared recorder (:func:`record_sdk_vendor_call`), the query
redactor it uses (:func:`redact_query`), and the Kubernetes transport hook
(:func:`instrument_api_client`) driven end to end through a real
``kubernetes_asyncio`` client against a local ``aiohttp`` server.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextvars import Token
from typing import Any

import pytest
from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from meho_backplane.connectors.kubernetes._wire_span import (
    _response_object_summary,
    instrument_api_client,
)
from meho_backplane.connectors.kubernetes.redaction import (
    LAST_APPLIED_ANNOTATION,
    REDACTED_PREFIX,
)
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.sdk_http import record_sdk_vendor_call
from meho_backplane.redaction.flight_recorder import (
    BODY_PATH_MARKER,
    HEADER_ALLOWLIST,
    QUERY_VALUE_MARKER,
    SECRET_FAMILY_OMITTED_MARKER,
    redact_query,
)

_TOKEN = "Bearer abc.def.ghi-secret-token-value"

# A base64 Secret value that matches no named credential pattern, so a green
# assertion proves the structural Secret masking fired, not the shape net.
_OPAQUE_B64 = "cGxhaW4tYXBwLXRva2VuLXdpdGgtbm8tbGFiZWw="


def _op(
    op_id: str = "k8s.apply", tags: tuple[str, ...] = (), body_paths: tuple[str, ...] = ()
) -> capture._OpContext:
    return capture._OpContext(
        op_id=op_id,
        connector_id="kubernetes",
        impl_id="kubernetes-1",
        product="kubernetes",
        tags=tags,
        body_paths=body_paths,
        # Set (not None) so the classifier does not read app settings.
        delete_shaped_patterns=(),
    )


@pytest.fixture
def scope() -> Iterator[capture._CaptureScope]:
    """Bind an active capture scope + op context, as the dispatcher does."""
    sc = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    cap_token: Token[Any] = capture._active_capture_var.set(sc)
    op_token: Token[Any] = capture._op_context_var.set(_op())
    try:
        yield sc
    finally:
        capture._op_context_var.reset(op_token)
        capture._active_capture_var.reset(cap_token)


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes)


# ---------------------------------------------------------------------------
# redact_query + header allowlist
# ---------------------------------------------------------------------------


def test_redact_query_keeps_safe_values_and_masks_the_rest() -> None:
    out = redact_query(
        [("dryRun", "All"), ("fieldManager", "meho"), ("continue", "opaque"), ("token", "s3cr3t")],
        {"dryRun", "fieldManager"},
    )
    assert out.uncertain is False
    assert out.value == (
        f"dryRun=All&fieldManager=meho&continue={QUERY_VALUE_MARKER}&token={QUERY_VALUE_MARKER}"
    )


def test_redact_query_accepts_mapping_and_scrubs_safe_values() -> None:
    out = redact_query({"labelSelector": _TOKEN, "limit": 5}, {"labelSelector", "limit"})
    assert "abc.def.ghi-secret-token-value" not in out.value
    assert "limit=5" in out.value


def test_redact_query_empty_and_unwalkable() -> None:
    assert redact_query(None, {"a"}).value is None
    bad = redact_query([1, 2], {"a"})
    assert bad.value is None
    assert bad.uncertain is True


def test_header_allowlist_has_kubernetes_diagnostics() -> None:
    assert {"warning", "audit-id", "server", "via"} <= HEADER_ALLOWLIST


# ---------------------------------------------------------------------------
# record_sdk_vendor_call
# ---------------------------------------------------------------------------


def test_record_sdk_vendor_call_noop_when_capture_off() -> None:
    record_sdk_vendor_call(None, method="GET", url="https://k/api", status="200")


def test_record_sdk_vendor_call_records_redacted_span(scope: capture._CaptureScope) -> None:
    record_sdk_vendor_call(
        capture.span_start(),
        method="PATCH",
        url="https://k8s.test:6443/api/v1/namespaces/demo?token=leak",
        status="201",
        request_headers={"Authorization": _TOKEN, "Content-Type": "application/apply-patch+yaml"},
        request_body={"metadata": {"name": "demo"}},
        response_headers={
            "Audit-Id": "a-1",
            "Set-Cookie": "s=1",
            "Content-Type": "application/json",
        },
        response_body=b'{"metadata": {"uid": "u-1"}}',
        query=[("dryRun", "All")],
        safe_query_names={"dryRun"},
        response_summary=lambda body: {"response_object": {"uid": body["metadata"]["uid"]}},
    )
    (span,) = scope.spans
    attrs = _attrs(span)
    assert span.span_kind == "vendor_call"
    assert span.status == "201"
    assert span.name == "PATCH https://k8s.test:6443/api/v1/namespaces/demo"
    assert attrs["query"] == "dryRun=All"
    assert "authorization" not in attrs["request_headers"]
    assert attrs["request_headers"]["content-type"] == "application/apply-patch+yaml"
    assert attrs["response_headers"] == {"audit-id": "a-1", "content-type": "application/json"}
    assert attrs["response_body"] == {"metadata": {"uid": "u-1"}}
    assert attrs["response_object"] == {"uid": "u-1"}
    assert "leak" not in json.dumps(attrs)
    assert scope.redaction_uncertain is False


def test_record_sdk_vendor_call_secret_family_omits_bodies(
    scope: capture._CaptureScope,
) -> None:
    token = capture._op_context_var.set(_op("k8s.secret_create", tags=("secret",)))
    try:
        record_sdk_vendor_call(
            capture.span_start(),
            method="POST",
            url="https://k8s.test/api/v1/namespaces/demo/secrets",
            status="201",
            request_body={"data": {"password": "aGVsbG8="}},
            response_body=b'{"data": {"password": "aGVsbG8="}}',
        )
    finally:
        capture._op_context_var.reset(token)
    (span,) = scope.spans
    assert span.attributes["request_body"] == SECRET_FAMILY_OMITTED_MARKER
    assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER


def test_response_summary_is_built_from_the_scrubbed_body(scope: capture._CaptureScope) -> None:
    """A credential shape removed from ``response_body`` never reappears in the summary."""
    raw = {"kind": "Namespace", "metadata": {"uid": "u-1", "managedFields": [{"manager": _TOKEN}]}}
    record_sdk_vendor_call(
        capture.span_start(),
        method="GET",
        url="https://k8s.test/api/v1/namespaces/demo",
        status="200",
        response_headers={"Content-Type": "application/json"},
        response_body=json.dumps(raw).encode(),
        response_summary=_response_object_summary,
    )
    (span,) = scope.spans
    attrs = _attrs(span)
    assert attrs["response_object"]["uid"] == "u-1"
    (entry,) = attrs["response_object"]["managedFields"]
    assert entry["manager"] == attrs["response_body"]["metadata"]["managedFields"][0]["manager"]
    assert "secret-token-value" not in json.dumps(attrs)
    assert scope.redaction_uncertain is False


def test_response_summary_honours_body_path_rules(scope: capture._CaptureScope) -> None:
    """A field a body-path rule removed is removed from the summary too."""
    token = capture._op_context_var.set(_op(body_paths=("metadata.uid",)))
    try:
        record_sdk_vendor_call(
            capture.span_start(),
            method="GET",
            url="https://k8s.test/api/v1/namespaces/demo",
            status="200",
            response_headers={"Content-Type": "application/json"},
            response_body=json.dumps(
                {"kind": "Namespace", "metadata": {"uid": "uid-hidden", "name": "demo"}}
            ).encode(),
            response_summary=_response_object_summary,
        )
    finally:
        capture._op_context_var.reset(token)
    (span,) = scope.spans
    attrs = _attrs(span)
    assert attrs["response_body"]["metadata"]["uid"] == BODY_PATH_MARKER
    assert attrs["response_object"] == {
        "kind": "Namespace",
        "name": "demo",
        "uid": BODY_PATH_MARKER,
    }
    assert "uid-hidden" not in json.dumps(attrs)
    assert scope.redaction_uncertain is False


def test_response_summary_skipped_when_body_not_recorded(scope: capture._CaptureScope) -> None:
    token = capture._op_context_var.set(_op("k8s.secret.create"))
    try:
        record_sdk_vendor_call(
            capture.span_start(),
            method="POST",
            url="https://k8s.test/api/v1/namespaces/demo/secrets",
            status="201",
            response_body=b'{"kind": "Secret", "metadata": {"uid": "u-1"}}',
            response_summary=_response_object_summary,
        )
    finally:
        capture._op_context_var.reset(token)
    (span,) = scope.spans
    assert "response_object" not in span.attributes
    assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER


def test_record_sdk_vendor_call_swallows_errors(
    scope: capture._CaptureScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("redaction down")

    monkeypatch.setattr(capture, "redact_span", _boom)
    record_sdk_vendor_call(capture.span_start(), method="GET", url="https://k/api", status="200")
    assert scope.spans == []


# ---------------------------------------------------------------------------
# Kubernetes transport hook, end to end through kubernetes_asyncio
# ---------------------------------------------------------------------------

_NAMESPACE = {
    "kind": "Namespace",
    "apiVersion": "v1",
    "metadata": {
        "name": "demo",
        "uid": "0b5c-uid",
        "resourceVersion": "4242",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "generation": 3,
        "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{secret}"},
        "managedFields": [
            {
                "manager": "meho",
                "operation": "Apply",
                "apiVersion": "v1",
                "time": "2026-01-01T00:00:00Z",
                "fieldsV1": {"f:data": {"f:password": {}}},
            }
        ],
    },
}
_K8S_HEADERS = {"Audit-Id": "audit-123", "Warning": '299 - "deprecated"', "Server": "kube"}

# What the API server returns for a Secret: the values are back in ``data``
# and once more inside the last-applied annotation.
_SECRET = {
    "kind": "Secret",
    "apiVersion": "v1",
    "type": "Opaque",
    "metadata": {
        "name": "app",
        "namespace": "demo",
        "uid": "sec-uid",
        "resourceVersion": "77",
        "annotations": {LAST_APPLIED_ANNOTATION: json.dumps({"data": {"tls.key": _OPAQUE_B64}})},
    },
    "data": {"tls.key": _OPAQUE_B64},
}
_SECRET_LIST = {
    "kind": "SecretList",
    "apiVersion": "v1",
    "metadata": {"resourceVersion": "78"},
    "items": [{k: v for k, v in _SECRET.items() if k not in ("kind", "apiVersion")}],
}


async def _namespace(request: web.Request) -> web.Response:
    if request.match_info["name"] == "missing":
        return web.json_response(
            {"kind": "Status", "code": 404, "reason": "NotFound"}, status=404, headers=_K8S_HEADERS
        )
    return web.json_response(_NAMESPACE, headers=_K8S_HEADERS)


async def _secret(request: web.Request) -> web.Response:
    return web.json_response(_SECRET, headers=_K8S_HEADERS)


async def _secret_list(request: web.Request) -> web.Response:
    return web.json_response(_SECRET_LIST, headers=_K8S_HEADERS)


@pytest.fixture
async def api_client() -> AsyncIterator[client.ApiClient]:
    app = web.Application()
    app.router.add_route("*", "/api/v1/namespaces/{name}", _namespace)
    app.router.add_route("*", "/api/v1/namespaces/{namespace}/secrets/{name}", _secret)
    app.router.add_get("/api/v1/namespaces/{namespace}/secrets", _secret_list)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    configuration = client.Configuration(host=f"http://127.0.0.1:{port}")
    configuration.api_key = {"BearerToken": "abc.def.ghi-secret-token-value"}
    configuration.api_key_prefix = {"BearerToken": "Bearer"}
    api = instrument_api_client(client.ApiClient(configuration=configuration))
    try:
        yield api
    finally:
        await api.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_k8s_typed_call_records_vendor_span(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    result = await client.CoreV1Api(api_client).patch_namespace(
        "demo",
        {"metadata": {"name": "demo"}},
        dry_run="All",
        field_manager="meho",
        _content_type="application/apply-patch+yaml",  # type: ignore[call-arg]
    )
    assert result.metadata.uid == "0b5c-uid"  # SDK result unchanged
    (span,) = scope.spans
    attrs = _attrs(span)
    assert span.span_kind == "vendor_call"
    assert span.status == "200"
    assert span.name.startswith("PATCH http://127.0.0.1:")
    assert span.name.endswith("/api/v1/namespaces/demo")
    assert attrs["method"] == "PATCH"
    assert attrs["query"] == "dryRun=All&fieldManager=meho"
    assert attrs["request_headers"]["content-type"] == "application/apply-patch+yaml"
    assert "authorization" not in attrs["request_headers"]
    assert attrs["response_headers"]["audit-id"] == "audit-123"
    assert attrs["response_headers"]["warning"] == '299 - "deprecated"'
    assert attrs["response_headers"]["server"] == "kube"
    assert attrs["response_object"] == {
        "kind": "Namespace",
        "apiVersion": "v1",
        "name": "demo",
        "uid": "0b5c-uid",
        "resourceVersion": "4242",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "generation": 3,
        "managedFields": [
            {
                "manager": "meho",
                "operation": "Apply",
                "apiVersion": "v1",
                "time": "2026-01-01T00:00:00Z",
            }
        ],
    }
    dumped = json.dumps(attrs)
    assert "secret-token-value" not in dumped
    assert "last-applied-configuration" not in json.dumps(attrs["response_object"])


@pytest.mark.asyncio
async def test_k8s_raw_response_is_recorded_and_still_readable(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    """``_preload_content=False`` (the ``DynamicClient`` path) still gets a full span."""
    response = await api_client.call_api(
        "/api/v1/namespaces/{name}",
        "GET",
        path_params={"name": "demo"},
        auth_settings=["BearerToken"],
        _preload_content=False,
    )
    assert (await response.json())["metadata"]["uid"] == "0b5c-uid"
    (span,) = scope.spans
    assert span.status == "200"
    assert _attrs(span)["response_object"]["resourceVersion"] == "4242"


def _assert_secret_masked(obj: dict[str, Any]) -> None:
    assert obj["data"]["tls.key"].startswith(REDACTED_PREFIX)
    assert obj["metadata"]["annotations"][LAST_APPLIED_ANNOTATION].startswith(REDACTED_PREFIX)


@pytest.mark.asyncio
async def test_k8s_apply_of_a_secret_records_no_secret_value(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    """``k8s.apply`` (server-side apply through the dynamic client) of a Secret."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "app", "namespace": "demo"},
        "stringData": {"config.ini": "plain-value-with-no-label"},
        "data": {"tls.key": _OPAQUE_B64},
    }
    response = await api_client.call_api(
        "/api/v1/namespaces/{namespace}/secrets/{name}",
        "PATCH",
        path_params={"namespace": "demo", "name": "app"},
        query_params=[("fieldManager", "meho"), ("force", "true")],
        header_params={"Content-Type": "application/apply-patch+yaml"},
        body=manifest,
        auth_settings=["BearerToken"],
        _preload_content=False,
    )
    assert (await response.json())["data"]["tls.key"] == _OPAQUE_B64  # SDK result unchanged
    (span,) = scope.spans
    attrs = _attrs(span)
    assert span.status == "200"
    assert attrs["op_id"] == "k8s.apply"
    assert attrs["query"] == "fieldManager=meho&force=true"
    assert attrs["request_body"]["data"]["tls.key"].startswith(REDACTED_PREFIX)
    assert attrs["request_body"]["stringData"]["config.ini"].startswith(REDACTED_PREFIX)
    assert attrs["request_body"]["metadata"]["name"] == "app"
    _assert_secret_masked(attrs["response_body"])
    assert attrs["response_body"]["type"] == "Opaque"
    assert attrs["response_object"]["uid"] == "sec-uid"
    dumped = json.dumps(attrs)
    assert _OPAQUE_B64 not in dumped
    assert "plain-value-with-no-label" not in dumped
    assert scope.redaction_uncertain is False


@pytest.mark.asyncio
async def test_k8s_cr_info_of_a_secret_records_no_secret_value(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    token = capture._op_context_var.set(_op("k8s.cr.info"))
    try:
        await api_client.call_api(
            "/api/v1/namespaces/{namespace}/secrets/{name}",
            "GET",
            path_params={"namespace": "demo", "name": "app"},
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
    finally:
        capture._op_context_var.reset(token)
    (span,) = scope.spans
    attrs = _attrs(span)
    assert attrs["op_id"] == "k8s.cr.info"
    _assert_secret_masked(attrs["response_body"])
    assert attrs["response_object"]["uid"] == "sec-uid"
    assert _OPAQUE_B64 not in json.dumps(attrs)
    assert scope.redaction_uncertain is False


@pytest.mark.asyncio
async def test_k8s_cr_list_of_secrets_records_no_secret_value(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    """The items of a ``SecretList`` carry no ``kind``; they are masked all the same."""
    token = capture._op_context_var.set(_op("k8s.cr.list"))
    try:
        await api_client.call_api(
            "/api/v1/namespaces/{namespace}/secrets",
            "GET",
            path_params={"namespace": "demo"},
            query_params=[("limit", 50), ("continue", "opaque-page-token")],
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
    finally:
        capture._op_context_var.reset(token)
    (span,) = scope.spans
    attrs = _attrs(span)
    assert attrs["op_id"] == "k8s.cr.list"
    assert attrs["query"] == f"limit=50&continue={QUERY_VALUE_MARKER}"
    (item,) = attrs["response_body"]["items"]
    _assert_secret_masked(item)
    assert attrs["response_object"] == {
        "kind": "SecretList",
        "apiVersion": "v1",
        "resourceVersion": "78",
        "item_count": 1,
    }
    dumped = json.dumps(attrs)
    assert _OPAQUE_B64 not in dumped
    assert "opaque-page-token" not in dumped
    assert scope.redaction_uncertain is False


@pytest.mark.asyncio
async def test_k8s_api_error_is_recorded_and_reraised(
    api_client: client.ApiClient, scope: capture._CaptureScope
) -> None:
    with pytest.raises(ApiException) as info:
        await client.CoreV1Api(api_client).read_namespace("missing")
    assert info.value.status == 404
    (span,) = scope.spans
    assert span.status == "404"
    assert _attrs(span)["response_object"] == {"kind": "Status"}
    assert _attrs(span)["response_headers"]["audit-id"] == "audit-123"


@pytest.mark.asyncio
async def test_k8s_transport_error_is_recorded_and_reraised(
    scope: capture._CaptureScope,
) -> None:
    configuration = client.Configuration(host="http://127.0.0.1:1")
    api = instrument_api_client(client.ApiClient(configuration=configuration))
    try:
        with pytest.raises(Exception) as info:
            await client.CoreV1Api(api).read_namespace("demo", _request_timeout=5)
    finally:
        await api.close()
    (span,) = scope.spans
    assert span.status == f"error:{type(info.value).__name__}"


@pytest.mark.asyncio
async def test_k8s_no_span_when_capture_off(api_client: client.ApiClient) -> None:
    result = await client.CoreV1Api(api_client).read_namespace("demo")
    assert result.metadata.name == "demo"


@pytest.mark.asyncio
async def test_instrument_api_client_is_idempotent() -> None:
    api = client.ApiClient(configuration=client.Configuration(host="http://127.0.0.1:1"))
    try:
        instrument_api_client(api)
        wrapped = api.rest_client.request
        instrument_api_client(api)
        assert api.rest_client.request is wrapped
    finally:
        await api.close()
