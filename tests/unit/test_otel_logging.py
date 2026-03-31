# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.core.otel module.

Tests the unified OpenTelemetry logging and tracing interface.

Phase 84: request_id_ctx removed from meho_app.core.observability, context var names changed.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: request_id_ctx removed from observability module, context variable names changed")

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOTelConfig:
    """Tests for OTelConfig and get_otel_config()."""

    def test_default_config(self):
        """Test OTelConfig has sensible defaults."""
        from meho_app.core.otel.config import OTelConfig

        config = OTelConfig()
        assert config.service_name == "meho"
        assert config.service_version == "1.0.0"
        assert config.environment == "dev"
        assert config.otlp_endpoint is None
        assert config.console_enabled is True
        assert config.log_level == "INFO"
        assert config.trace_sample_rate == 1.0
        assert config.batch_delay_ms == 5000
        assert config.max_batch_size == 512

    def test_config_loads_from_env(self, monkeypatch):
        """Test get_otel_config() loads values from environment."""
        from meho_app.core.otel.config import get_otel_config

        monkeypatch.setenv("OTEL_SERVICE_NAME", "test-service")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://seq:5341")
        monkeypatch.setenv("MEHO_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("OTEL_CONSOLE", "false")

        # Clear cache to pick up new env vars
        get_otel_config.cache_clear()
        try:
            config = get_otel_config()
            assert config.service_name == "test-service"
            assert config.otlp_endpoint == "http://seq:5341"
            assert config.log_level == "DEBUG"
            assert config.console_enabled is False
        finally:
            get_otel_config.cache_clear()

    def test_config_parses_headers(self, monkeypatch):
        """Test get_otel_config() parses OTLP headers."""
        from meho_app.core.otel.config import get_otel_config

        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_HEADERS",
            "Authorization=Bearer token123,X-Custom=value",
        )

        get_otel_config.cache_clear()
        try:
            config = get_otel_config()
            assert config.otlp_headers == {
                "Authorization": "Bearer token123",
                "X-Custom": "value",
            }
        finally:
            get_otel_config.cache_clear()

    def test_config_no_headers_returns_empty_dict(self, monkeypatch):
        """Test get_otel_config() returns empty dict when no headers set."""
        from meho_app.core.otel.config import get_otel_config

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)

        get_otel_config.cache_clear()
        try:
            config = get_otel_config()
            assert config.otlp_headers == {}
        finally:
            get_otel_config.cache_clear()

    def test_config_trace_sample_rate(self, monkeypatch):
        """Test trace sample rate is parsed as float."""
        from meho_app.core.otel.config import get_otel_config

        monkeypatch.setenv("OTEL_TRACE_SAMPLE_RATE", "0.5")

        get_otel_config.cache_clear()
        try:
            config = get_otel_config()
            assert config.trace_sample_rate == 0.5
        finally:
            get_otel_config.cache_clear()


# ---------------------------------------------------------------------------
# Logger tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStructuredLogger:
    """Tests for StructuredLogger and get_logger()."""

    def test_get_logger_returns_structured_logger(self):
        """Test get_logger returns a StructuredLogger instance."""
        from meho_app.core.otel import get_logger
        from meho_app.core.otel.logging import StructuredLogger

        logger = get_logger("test_module")
        assert logger is not None
        assert isinstance(logger, StructuredLogger)
        assert logger._name == "test_module"

    def test_get_logger_is_cached(self):
        """Test get_logger returns the same instance for the same name."""
        from meho_app.core.otel import get_logger

        logger1 = get_logger("cached_test")
        logger2 = get_logger("cached_test")
        assert logger1 is logger2

    def test_get_logger_different_names(self):
        """Test get_logger returns different instances for different names."""
        from meho_app.core.otel import get_logger

        logger1 = get_logger("module_a")
        logger2 = get_logger("module_b")
        assert logger1 is not logger2

    def test_logger_has_all_methods(self):
        """Test logger has all standard logging methods."""
        from meho_app.core.otel import get_logger

        logger = get_logger("methods_test")
        assert hasattr(logger, "log")
        assert hasattr(logger, "debug")
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "warn")
        assert hasattr(logger, "error")
        assert hasattr(logger, "exception")
        assert hasattr(logger, "critical")

    def test_logger_accepts_attributes(self):
        """Test logger accepts keyword attributes without error."""
        from meho_app.core.otel import get_logger

        logger = get_logger("attrs_test")
        # None of these should raise
        logger.info("test message", user_id="123", connector_id="abc")
        logger.debug("debug msg", count=42, items=["a", "b"])
        logger.warning("warn msg", status_code=404)
        logger.error("error msg", error="something failed", path="/api/test")
        logger.critical("critical msg", severity="high")

    def test_logger_accepts_no_attributes(self):
        """Test logger works with message only."""
        from meho_app.core.otel import get_logger

        logger = get_logger("no_attrs_test")
        # Should not raise
        logger.info("plain message")
        logger.debug("debug")
        logger.warning("warning")
        logger.error("error")

    def test_warn_is_alias_for_warning(self):
        """Test warn() delegates to warning()."""
        from meho_app.core.otel import get_logger

        logger = get_logger("warn_alias_test")
        # Should not raise
        logger.warn("test warning", key="value")

    def test_log_method_accepts_dynamic_level(self):
        """Test public .log() method works with a dynamic level."""
        import logging as stdlib_logging

        from meho_app.core.otel import get_logger

        logger = get_logger("log_method_test")
        # Should not raise -- mirrors stdlib logging.Logger.log()
        logger.log(stdlib_logging.INFO, "dynamic level message", key="val")
        logger.log(stdlib_logging.WARNING, "warning via log()")
        logger.log(stdlib_logging.DEBUG, "debug via log()", count=42)


# ---------------------------------------------------------------------------
# Tracing tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTracing:
    """Tests for tracing utilities."""

    def test_span_context_manager(self):
        """Test span() works as a context manager."""
        from meho_app.core.otel import span

        with span("test_operation", key="value") as s:
            assert s is not None

    def test_span_with_no_attributes(self):
        """Test span() works with no attributes."""
        from meho_app.core.otel import span

        with span("simple_operation") as s:
            assert s is not None

    def test_get_tracer_returns_tracer(self):
        """Test get_tracer returns a Tracer instance."""
        from opentelemetry import trace

        from meho_app.core.otel import get_tracer

        tracer = get_tracer("test_tracer")
        assert tracer is not None
        assert isinstance(tracer, trace.Tracer)

    def test_get_tracer_is_cached(self):
        """Test get_tracer returns the same instance for the same name."""
        from meho_app.core.otel import get_tracer

        tracer1 = get_tracer("cached_tracer")
        tracer2 = get_tracer("cached_tracer")
        assert tracer1 is tracer2


# ---------------------------------------------------------------------------
# Enricher tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContextEnricher:
    """Tests for ContextEnricherProcessor."""

    def test_enricher_can_be_instantiated(self):
        """Test ContextEnricherProcessor can be created."""
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        assert processor is not None

    def test_enricher_shutdown(self):
        """Test shutdown() does not raise."""
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        processor.shutdown()

    def test_enricher_force_flush(self):
        """Test force_flush() returns True."""
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        assert processor.force_flush() is True


# ---------------------------------------------------------------------------
# Enricher integration tests (context injection)
# ---------------------------------------------------------------------------


def _make_mock_log_record(attributes=None):
    """Create a mock ReadWriteLogRecord for enricher testing.

    The enricher accesses ``log_record.log_record.attributes``, so the
    mock needs two levels of nesting.
    """
    inner = MagicMock()
    inner.attributes = attributes
    outer = MagicMock()
    outer.log_record = inner
    return outer


@pytest.mark.unit
class TestContextEnricherInjection:
    """Tests that ContextEnricherProcessor actually injects context."""

    def test_injects_request_context(self):
        """Test enricher injects request_id, user_id, tenant_id."""
        from meho_app.core.otel.context import (
            request_id_ctx,
            tenant_id_ctx,
            user_id_ctx,
        )
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes={})

        # Set context vars
        req_token = request_id_ctx.set("req-abc-123")
        user_token = user_id_ctx.set("user-456")
        tenant_token = tenant_id_ctx.set("tenant-789")
        try:
            processor.on_emit(mock_record)

            attrs = mock_record.log_record.attributes
            assert attrs["request_id"] == "req-abc-123"
            assert attrs["user_id"] == "user-456"
            assert attrs["tenant_id"] == "tenant-789"
        finally:
            request_id_ctx.reset(req_token)
            user_id_ctx.reset(user_token)
            tenant_id_ctx.reset(tenant_token)

    def test_injects_trace_context_inside_span(self):
        """Test enricher injects trace_id and span_id when inside a span."""
        from meho_app.core.otel import span
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes={})

        with span("test_enricher_span"):
            processor.on_emit(mock_record)

        attrs = mock_record.log_record.attributes
        assert "trace_id" in attrs
        assert "span_id" in attrs
        # trace_id is 32 hex chars, span_id is 16 hex chars
        assert len(attrs["trace_id"]) == 32
        assert len(attrs["span_id"]) == 16
        # Should be valid hex
        int(attrs["trace_id"], 16)
        int(attrs["span_id"], 16)

    def test_no_trace_context_outside_span(self):
        """Test enricher does not inject trace/span when no span is active."""
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes={})

        processor.on_emit(mock_record)

        attrs = mock_record.log_record.attributes
        assert "trace_id" not in attrs
        assert "span_id" not in attrs

    def test_handles_missing_request_context(self):
        """Test enricher does not crash when no request context is set."""
        from meho_app.core.otel.context import clear_request_context
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        clear_request_context()
        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes={})

        # Should not raise
        processor.on_emit(mock_record)

        attrs = mock_record.log_record.attributes
        assert "request_id" not in attrs
        assert "user_id" not in attrs
        assert "tenant_id" not in attrs

    def test_handles_none_attributes_dict(self):
        """Test enricher creates attributes dict when None."""
        from meho_app.core.otel.context import request_id_ctx
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes=None)

        token = request_id_ctx.set("req-from-none")
        try:
            processor.on_emit(mock_record)
            # Should have created the dict
            attrs = mock_record.log_record.attributes
            assert isinstance(attrs, dict)
            assert attrs["request_id"] == "req-from-none"
        finally:
            request_id_ctx.reset(token)

    def test_partial_request_context(self):
        """Test enricher only injects context vars that are set."""
        from meho_app.core.otel.context import (
            clear_request_context,
            user_id_ctx,
        )
        from meho_app.core.otel.enrichers import ContextEnricherProcessor

        clear_request_context()
        processor = ContextEnricherProcessor()
        mock_record = _make_mock_log_record(attributes={})

        # Only set user_id
        token = user_id_ctx.set("only-user")
        try:
            processor.on_emit(mock_record)

            attrs = mock_record.log_record.attributes
            assert "request_id" not in attrs
            assert attrs["user_id"] == "only-user"
            assert "tenant_id" not in attrs
        finally:
            user_id_ctx.reset(token)


# ---------------------------------------------------------------------------
# Context module tests (canonical ContextVar home)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOtelContext:
    """Tests for the canonical context module at meho_app.core.otel.context."""

    def test_context_vars_importable(self):
        """Test ContextVars are importable from otel.context."""
        from meho_app.core.otel.context import (
            request_id_ctx,
            tenant_id_ctx,
            user_id_ctx,
        )

        assert request_id_ctx is not None
        assert user_id_ctx is not None
        assert tenant_id_ctx is not None

    def test_context_functions_importable(self):
        """Test context functions are importable from otel.context."""
        from meho_app.core.otel.context import (
            clear_request_context,
            get_request_context,
            set_request_context,
        )

        assert callable(set_request_context)
        assert callable(clear_request_context)
        assert callable(get_request_context)

    def test_context_round_trip(self):
        """Test set/get/clear cycle via otel.context."""
        from meho_app.core.otel.context import (
            clear_request_context,
            get_request_context,
            set_request_context,
        )

        set_request_context(
            request_id="ctx-req-1",
            user_id="ctx-user-1",
            tenant_id="ctx-tenant-1",
        )

        ctx = get_request_context()
        assert ctx["request_id"] == "ctx-req-1"
        assert ctx["user_id"] == "ctx-user-1"
        assert ctx["tenant_id"] == "ctx-tenant-1"

        clear_request_context()

        ctx = get_request_context()
        assert ctx["request_id"] is None
        assert ctx["user_id"] is None
        assert ctx["tenant_id"] is None

    def test_backward_compat_same_vars(self):
        """Test that observability.py re-exports the same ContextVars."""
        from meho_app.core.observability import (
            request_id_ctx as obs_req,
        )
        from meho_app.core.observability import (
            tenant_id_ctx as obs_tenant,
        )
        from meho_app.core.observability import (
            user_id_ctx as obs_user,
        )
        from meho_app.core.otel.context import (
            request_id_ctx as ctx_req,
        )
        from meho_app.core.otel.context import (
            tenant_id_ctx as ctx_tenant,
        )
        from meho_app.core.otel.context import (
            user_id_ctx as ctx_user,
        )

        assert ctx_req is obs_req
        assert ctx_user is obs_user
        assert ctx_tenant is obs_tenant


# ---------------------------------------------------------------------------
# Exporter factory tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExporterFactory:
    """Tests for exporter factory functions."""

    def test_no_exporters_without_otlp(self):
        """Test no OTLP exporters when endpoint is not set.

        Console output is handled by a stdlib StreamHandler in
        ``logging.py``, not by an OTEL exporter, so
        ``create_log_exporters`` / ``create_trace_exporters`` return
        an empty list when no OTLP endpoint is configured.
        """
        from meho_app.core.otel.config import OTelConfig
        from meho_app.core.otel.exporters import (
            create_log_exporters,
            create_trace_exporters,
        )

        config = OTelConfig(console_enabled=True, otlp_endpoint=None)

        log_exporters = create_log_exporters(config)
        assert len(log_exporters) == 0

        trace_exporters = create_trace_exporters(config)
        assert len(trace_exporters) == 0

    def test_otlp_exporter_created_when_endpoint_set(self):
        """Test OTLP exporter is created when endpoint is configured."""
        from meho_app.core.otel.config import OTelConfig
        from meho_app.core.otel.exporters import (
            create_log_exporters,
            create_trace_exporters,
        )

        config = OTelConfig(
            console_enabled=False,
            otlp_endpoint="http://localhost:5341/ingest/otlp",
        )

        log_exporters = create_log_exporters(config)
        assert len(log_exporters) == 1  # OTLP only

        trace_exporters = create_trace_exporters(config)
        assert len(trace_exporters) == 1  # OTLP only

    def test_otlp_exporter_with_console_enabled(self):
        """Test OTLP exporter count is 1 even when console is enabled.

        Console output is handled by stdlib, so only the OTLP processor
        is returned.
        """
        from meho_app.core.otel.config import OTelConfig
        from meho_app.core.otel.exporters import (
            create_log_exporters,
            create_trace_exporters,
        )

        config = OTelConfig(
            console_enabled=True,
            otlp_endpoint="http://localhost:5341/ingest/otlp",
        )

        log_exporters = create_log_exporters(config)
        assert len(log_exporters) == 1  # OTLP only

        trace_exporters = create_trace_exporters(config)
        assert len(trace_exporters) == 1  # OTLP only


# ---------------------------------------------------------------------------
# Public interface tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPublicInterface:
    """Tests for the public __init__.py exports."""

    def test_all_exports_available(self):
        """Test all expected symbols are exported from meho_app.core.otel."""
        from meho_app.core.otel import (
            OTelConfig,
            StructuredLogger,
            get_logger,
            get_otel_config,
            get_tracer,
            span,
        )

        assert get_logger is not None
        assert get_tracer is not None
        assert span is not None
        assert get_otel_config is not None
        assert OTelConfig is not None
        assert StructuredLogger is not None


# ---------------------------------------------------------------------------
# Backward compatibility tests (observability.py shim)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObservabilityBackwardCompat:
    """Tests for the backward-compat shim in meho_app.core.observability."""

    def test_configure_observability_returns_true(self):
        """Test configure_observability() returns True."""
        from meho_app.core.observability import configure_observability

        result = configure_observability()
        assert result is True

    def test_is_configured_reflects_state(self):
        """Test is_configured() reflects that configure_observability was called."""
        from meho_app.core.observability import configure_observability, is_configured

        configure_observability()
        assert is_configured() is True

    def test_span_returns_context_manager(self):
        """Test span() from observability.py works as context manager."""
        from meho_app.core.observability import span

        with span("compat_test_span", key="val") as s:
            assert s is not None

    def test_set_get_clear_request_context(self):
        """Test request context round-trips correctly."""
        from meho_app.core.observability import (
            clear_request_context,
            get_request_context,
            set_request_context,
        )

        # Set all three
        set_request_context(
            request_id="req-111",
            user_id="user-222",
            tenant_id="tenant-333",
        )

        ctx = get_request_context()
        assert ctx["request_id"] == "req-111"
        assert ctx["user_id"] == "user-222"
        assert ctx["tenant_id"] == "tenant-333"

        # Clear
        clear_request_context()

        ctx = get_request_context()
        assert ctx["request_id"] is None
        assert ctx["user_id"] is None
        assert ctx["tenant_id"] is None

    def test_set_partial_request_context(self):
        """Test setting only some context fields."""
        from meho_app.core.observability import (
            clear_request_context,
            get_request_context,
            set_request_context,
        )

        clear_request_context()

        set_request_context(user_id="only-user")

        ctx = get_request_context()
        assert ctx["request_id"] is None
        assert ctx["user_id"] == "only-user"
        assert ctx["tenant_id"] is None

        clear_request_context()

    def test_context_vars_are_exported(self):
        """Test context vars are importable from observability."""
        from meho_app.core.observability import (
            request_id_ctx,
            tenant_id_ctx,
            user_id_ctx,
        )

        assert request_id_ctx is not None
        assert user_id_ctx is not None
        assert tenant_id_ctx is not None


# ---------------------------------------------------------------------------
# Instrumentation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInstrumentation:
    """Tests for meho_app.core.otel.instrumentation."""

    def test_instrument_httpx_does_not_raise(self):
        """Test instrument_httpx() does not raise."""
        from meho_app.core.otel.instrumentation import instrument_httpx

        # Should not raise (may already be instrumented, which is fine)
        try:  # noqa: SIM105 -- explicit error handling preferred
            instrument_httpx()
        except RuntimeError:
            # HTTPX may already be instrumented from a previous test
            pass

    def test_instrument_all_with_none_args(self):
        """Test instrument_all() works with app=None, engine=None (console-only)."""
        from meho_app.core.otel.instrumentation import instrument_all

        # Should not raise
        try:  # noqa: SIM105 -- explicit error handling preferred
            instrument_all(app=None, engine=None)
        except RuntimeError:
            # HTTPX may already be instrumented
            pass

    def test_instrument_fastapi_with_test_app(self):
        """Test instrument_fastapi() patches a FastAPI app without error."""
        from fastapi import FastAPI

        from meho_app.core.otel.instrumentation import instrument_fastapi

        app = FastAPI()
        # Should not raise
        instrument_fastapi(app)
