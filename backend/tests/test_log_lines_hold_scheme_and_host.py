"""MEHO's own log lines hold the scheme and host of an address, never more.

Each test builds made-up address parts at run time, runs the real code path
that writes the log line, and checks the captured event.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from structlog.testing import capture_logs

from meho_backplane.connectors._shared.log_host import (
    UNPARSEABLE_ADDRESS,
    origin_for_log,
    spec_source_for_log,
)
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
)


def _parts() -> dict[str, str]:
    tag = uuid.uuid4().hex
    return {
        "user": f"user{tag[:8]}",
        "password": f"pw{tag[8:16]}",
        "path": f"/path{tag[16:24]}/more",
        "query": f"key=q{tag[24:]}",
        "fragment": f"frag{tag[:6]}",
    }


def _address(host: str, parts: dict[str, str], scheme: str = "https") -> str:
    return (
        f"{scheme}://{parts['user']}:{parts['password']}@{host}"
        f"{parts['path']}?{parts['query']}#{parts['fragment']}"
    )


def _assert_no_parts(captured: list[dict[str, Any]], parts: dict[str, str]) -> None:
    text = json.dumps(captured, default=str)
    for value in parts.values():
        assert value not in text


def test_origin_for_log_holds_scheme_and_host_only() -> None:
    parts = _parts()
    assert origin_for_log(_address("vendor.example.test", parts)) == "https://vendor.example.test"
    assert origin_for_log("http://[::1]:8080/x?y=z") == "http://[::1]"


@pytest.mark.parametrize(
    "value", ["/relative/path", "vendor.example.test/path", "", "http://\\evil"]
)
def test_origin_for_log_gives_one_fixed_mark_without_a_host(value: str) -> None:
    assert origin_for_log(value) == UNPARSEABLE_ADDRESS


def test_spec_source_for_log_names_a_kind_for_a_file() -> None:
    parts = _parts()
    assert spec_source_for_log(_address("spec.example.test", parts)) == "https://spec.example.test"
    assert spec_source_for_log(f"/srv{parts['path']}/spec.yaml") == "file"
    assert spec_source_for_log(f"file://{parts['path']}") == "file"


@pytest.mark.asyncio
async def test_gcloud_token_refresh_lines_hold_the_host_only() -> None:
    parts = _parts()
    connector = GcloudConnector()
    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401 if calls["n"] % 2 else 200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    async def _token(*_a: Any, **_k: Any) -> str:
        return "t"

    async def _client(*_a: Any, **_k: Any) -> httpx.AsyncClient:
        return client

    connector._ensure_token = _token  # type: ignore[method-assign]
    connector.refresh_token = _token  # type: ignore[method-assign]
    connector._http_client = _client  # type: ignore[method-assign]
    target = type("T", (), {"name": "gcp-a"})()
    url = f"https://api.example.test{parts['path']}"
    with capture_logs() as captured:
        await connector._get_json_abs(target, url, operator=None, params={"k": parts["query"]})  # type: ignore[arg-type]
        await connector._post_json_abs(target, url, operator=None, json_body={})  # type: ignore[arg-type,attr-defined]
    events = {e["event"]: e for e in captured}
    assert events["gcloud_401_token_refresh"]["url"] == "api.example.test"
    assert events["gcloud_401_token_refresh_post"]["url"] == "api.example.test"
    _assert_no_parts(captured, parts)


def test_connector_auto_registered_line_holds_scheme_and_host_only() -> None:
    parts = _parts()
    clear_registry()
    try:
        with capture_logs() as captured:
            assert ensure_connector_class_registered(
                product="logprobe",
                version="1.0",
                impl_id="logprobe-rest",
                base_url=_address("api.example.test", parts),
            )
    finally:
        clear_registry()
    (event,) = [e for e in captured if e["event"] == "connector_auto_registered"]
    assert event["base_url"] == "https://api.example.test"
    _assert_no_parts(captured, parts)


def test_ingest_version_lines_hold_the_spec_source_only() -> None:
    import structlog

    from meho_backplane.operations.ingest.api_schemas import SpecSource
    from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

    parts = _parts()

    def _spec(version: str | None) -> str:
        info = f"info:\n  title: t\n  version: {version}\n" if version else "info:\n  title: t\n"
        return f"openapi: 3.0.0\n{info}paths: {{}}\n"

    web = _address("spec.example.test", parts)
    local = f"/srv{parts['path']}/spec.yaml"
    service = IngestionPipelineService.__new__(IngestionPipelineService)
    runs: list[tuple[str, str | None, tuple[str, ...] | None, str, str]] = [
        (web, None, None, "connector_ingest_version_check_skipped", "https://spec.example.test"),
        (local, "1.3.0", None, "connector_ingest_version_drift", "file"),
        (
            web,
            "1.2.0",
            ("1.x",),
            "connector_ingest_version_label_decoupled",
            "https://spec.example.test",
        ),
    ]
    with capture_logs() as captured:
        for uri, version, compatible, _event, _source in runs:
            service._validate_spec_versions(
                specs=[SpecSource(uri=uri, content=_spec(version))],
                requested_version="1.2.0" if compatible else "1.0.0",
                log=structlog.get_logger(),
                spec_info_versions_compatible=compatible,
            )
    assert [(e["event"], e["spec_source"]) for e in captured] == [
        (event, source) for _u, _v, _c, event, source in runs
    ]
    _assert_no_parts(captured, parts)


@pytest.mark.asyncio
async def test_runner_started_line_holds_scheme_and_host_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.runner import loop
    from meho_backplane.runner.settings import RunnerSettings

    parts = _parts()
    settings = RunnerSettings(
        central_url=_address("central.example.test", parts),
        runner_id="r1",
        key_dir="/tmp/k",
        tick_interval_seconds=1.0,
        spool_dir="/tmp/s",
        spool_max_files=1,
    )

    class _StopError(Exception):
        pass

    async def _stop(_settings: Any) -> None:
        raise _StopError

    monkeypatch.setattr(loop, "open_signed_jwt_login", _stop)
    with capture_logs() as captured, pytest.raises(_StopError):
        await loop._run_loop(settings)
    (event,) = [e for e in captured if e["event"] == "runner_started"]
    assert event["central_url"] == "https://central.example.test"
    _assert_no_parts(captured, parts)


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_mcp_malformed_session_header_is_not_written() -> None:
    from meho_backplane.mcp.server import _bind_mcp_session_id

    made_up = f"sess-{uuid.uuid4().hex}"
    with capture_logs() as captured:
        _bind_mcp_session_id(_request({"mcp-session-id": made_up}), {})
    (event,) = [e for e in captured if e["event"] == "mcp_malformed_session_id"]
    assert event["header"] == "malformed"
    assert made_up not in json.dumps(captured, default=str)


def test_mcp_protocol_version_header_is_written_only_in_the_date_form() -> None:
    from meho_backplane.mcp.server import _validate_protocol_version_header

    made_up = f"ver-{uuid.uuid4().hex}"
    with capture_logs() as captured:
        _validate_protocol_version_header(
            _request({"mcp-protocol-version": made_up}), "tools/list", {}
        )
        _validate_protocol_version_header(
            _request({"mcp-protocol-version": "2024-11-05"}), "tools/list", {}
        )
    events = [e for e in captured if e["event"] == "mcp_unsupported_protocol_version"]
    assert [e["header"] for e in events] == ["malformed", "2024-11-05"]
    assert made_up not in json.dumps(captured, default=str)


# ---------------------------------------------------------------------------
# Guard: a log call never passes a raw address or header value
# ---------------------------------------------------------------------------

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
_ADDRESS_WORDS = ("url", "uri", "location", "href", "endpoint")
_SAFE_CALLS = {"host_for_log", "origin_for_log", "spec_source_for_log"}
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

# (file, event) pairs whose value is MEHO's own address or route, or is already cut.
_ALLOWED = {
    ("api/v1/conventions.py", "mcp_resource_updated_emit"),
    ("mcp/handlers.py", "mcp_resource_read_forbidden"),
    ("mcp/handlers.py", "mcp_resource_read_capability_forbidden"),
    ("mcp/handlers.py", "mcp_audit_write_failed"),
    ("mcp/server.py", "mcp_profile_token_url_mismatch"),
    # The value is already a host name or the fixed mark when it reaches the call.
    ("connectors/adapters/http.py", "connector_redirect_not_followed_cross_origin"),
    # MEHO's own resource address template.
    ("mcp/registry.py", "mcp_resource_registered"),
    # The value is cut to the date form or the fixed mark inside the call.
    ("mcp/server.py", "mcp_unsupported_protocol_version"),
}


def _is_logger(node: ast.expr) -> bool:
    name = ""
    while isinstance(node, ast.Attribute | ast.Call):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    if isinstance(node, ast.Name):
        name = node.id
    return "log" in name.lower()


def _safe_value(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.IfExp):
        return _safe_value(value.body) and _safe_value(value.orelse)
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _SAFE_CALLS
    )


def _bad_log_calls() -> list[str]:
    bad: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and _is_logger(node.func.value)
            ):
                continue
            event = ""
            if node.args and isinstance(node.args[0], ast.Constant):
                event = str(node.args[0].value)
            for kw in node.keywords:
                if kw.arg is None or _safe_value(kw.value):
                    continue
                key = kw.arg.lower()
                if kw.arg in {"header", "cookie"} or any(w in key for w in _ADDRESS_WORDS):
                    if (rel, event) in _ALLOWED:
                        continue
                    if key.endswith(("_name", "_names", "_count")) or "kind" in key:
                        continue
                    bad.append(f"{rel}:{node.lineno} {event} {kw.arg}")
    return bad


def test_no_log_call_passes_a_raw_address() -> None:
    assert _bad_log_calls() == []
