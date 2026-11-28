"""MEHO's own log lines hold the scheme and host of an address, never more.

Each run test builds made-up address parts at run time, runs the real code
path with the production log setup at each root level, and reads the JSON
lines MEHO writes on stdout.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.log_host import (
    UNPARSEABLE_ADDRESS,
    origin_for_log,
    spec_source_for_log,
)
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.logging import configure_logging
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
)
from meho_backplane.settings import get_settings
from tests._production_log_lines import _drop_cached_loggers, json_lines


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


@pytest.fixture(
    params=[logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING],
    ids=["notset", "debug", "info", "warning"],
)
def log_level(request: pytest.FixtureRequest) -> Iterator[int]:
    """Log with the production setup at the given root level; put the old setup back after."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging(request.param)
    structlog.configure(cache_logger_on_first_use=False)
    _drop_cached_loggers()
    try:
        yield request.param
    finally:
        structlog.reset_defaults()
        _drop_cached_loggers()
        root.handlers[:] = handlers
        root.setLevel(level)


def _read_lines(
    capfd: pytest.CaptureFixture[str], made_up: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Check that no made-up part is in stdout; return its JSON lines by event."""
    out = capfd.readouterr().out
    for value in made_up:
        assert value not in out
    events: dict[str, list[dict[str, Any]]] = {}
    for line in json_lines(out):
        events.setdefault(line["event"], []).append(line)
    return events


def _expect(
    events: dict[str, list[dict[str, Any]]],
    event: str,
    field: str,
    values: list[str],
    *,
    root_level: int,
    event_level: int = logging.INFO,
) -> None:
    """Check the field of each line of ``event``, or that the line is not written."""
    lines = events.get(event, [])
    if root_level <= event_level:
        assert [line[field] for line in lines] == values
    else:
        assert lines == []


@pytest.mark.asyncio
async def test_gcloud_token_refresh_lines_hold_the_host_only(
    log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
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
    url = _address("api.example.test", parts)
    capfd.readouterr()
    await connector._get_json_abs(target, url, operator=None, params={})  # type: ignore[arg-type]
    await connector._post_json_abs(target, url, operator=None, json_body={})  # type: ignore[arg-type,attr-defined]
    events = _read_lines(capfd, list(parts.values()))
    for event in ("gcloud_401_token_refresh", "gcloud_401_token_refresh_post"):
        _expect(events, event, "url", ["api.example.test"], root_level=log_level)


def test_connector_auto_registered_line_holds_scheme_and_host_only(
    log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
    parts = _parts()
    clear_registry()
    capfd.readouterr()
    try:
        assert ensure_connector_class_registered(
            product="logprobe",
            version="1.0",
            impl_id="logprobe-rest",
            base_url=_address("api.example.test", parts),
        )
    finally:
        clear_registry()
    events = _read_lines(capfd, list(parts.values()))
    _expect(
        events,
        "connector_auto_registered",
        "base_url",
        ["https://api.example.test"],
        root_level=log_level,
    )


def test_ingest_version_lines_hold_the_spec_source_only(
    log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
    from meho_backplane.operations.ingest.api_schemas import SpecSource
    from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

    parts = _parts()

    def _spec(version: str | None) -> str:
        info = f"info:\n  title: t\n  version: {version}\n" if version else "info:\n  title: t\n"
        return f"openapi: 3.0.0\n{info}paths: {{}}\n"

    web = _address("spec.example.test", parts)
    local = f"/srv{parts['path']}/spec.yaml"
    service = IngestionPipelineService.__new__(IngestionPipelineService)
    runs: list[tuple[str, str | None, tuple[str, ...] | None, str, str, int]] = [
        (
            web,
            None,
            None,
            "connector_ingest_version_check_skipped",
            "https://spec.example.test",
            logging.INFO,
        ),
        (local, "1.3.0", None, "connector_ingest_version_drift", "file", logging.WARNING),
        (
            web,
            "1.2.0",
            ("1.x",),
            "connector_ingest_version_label_decoupled",
            "https://spec.example.test",
            logging.INFO,
        ),
    ]
    capfd.readouterr()
    for uri, version, compatible, _event, _source, _level in runs:
        service._validate_spec_versions(
            specs=[SpecSource(uri=uri, content=_spec(version))],
            requested_version="1.2.0" if compatible else "1.0.0",
            log=structlog.get_logger(),
            spec_info_versions_compatible=compatible,
        )
    events = _read_lines(capfd, list(parts.values()))
    for _uri, _version, _compatible, event, source, level in runs:
        _expect(events, event, "spec_source", [source], root_level=log_level, event_level=level)


class _StopAfterLogError(Exception):
    pass


@pytest.mark.asyncio
async def test_ingest_op_allowlist_dropped_line_holds_scheme_and_host_only(
    monkeypatch: pytest.MonkeyPatch, log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
    from meho_backplane.operations.ingest import register_ingested
    from meho_backplane.operations.ingest.op_allowlist import DroppedOp, OpAllowlistResult

    def _drop_one(**_: object) -> OpAllowlistResult:
        return OpAllowlistResult(kept=(), dropped=(DroppedOp(method="GET", path="/dropped"),))

    def _stop(*_: object, **__: object) -> None:
        raise _StopAfterLogError

    monkeypatch.setattr(register_ingested, "apply_op_allowlist", _drop_one)
    monkeypatch.setattr(register_ingested, "_prepare_ingested_operations", _stop)
    parts = _parts()
    capfd.readouterr()
    with pytest.raises(_StopAfterLogError):
        await register_ingested.register_ingested_operations(
            product="acme",
            version="1.0",
            impl_id="acme-rest-1.0",
            spec_source=_address("spec.example.test", parts),
            operations=[],
        )
    _expect(
        _read_lines(capfd, list(parts.values())),
        "ingest_op_allowlist_dropped",
        "spec_source",
        ["https://spec.example.test"],
        root_level=log_level,
    )


@pytest.mark.asyncio
async def test_runner_started_line_holds_scheme_and_host_only(
    monkeypatch: pytest.MonkeyPatch, log_level: int, capfd: pytest.CaptureFixture[str]
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
    capfd.readouterr()
    with pytest.raises(_StopError):
        await loop._run_loop(settings)
    events = _read_lines(capfd, list(parts.values()))
    _expect(
        events,
        "runner_started",
        "central_url",
        ["https://central.example.test"],
        root_level=log_level,
    )


@pytest.fixture
def mcp_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client of the MCP endpoint, as ``tests/test_mcp_server.py`` builds it."""
    from meho_backplane.main import app
    from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind

    async def _operator() -> Operator:
        return Operator(
            sub="log-test-operator",
            name="Log Test",
            email=None,
            raw_jwt="fixture-jwt-not-real",
            tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
            tenant_role=TenantRole.OPERATOR,
        )

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    app.dependency_overrides[verify_mcp_jwt_and_bind] = _operator
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        get_settings.cache_clear()


def _ping(client: TestClient, headers: dict[str, str]) -> None:
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)


def test_mcp_malformed_session_header_is_not_written(
    mcp_client: TestClient, log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
    made_up = f"sess-{uuid.uuid4().hex}"
    capfd.readouterr()
    _ping(mcp_client, {"mcp-session-id": made_up, "mcp-protocol-version": PROTOCOL_VERSION})
    events = _read_lines(capfd, [made_up])
    _expect(
        events,
        "mcp_malformed_session_id",
        "header",
        ["malformed"],
        root_level=log_level,
        event_level=logging.WARNING,
    )


def test_mcp_protocol_version_header_is_written_only_in_the_date_form(
    mcp_client: TestClient, log_level: int, capfd: pytest.CaptureFixture[str]
) -> None:
    made_up = f"ver-{uuid.uuid4().hex}"
    capfd.readouterr()
    _ping(mcp_client, {"mcp-protocol-version": made_up})
    _ping(mcp_client, {"mcp-protocol-version": "2024-11-05"})
    events = _read_lines(capfd, [made_up])
    _expect(
        events,
        "mcp_unsupported_protocol_version",
        "header",
        ["malformed", "2024-11-05"],
        root_level=log_level,
        event_level=logging.WARNING,
    )


# ---------------------------------------------------------------------------
# Guard: a log call never passes a raw address or header value
# ---------------------------------------------------------------------------

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
_ADDRESS_WORDS = ("url", "uri", "location", "href", "endpoint")
_ADDRESS_NAMES = {"header", "cookie", "spec_source"}
_SAFE_CALLS = {
    "host_for_log",
    "origin_for_log",
    "spec_source_for_log",
    "_protocol_header_for_log",
}
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

# (file, event, keyword) of a log field whose value is not a raw address of a caller or vendor.
_ALLOWED = {
    # MEHO's own resource address.
    ("api/v1/conventions.py", "mcp_resource_updated_emit", "uri"),
    # The resource address of a refused MCP read.
    ("mcp/handlers.py", "mcp_resource_read_forbidden", "uri"),
    ("mcp/handlers.py", "mcp_resource_read_capability_forbidden", "uri"),
    # The name of an MCP profile, not an address.
    ("mcp/server.py", "mcp_profile_token_url_mismatch", "url_profile"),
    # The caller cuts the value to a host name or the fixed mark.
    ("connectors/adapters/http.py", "connector_redirect_not_followed_cross_origin", "location"),
    # MEHO's own resource address template.
    ("mcp/registry.py", "mcp_resource_registered", "uri_template"),
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
                if key in _ADDRESS_NAMES or any(w in key for w in _ADDRESS_WORDS):
                    if (rel, event, kw.arg) in _ALLOWED:
                        continue
                    if key.endswith(("_name", "_names", "_count")) or "kind" in key:
                        continue
                    bad.append(f"{rel}:{node.lineno} {event} {kw.arg}")
    return bad


def test_no_log_call_passes_a_raw_address() -> None:
    assert _bad_log_calls() == []
