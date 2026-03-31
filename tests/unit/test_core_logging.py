# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.core.logging

Phase 84: meho_app.core.logging module was removed; logging is now handled via
meho_app.core.observability. All tests skipped.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: meho_app.core.logging module was removed, logging now in meho_app.core.observability")


@pytest.mark.unit
def test_setup_logging():
    """Test setup_logging configures logging"""
    setup_logging(log_level="INFO", env="test")

    # Should not raise
    logger = get_logger("test")
    assert logger is not None


@pytest.mark.unit
def test_get_logger():
    """Test get_logger returns a logger instance"""
    setup_logging(log_level="INFO", env="test")

    logger = get_logger("test_module")
    assert logger is not None

    # Should have logging methods
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")
    assert hasattr(logger, "warning")
    assert hasattr(logger, "debug")


@pytest.mark.unit
def test_logger_info(capsys):
    """Test logger can log info messages"""
    setup_logging(log_level="INFO", env="test")

    logger = get_logger("test")
    logger.info("test message")

    # In test mode, output goes to stderr
    captured = capsys.readouterr()
    # Message should be somewhere in output
    assert "test message" in captured.err or "test message" in captured.out


@pytest.mark.unit
def test_logger_with_context(capsys):
    """Test logger includes context variables"""
    setup_logging(log_level="INFO", env="test")

    logger = get_logger("test")
    add_log_context(tenant_id="tenant-123", user_id="user-456")
    logger.info("test with context")

    captured = capsys.readouterr()
    output = captured.err + captured.out

    # Context should be in log output
    assert "test with context" in output
    # In structured logging, context may be in separate fields


@pytest.mark.unit
def test_clear_log_context():
    """Test clear_log_context removes context variables"""
    setup_logging(log_level="INFO", env="test")

    add_log_context(tenant_id="tenant-123")
    clear_log_context()

    # Should not raise
    logger = get_logger("test")
    logger.info("test after clear")


@pytest.mark.unit
def test_logging_respects_log_level():
    """Test that log level is respected"""
    setup_logging(log_level="WARNING", env="test")

    logger = get_logger("test")

    # Should be able to call debug (even if not output)
    logger.debug("debug message")
    logger.info("info message")
    logger.warning("warning message")

    # Should not raise


@pytest.mark.unit
def test_logging_dev_vs_prod():
    """Test different logging formats for dev vs prod"""
    # Dev mode
    setup_logging(log_level="INFO", env="dev")
    logger_dev = get_logger("test_dev")
    assert logger_dev is not None

    # Prod mode (JSON output)
    setup_logging(log_level="INFO", env="prod")
    logger_prod = get_logger("test_prod")
    assert logger_prod is not None
