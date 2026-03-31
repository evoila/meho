# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for response formatters module.

Tests format adapters (markdown -> connector-specific format) and
Jinja2 parameter mapping for response channel payloads.
"""

import pytest


class TestFormatForConnector:
    """Tests for the format_for_connector dispatcher."""

    def test_format_jira_passthrough(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("jira", "**bold** text")
        assert result == "**bold** text"

    def test_format_confluence_passthrough(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("confluence", "**bold** text")
        assert result == "**bold** text"

    def test_format_slack_bold(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("slack", "**bold** text")
        assert result == "*bold* text"

    def test_format_slack_headers(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("slack", "# Heading\n## Subheading")
        assert result == "*Heading*\n*Subheading*"

    def test_format_slack_links(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("slack", "[link](https://example.com)")
        assert result == "<https://example.com|link>"

    def test_format_email_passthrough(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("email", "**bold**")
        assert result == "**bold**"

    def test_format_unknown_plaintext_fallback(self):
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("unknown_type", "**bold**")
        assert result == "bold"

    def test_format_slack_nested_bold(self):
        """Edge case: nested formatting should be handled gracefully."""
        from meho_app.modules.connectors.response_formatters import format_for_connector

        result = format_for_connector("slack", "**nested *italic* bold**")
        # After bold conversion: *nested *italic* bold* -- Slack will render this
        # The key is it doesn't crash
        assert isinstance(result, str)
        assert "italic" in result


class TestRenderResponseParameters:
    """Tests for Jinja2 parameter mapping."""

    def test_render_with_payload_and_result(self):
        from meho_app.modules.connectors.response_formatters import render_response_parameters

        mapping = {
            "issue_key": "{{payload.issue.key}}",
            "body": "{{result}}",
        }
        payload = {"issue": {"key": "PROJ-123"}}
        result = render_response_parameters(
            parameter_mapping=mapping,
            payload=payload,
            result="Investigation complete",
            session_id="abc-123",
            session_title="Test Session",
        )
        assert result == {"issue_key": "PROJ-123", "body": "Investigation complete"}

    def test_render_session_variables(self):
        from meho_app.modules.connectors.response_formatters import render_response_parameters

        mapping = {
            "session": "{{session_id}}",
            "title": "{{session_title}}",
        }
        result = render_response_parameters(
            parameter_mapping=mapping,
            payload={},
            result="",
            session_id="sess-456",
            session_title="My Investigation",
        )
        assert result == {"session": "sess-456", "title": "My Investigation"}

    def test_render_invalid_template_returns_empty(self):
        from meho_app.modules.connectors.response_formatters import render_response_parameters

        mapping = {
            "bad": "{{unclosed",
        }
        result = render_response_parameters(
            parameter_mapping=mapping,
            payload={},
            result="",
            session_id="",
            session_title="",
        )
        assert result == {}
