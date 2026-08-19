# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the observability surface.

Covers Task #20 acceptance criteria:

* ``/metrics`` returns Prometheus exposition with the locked content
  type and includes both default process metrics and the application
  ``http_requests_total`` counter.
* structlog produces valid JSON to stdout for every log line.
* The request-context middleware propagates ``request_id`` from the
  incoming header to structlog contextvars to the response header to
  the structured log.
* Sensitive request headers (``Authorization``, ``Cookie``,
  ``X-API-Key``) never leak into logs.
* Standard-library ``logging`` records (uvicorn / third-party libs)
  are bridged into the same JSON pipeline with ``request_id``
  correlation and locals-stripped tracebacks (#2887).

The tests redirect structlog's logger factory to a per-test
:class:`io.StringIO` buffer rather than touching ``sys.stdout`` —
``capsys`` would also work, but the ``cache_logger_on_first_use=True``
setting in production means the first call to
:func:`structlog.get_logger` pins the file handle for the process
lifetime; rebinding the factory inside the test body is the cleaner
seam.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import platform
import re
import sys
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from meho_backplane.main import app
from meho_backplane.middleware import UNMATCHED_ROUTE_LABEL, RequestContextMiddleware

_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _configure_capture(buf: io.StringIO) -> None:
    """Configure structlog to write JSON lines to ``buf``.

    Mirrors :func:`meho_backplane.logging.configure_logging` but with
    the logger factory pointed at the in-memory buffer. Tests must
    call this in the ``client`` fixture *and* before any
    :func:`structlog.get_logger` call to bypass the production
    ``cache_logger_on_first_use=True`` cache.
    """
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    """Per-test log capture buffer."""
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """TestClient over the production app, with logs captured in ``log_buffer``.

    The ``log_buffer`` fixture is injected (even though only used
    transitively via the structlog factory) to guarantee
    ``_configure_capture`` runs before the TestClient drives a request.

    Using a context manager exits the FastAPI ``lifespan``, which would
    *re-run* the production :func:`configure_logging` and clobber the
    capture. Driving requests against the bare ``app`` object via
    :class:`fastapi.testclient.TestClient` without the ``with`` block
    skips the lifespan — acceptable here because the only lifespan
    side effect is logging configuration, which the fixture has
    already taken over.
    """
    yield TestClient(app)


def _read_log_lines(buf: io.StringIO) -> list[dict[str, object]]:
    """Parse each non-empty line in ``buf`` as JSON."""
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_prometheus_text_format(
    client: TestClient,
) -> None:
    """``/metrics`` returns the legacy 0.0.4 Prometheus text format."""
    # First drive a request through ``/`` so the counter has at least
    # one labelled sample to expose.
    client.get("/")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = response.text

    # The application counter, with all three labels populated.
    assert 'http_requests_total{method="GET"' in body
    assert 'path="/"' in body
    assert 'status="200"' in body


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason=(
        "prometheus_client.ProcessCollector reads /proc/<pid>/; "
        "not available on non-Linux platforms"
    ),
)
def test_metrics_endpoint_exposes_process_metrics_on_linux(
    client: TestClient,
) -> None:
    """Default process-collector metrics — the runtime fingerprint Goal #11
    promised operators. ``prometheus_client.ProcessCollector`` derives these
    from ``/proc/<pid>/`` files and emits no samples on non-Linux platforms,
    so the assertion is scoped to Linux (which is the CI runner pool's OS).

    Repeats the basic ``/metrics`` response asserts from the portable test
    so a broken endpoint on Linux surfaces as a clear status / content-type
    failure rather than a confusing missing-substring error.
    """
    client.get("/")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = response.text

    assert "process_resident_memory_bytes" in body
    assert "process_open_fds" in body


def test_metrics_endpoint_does_not_increment_for_itself_during_render(
    client: TestClient,
) -> None:
    """A ``/metrics`` request increments exactly once, after the response.

    The middleware increments :data:`HTTP_REQUESTS_TOTAL` after the
    handler returns, so a single ``/metrics`` request must move the
    counter for ``path="/metrics"`` forward by exactly 1.0 — never 2.0
    (which would mean the renderer itself inflated the count) and
    never 0.0 (which would mean the increment never landed). The two
    sequential requests pin both sides: ``mid - before`` proves
    response_one's request applied exactly one increment, and
    ``after - mid`` proves response_two did the same independently.

    The naive ``"http_requests_total" in response_two.text`` substring
    check is too weak — the HELP/TYPE preamble alone satisfies it
    regardless of which samples are actually present, so it cannot
    distinguish a working counter from a silently-broken one.
    """
    label_set = {"method": "GET", "path": "/metrics", "status": "200"}

    before = REGISTRY.get_sample_value("http_requests_total", labels=label_set) or 0.0

    response_one = client.get("/metrics")
    mid = REGISTRY.get_sample_value("http_requests_total", labels=label_set) or 0.0

    response_two = client.get("/metrics")
    after = REGISTRY.get_sample_value("http_requests_total", labels=label_set) or 0.0

    assert response_one.status_code == 200
    assert response_two.status_code == 200

    # Each /metrics request increments its own labelled sample by
    # exactly 1.0 — no double-counting during render.
    assert mid - before == pytest.approx(1.0)
    assert after - mid == pytest.approx(1.0)
    # Response_two's body must expose the sample the previous request
    # registered (proves the renderer reflects the post-increment
    # registry state, not stale memory).
    assert 'http_requests_total{method="GET",path="/metrics",status="200"}' in response_two.text


# ---------------------------------------------------------------------------
# structlog JSON shape
# ---------------------------------------------------------------------------


def test_logs_are_valid_json_lines(client: TestClient, log_buffer: io.StringIO) -> None:
    """Every emitted log record is a single JSON object per line."""
    client.get("/")
    client.get("/")

    lines = _read_log_lines(log_buffer)

    assert len(lines) >= 2
    for entry in lines:
        assert "timestamp" in entry
        assert "level" in entry
        assert "event" in entry


def test_handler_exception_emits_structured_traceback(
    log_buffer: io.StringIO,
) -> None:
    """``log.exception`` in the middleware serialises the traceback.

    Regression guard for the missing ``dict_tracebacks`` processor:
    without it, the ``request_failed`` log line carries the literal
    ``"exc_info": true`` and zero traceback content, which strips
    production triage of the only signal that maps a 5xx back to a
    line of source. The middleware's ``except Exception: log.exception(...)``
    block is the load-bearing surface here, so the test drives a
    handler that raises and asserts the captured log line carries a
    non-empty structured ``exception`` payload.
    """
    from fastapi import FastAPI

    boom = FastAPI()
    boom.add_middleware(RequestContextMiddleware)

    @boom.get("/boom")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("synthetic-handler-failure")

    boom_client = TestClient(boom, raise_server_exceptions=False)
    response = boom_client.get("/boom")
    assert response.status_code == 500

    failed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_failed"
    ]
    assert failed, "expected a request_failed log line for the raising handler"

    entry = failed[-1]
    # dict_tracebacks emits a list of {exc_type, exc_value, frames, ...}
    # dicts. The literal "exc_info": true bug shape must never reappear.
    assert entry.get("exc_info") is not True, (
        "dict_tracebacks regression: log line carries the unrendered exc_info=true literal "
        "instead of a structured traceback"
    )
    exception_payload = entry.get("exception")
    assert isinstance(exception_payload, list) and exception_payload, (
        "expected non-empty structured exception list from dict_tracebacks"
    )
    head = exception_payload[0]
    assert isinstance(head, dict)
    assert head.get("exc_type") == "RuntimeError"
    assert head.get("exc_value") == "synthetic-handler-failure"
    frames = head.get("frames")
    assert isinstance(frames, list) and frames, "expected at least one traceback frame"


def test_request_completed_log_shape(client: TestClient, log_buffer: io.StringIO) -> None:
    """``request_completed`` carries method / path / status / duration_ms."""
    response = client.get("/")
    assert response.status_code == 200

    completed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_completed"
    ]
    assert len(completed) == 1

    entry = completed[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/"
    assert entry["status"] == 200
    assert isinstance(entry["duration_ms"], int | float)
    assert entry["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Request id propagation
# ---------------------------------------------------------------------------


def test_request_id_propagates_from_incoming_header(
    client: TestClient, log_buffer: io.StringIO
) -> None:
    """A client-supplied ``X-Request-Id`` is preserved end-to-end."""
    incoming = "client-correlation-42"
    response = client.get("/", headers={"X-Request-Id": incoming})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == incoming

    completed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_completed"
    ]
    assert completed and completed[-1]["request_id"] == incoming


def test_request_id_generated_when_header_absent(
    client: TestClient, log_buffer: io.StringIO
) -> None:
    """Without an incoming header, a UUID4 hex is minted and echoed back."""
    response = client.get("/")

    generated = response.headers["x-request-id"]
    assert _UUID_HEX_RE.match(generated), f"not a UUID4 hex: {generated!r}"

    # ``UUID(hex=...)`` rejects malformed values; use it as a tighter
    # parser than the regex alone.
    UUID(hex=generated)

    completed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_completed"
    ]
    assert completed and completed[-1]["request_id"] == generated


def test_request_id_visible_to_handlers_via_contextvars(
    log_buffer: io.StringIO,
) -> None:
    """Handler-side ``structlog.get_logger().info(...)`` carries ``request_id``.

    This is the load-bearing invariant — every downstream Initiative
    relies on handlers logging without threading ``request_id``
    through every call.
    """
    from fastapi import FastAPI

    probe = FastAPI()
    probe.add_middleware(RequestContextMiddleware)

    @probe.get("/probe")
    async def probe_handler() -> dict[str, str]:
        structlog.get_logger().info("handler_log")
        return {"ok": "yes"}

    probe_client = TestClient(probe)
    response = probe_client.get("/probe", headers={"X-Request-Id": "handler-trace"})
    assert response.status_code == 200

    handler_logs = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "handler_log"
    ]
    assert handler_logs
    assert handler_logs[0]["request_id"] == "handler-trace"


# ---------------------------------------------------------------------------
# Sensitive-header redaction
# ---------------------------------------------------------------------------


def test_sensitive_headers_never_leak_into_logs(
    client: TestClient, log_buffer: io.StringIO
) -> None:
    """``Authorization`` / ``Cookie`` / ``X-API-Key`` values stay out of logs."""
    secrets = {
        "Authorization": "Bearer SECRET-BEARER-TOKEN-XYZ",
        "Cookie": "session=COOKIE-VAL-ABC",
        "X-API-Key": "APIKEY-VAL-123",
    }

    response = client.get("/", headers=secrets)
    assert response.status_code == 200

    captured = log_buffer.getvalue()
    assert captured, "expected at least one log line"

    for marker in ("SECRET-BEARER-TOKEN-XYZ", "COOKIE-VAL-ABC", "APIKEY-VAL-123"):
        assert marker not in captured, (
            f"sensitive header value {marker!r} leaked into logs:\n{captured}"
        )


# ---------------------------------------------------------------------------
# http_requests_total counter
# ---------------------------------------------------------------------------


def test_http_requests_total_increments_per_request(client: TestClient) -> None:
    """Two requests to ``/`` move the counter forward by two."""
    before = (
        REGISTRY.get_sample_value(
            "http_requests_total", labels={"method": "GET", "path": "/", "status": "200"}
        )
        or 0.0
    )

    client.get("/")
    client.get("/")

    after = REGISTRY.get_sample_value(
        "http_requests_total", labels={"method": "GET", "path": "/", "status": "200"}
    )
    assert after is not None
    assert after - before == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# stdlib logging bridge (#2887)
# ---------------------------------------------------------------------------


def _reset_logging_tree() -> None:
    """Undo :func:`configure_logging`'s global mutations after a test.

    ``configure_logging`` installs a root-logger handler and re-points
    the uvicorn loggers; without an explicit reset those escape into
    later tests. Only the bridge's own named handler is removed, so
    pytest's ``caplog`` handler is left untouched.
    """
    from meho_backplane.logging import _STDLIB_BRIDGE_HANDLER_NAME

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.name == _STDLIB_BRIDGE_HANDLER_NAME]:
        root.removeHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def _bridged_stdlib_logger(name: str) -> logging.Logger:
    """Return a stdlib logger guaranteed enabled for the bridge test.

    Heavy libraries imported during the test session call
    ``logging.config.dictConfig(disable_existing_loggers=True)``, which
    flips ``disabled=True`` on unrelated pre-existing loggers (``httpx``,
    ``sqlalchemy.engine``, ...). Production is unaffected — uvicorn's own
    dictConfig uses ``disable_existing_loggers=False`` and the backplane
    calls no dictConfig — so re-enabling here keeps the test deterministic
    without masking any real bridge behaviour.
    """
    logger = logging.getLogger(name)
    logger.disabled = False
    return logger


@contextlib.contextmanager
def _stdlib_bridge_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Run the *production* ``configure_logging`` with stdout captured.

    Unlike ``log_buffer`` (which repoints only structlog's own factory),
    this exercises the real
    :func:`meho_backplane.logging.configure_logging` end to end —
    including the stdlib-logging bridge it installs on the root logger —
    with ``sys.stdout`` swapped for an in-memory buffer. The bridge
    handler resolves ``sys.stdout`` per emit, so the swap is seen.

    Deliberately a context manager called *inside* the test body rather
    than a fixture: pytest's capture manager re-installs its own stdout
    at every setup/call/teardown boundary, so a ``sys.stdout`` swap done
    in fixture setup is stranded before the test emits. Swapping in the
    same phase as the log call keeps the buffer live.
    """
    from meho_backplane.logging import configure_logging

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    configure_logging(level=logging.INFO)
    try:
        yield buf
    finally:
        _reset_logging_tree()


def test_stdlib_logger_warning_carries_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stdlib ``logging`` warning inside a request context emits one
    JSON line with level, timestamp, and the request's ``request_id``.

    This is Initiative #2884's stated success criterion for the bridge:
    third-party libraries (uvicorn / httpx / SQLAlchemy / ...) log
    through :mod:`logging`, and those records must land as the same
    correlated JSON as structlog-native lines.
    """
    with _stdlib_bridge_capture(monkeypatch) as buf:
        # The middleware binds ``request_id`` on request entry; emulate
        # that bound context, then log through *stdlib* logging from
        # outside the meho namespace with lazy %-args (as a well-behaved
        # library does).
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-2887-abc")
        _bridged_stdlib_logger("httpx").warning("connection retry %d", 3)
        lines = _read_log_lines(buf)

    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    record = lines[0]
    assert record["level"] == "warning"
    assert record["event"] == "connection retry 3"
    assert record["request_id"] == "req-2887-abc"
    # ISO 8601 UTC timestamp (structlog's ``TimeStamper(utc=True)``).
    assert isinstance(record["timestamp"], str) and record["timestamp"].endswith("Z")


def test_stdlib_exception_strips_frame_locals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bridged ``logging.exception`` renders a structured, locals-stripped
    traceback — CWE-532 protection extends to stdlib records too.

    Mirrors the structlog-native guarantee proven in
    ``test_secret_leak_checks``: a secret held only as a frame local on
    the failing traceback must not reach the log line.
    """
    secret_canary = "STDLIB-FRAME-LOCAL-SECRET-2887"

    def _raise_holding_secret() -> None:
        agent_client_secret = secret_canary  # noqa: F841 — the frame local under test
        raise RuntimeError("stdlib boom")

    with _stdlib_bridge_capture(monkeypatch) as buf:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-2887-exc")
        try:
            _raise_holding_secret()
        except RuntimeError:
            _bridged_stdlib_logger("sqlalchemy.engine").exception("query_failed")
        captured = buf.getvalue()

    assert "query_failed" in captured, "expected the stdlib exception to be logged"
    assert secret_canary not in captured, (
        f"frame-local secret leaked into the bridged stdlib exception log:\n{captured}"
    )
    record = json.loads(captured.splitlines()[-1])
    assert record["request_id"] == "req-2887-exc"
    # Structured frames, not the ``"exc_info": true`` literal or a
    # plain-text traceback appended by the base logging.Formatter.
    assert isinstance(record["exception"], list) and record["exception"]


def test_uvicorn_access_dropped_error_bridged(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure_logging overrides uvicorn's own log config.

    uvicorn applies its dictConfig at server startup (before the FastAPI
    lifespan runs ``configure_logging``), pinning handlers on its loggers
    with ``propagate=False``. The bridge must win: access logs are
    dropped (``request_completed`` already covers per-request lines) and
    error/startup logs route into the JSON root handler.
    """
    # Simulate uvicorn's server-startup dictConfig state (its own
    # handlers + propagate=False). ``disabled=False`` guards against a
    # heavy test-session import having disabled these loggers, so the
    # emits below stay non-vacuous.
    seed = logging.NullHandler()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [seed]
        lg.propagate = False
        lg.disabled = False

    with _stdlib_bridge_capture(monkeypatch) as buf:
        access = logging.getLogger("uvicorn.access")
        assert access.propagate is False
        assert access.handlers == []  # dropped, not bridged

        for name in ("uvicorn", "uvicorn.error"):
            lg = logging.getLogger(name)
            assert lg.propagate is True  # routed to the JSON root handler
            assert lg.handlers == []

        # End to end: a runtime uvicorn.error line lands as JSON; an
        # access line does not surface at all.
        logging.getLogger("uvicorn.error").warning("bind_failed")
        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET / HTTP/1.1" 200')
        events = [str(record.get("event", "")) for record in _read_log_lines(buf)]

    assert "bind_failed" in events
    assert all("GET /" not in event for event in events)


# ---------------------------------------------------------------------------
# http_request_duration_seconds histogram
# ---------------------------------------------------------------------------


def test_http_request_duration_histogram_observes_per_request(client: TestClient) -> None:
    """Each request adds exactly one observation to the duration histogram.

    The histogram is observed at the same middleware seam as
    :data:`HTTP_REQUESTS_TOTAL` (#2886), so two requests to ``/`` move
    the ``_count`` sample for the matched-template label set forward by
    exactly two — proving the observe() call rides the same code path as
    the counter increment.
    """
    labels = {"method": "GET", "path": "/", "status": "200"}
    before = REGISTRY.get_sample_value("http_request_duration_seconds_count", labels=labels) or 0.0

    client.get("/")
    client.get("/")

    after = REGISTRY.get_sample_value("http_request_duration_seconds_count", labels=labels)
    assert after is not None
    assert after - before == pytest.approx(2.0)


def test_metrics_endpoint_exposes_duration_histogram(client: TestClient) -> None:
    """``/metrics`` exports the histogram's bucket / count / sum series.

    A ``prometheus_client.Histogram`` exposes ``<name>_bucket{le=…}``,
    ``<name>_count`` and ``<name>_sum``. The full labelled ``_count`` and
    ``_sum`` assertions prove the exposition reflects a real observation
    for the matched-template label set, not merely the HELP/TYPE preamble
    (which carries the bare metric name and would satisfy a loose
    substring check even with zero samples).
    """
    client.get("/")
    body = client.get("/metrics").text

    assert "http_request_duration_seconds_bucket{" in body
    assert 'le="+Inf"' in body
    assert 'http_request_duration_seconds_count{method="GET",path="/",status="200"}' in body
    assert 'http_request_duration_seconds_sum{method="GET",path="/",status="200"}' in body


def test_unmatched_routes_collapse_to_single_metric_label() -> None:
    """Distinct 404 paths fold into one ``path="__unmatched__"`` metric label.

    Hardening (#2886): an unauthenticated scanner spraying distinct
    non-existent paths must not mint one Prometheus label value per URL
    on the unauthenticated ``/metrics`` endpoint. Both the counter and
    the duration histogram collapse every unmatched route to the
    :data:`~meho_backplane.middleware.UNMATCHED_ROUTE_LABEL` constant,
    while the literal path survives only on the (unbounded-by-design)
    log line.
    """
    from fastapi import FastAPI

    probe = FastAPI()
    probe.add_middleware(RequestContextMiddleware)

    @probe.get("/exists")
    async def _exists() -> dict[str, str]:
        return {"ok": "yes"}

    probe_client = TestClient(probe)

    unmatched = {"method": "GET", "path": UNMATCHED_ROUTE_LABEL, "status": "404"}
    counter_before = REGISTRY.get_sample_value("http_requests_total", labels=unmatched) or 0.0
    hist_before = (
        REGISTRY.get_sample_value("http_request_duration_seconds_count", labels=unmatched) or 0.0
    )

    scan_paths = [f"/nonexistent-{uuid4().hex}" for _ in range(5)]
    for path in scan_paths:
        assert probe_client.get(path).status_code == 404

    counter_after = REGISTRY.get_sample_value("http_requests_total", labels=unmatched) or 0.0
    hist_after = (
        REGISTRY.get_sample_value("http_request_duration_seconds_count", labels=unmatched) or 0.0
    )

    # All five distinct scans folded into the single constant label —
    # both the counter and the histogram advanced by exactly five.
    assert counter_after - counter_before == pytest.approx(len(scan_paths))
    assert hist_after - hist_before == pytest.approx(len(scan_paths))

    # And not one literal scanned path minted its own label value.
    for path in scan_paths:
        literal = {"method": "GET", "path": path, "status": "404"}
        assert REGISTRY.get_sample_value("http_requests_total", labels=literal) is None
        assert (
            REGISTRY.get_sample_value("http_request_duration_seconds_count", labels=literal) is None
        )


# ---------------------------------------------------------------------------
# background-loop liveness gauges (#2888)
# ---------------------------------------------------------------------------


def test_background_loop_last_tick_goes_stale_while_others_advance() -> None:
    """A wedged loop's stamp stays behind while a healthy loop advances.

    The load-bearing liveness property: a loop that stops ticking keeps its
    old ``background_loop_last_tick_timestamp_seconds`` value while loops
    that keep ticking move theirs forward, so ``time() - stamp`` crosses the
    ``MehoBackgroundLoopStalled`` threshold for the stalled loop only. The
    injectable clock (``now=``) makes the assertion deterministic — no
    sleeps, no wall-clock flake.
    """
    from meho_backplane.metrics import note_loop_tick

    # Test-only loop labels so the shared process registry cannot leak real
    # loop series into (or out of) this test.
    t0 = 1_000_000.0
    note_loop_tick("test_loop_stalled", 10.0, now=t0)
    note_loop_tick("test_loop_healthy", 10.0, now=t0)

    # The healthy loop keeps ticking; the stalled one wedges after t0.
    note_loop_tick("test_loop_healthy", 10.0, now=t0 + 30.0)

    stalled = REGISTRY.get_sample_value(
        "background_loop_last_tick_timestamp_seconds", labels={"loop": "test_loop_stalled"}
    )
    healthy = REGISTRY.get_sample_value(
        "background_loop_last_tick_timestamp_seconds", labels={"loop": "test_loop_healthy"}
    )
    assert stalled == pytest.approx(t0)
    assert healthy == pytest.approx(t0 + 30.0)
    # The healthy stamp is 30 s ahead, so the stalled loop's staleness
    # (``time() - stamp``) is strictly larger — exactly what the alert trips
    # on while the healthy loop stays clear of the threshold.
    assert healthy - stalled == pytest.approx(30.0)


def test_note_loop_tick_publishes_interval_for_the_alert_threshold() -> None:
    """``note_loop_tick`` re-publishes the loop's interval on every tick.

    ``MehoBackgroundLoopStalled`` thresholds staleness at
    ``N x background_loop_interval_seconds{loop}``, so the interval gauge
    must carry each loop's current cadence for the per-loop threshold to
    mean anything.
    """
    from meho_backplane.metrics import note_loop_tick

    note_loop_tick("test_loop_interval", 42.0, now=1_000_000.0)

    assert REGISTRY.get_sample_value(
        "background_loop_interval_seconds", labels={"loop": "test_loop_interval"}
    ) == pytest.approx(42.0)
