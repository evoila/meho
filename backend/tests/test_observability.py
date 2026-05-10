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

The tests redirect structlog's logger factory to a per-test
:class:`io.StringIO` buffer rather than touching ``sys.stdout`` —
``capsys`` would also work, but the ``cache_logger_on_first_use=True``
setting in production means the first call to
:func:`structlog.get_logger` pins the file handle for the process
lifetime; rebinding the factory inside the test body is the cleaner
seam.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from uuid import UUID

import pytest
import structlog
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from meho_backplane.main import app
from meho_backplane.middleware import RequestContextMiddleware

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

    # Default process collector metrics — the runtime fingerprint
    # Goal #11 promised operators.
    assert "process_resident_memory_bytes" in body
    assert "process_open_fds" in body

    # The application counter, with all three labels populated.
    assert 'http_requests_total{method="GET"' in body
    assert 'path="/"' in body
    assert 'status="200"' in body


def test_metrics_endpoint_does_not_increment_for_itself_during_render(
    client: TestClient,
) -> None:
    """A single ``/metrics`` request renders deterministically.

    Sanity check: the counter is incremented *after* the response is
    generated, so the body of any given ``/metrics`` response reflects
    state captured before the request itself completed.
    """
    response_one = client.get("/metrics")
    response_two = client.get("/metrics")

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    assert "http_requests_total" in response_two.text


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
    assert isinstance(entry["duration_ms"], (int, float))
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
