# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for credential scrubbing and payload truncation.

Tests the security utilities that prevent sensitive data from being stored.
"""

from meho_app.modules.agents.persistence.scrubber import (
    REDACTED,
    is_sensitive_key,
    sanitize_headers,
    sanitize_http_body,
    sanitize_tool_output,
    scrub_sensitive_data,
    scrub_value,
    truncate_payload,
    truncate_result_sample,
)


class TestIsSensitiveKey:
    """Tests for is_sensitive_key function."""

    def test_exact_matches(self):
        """Test exact sensitive key matching."""
        assert is_sensitive_key("password") is True
        assert is_sensitive_key("api_key") is True
        assert is_sensitive_key("token") is True
        assert is_sensitive_key("secret") is True
        assert is_sensitive_key("authorization") is True

    def test_case_insensitive(self):
        """Test case insensitive matching."""
        assert is_sensitive_key("PASSWORD") is True
        assert is_sensitive_key("Api_Key") is True
        assert is_sensitive_key("TOKEN") is True

    def test_partial_matches(self):
        """Test partial matching."""
        assert is_sensitive_key("my_password") is True
        assert is_sensitive_key("api_key_secret") is True
        assert is_sensitive_key("auth_token") is True

    def test_non_sensitive_keys(self):
        """Test non-sensitive keys are not flagged."""
        assert is_sensitive_key("name") is False
        assert is_sensitive_key("email") is False
        assert is_sensitive_key("id") is False
        assert is_sensitive_key("count") is False


class TestScrubValue:
    """Tests for scrub_value function."""

    def test_bearer_token_scrubbed(self):
        """Test Bearer tokens are scrubbed."""
        value = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = scrub_value(value)
        assert "eyJhbG" not in result
        assert REDACTED in result

    def test_basic_auth_scrubbed(self):
        """Test Basic auth is scrubbed."""
        value = "Basic dXNlcm5hbWU6cGFzc3dvcmQ="
        result = scrub_value(value)
        assert "dXNlcm" not in result
        assert REDACTED in result

    def test_password_in_json_scrubbed(self):
        """Test password in JSON-like string is scrubbed."""
        value = '{"username": "admin", "password": "secret123"}'
        result = scrub_value(value)
        assert "secret123" not in result

    def test_non_string_unchanged(self):
        """Test non-string values pass through."""
        assert scrub_value(123) == 123
        assert scrub_value(None) is None
        assert scrub_value([1, 2, 3]) == [1, 2, 3]


class TestScrubSensitiveData:
    """Tests for scrub_sensitive_data function."""

    def test_simple_dict(self):
        """Test simple dictionary scrubbing."""
        data = {
            "username": "admin",
            "password": "secret123",
            "count": 5,
        }
        result = scrub_sensitive_data(data)

        assert result["username"] == "admin"
        assert result["password"] == REDACTED
        assert result["count"] == 5

    def test_nested_dict(self):
        """Test nested dictionary scrubbing."""
        data = {
            "user": {
                "name": "John",
                "config": {
                    "api_key": "abc123",
                    "token": "xyz789",
                },
            },
        }
        result = scrub_sensitive_data(data)

        assert result["user"]["name"] == "John"
        # "config" is not sensitive, so the nested dict is preserved
        assert result["user"]["config"]["api_key"] == REDACTED
        assert result["user"]["config"]["token"] == REDACTED

    def test_sensitive_parent_key_redacts_all(self):
        """Test that sensitive parent keys redact the whole value."""
        data = {
            "credentials": {
                "api_key": "abc123",
                "token": "xyz789",
            },
        }
        result = scrub_sensitive_data(data)

        # "credentials" is a sensitive key, so entire value is redacted
        assert result["credentials"] == REDACTED

    def test_list_of_dicts(self):
        """Test list of dictionaries scrubbing."""
        data = {
            "users": [
                {"name": "Alice", "password": "pass1"},
                {"name": "Bob", "password": "pass2"},
            ]
        }
        result = scrub_sensitive_data(data)

        assert result["users"][0]["name"] == "Alice"
        assert result["users"][0]["password"] == REDACTED
        assert result["users"][1]["password"] == REDACTED


class TestSanitizeHeaders:
    """Tests for sanitize_headers function."""

    def test_authorization_redacted(self):
        """Test Authorization header is redacted."""
        headers = {
            "Authorization": "Bearer abc123",
            "Content-Type": "application/json",
        }
        result = sanitize_headers(headers)

        assert result["Authorization"] == REDACTED
        assert result["Content-Type"] == "application/json"

    def test_api_key_headers_redacted(self):
        """Test API key headers are redacted."""
        headers = {
            "X-API-Key": "secret-key",
            "x-auth-token": "token123",
        }
        result = sanitize_headers(headers)

        assert result["X-API-Key"] == REDACTED
        assert result["x-auth-token"] == REDACTED

    def test_cookie_redacted(self):
        """Test Cookie headers are redacted."""
        headers = {
            "Cookie": "session=abc123",
            "Accept": "application/json",
        }
        result = sanitize_headers(headers)

        assert result["Cookie"] == REDACTED
        assert result["Accept"] == "application/json"

    def test_none_input(self):
        """Test None input returns None."""
        assert sanitize_headers(None) is None


class TestTruncatePayload:
    """Tests for truncate_payload function."""

    def test_small_payload_unchanged(self):
        """Test small payloads pass through."""
        data = "Hello, World!"
        result = truncate_payload(data)
        assert result == data

    def test_large_payload_truncated(self):
        """Test large payloads are truncated."""
        data = "x" * 100000  # 100KB
        result = truncate_payload(data, max_size=1000)

        assert len(result) < 100000
        assert "[TRUNCATED:" in result
        assert "Original size: 100000 bytes" in result

    def test_custom_max_size(self):
        """Test custom max size."""
        data = "x" * 200
        result = truncate_payload(data, max_size=100)

        assert len(result) < 200
        assert "[TRUNCATED:" in result


class TestTruncateResultSample:
    """Tests for truncate_result_sample function."""

    def test_small_sample_unchanged(self):
        """Test small samples pass through."""
        results = [{"id": 1}, {"id": 2}]
        result = truncate_result_sample(results, max_rows=10)

        assert len(result) == 2
        assert result[0]["id"] == 1

    def test_large_sample_truncated(self):
        """Test large samples are truncated."""
        results = [{"id": i} for i in range(100)]
        result = truncate_result_sample(results, max_rows=5)

        assert len(result) == 6  # 5 rows + 1 note
        assert result[0]["id"] == 0
        assert result[4]["id"] == 4
        assert "_note" in result[5]
        assert "5 of 100" in result[5]["_note"]

    def test_none_input(self):
        """Test None input returns None."""
        assert truncate_result_sample(None) is None


class TestSanitizeHttpBody:
    """Tests for sanitize_http_body function."""

    def test_body_with_token_scrubbed(self):
        """Test body with token is scrubbed."""
        body = '{"access_token": "Bearer secret123"}'
        result = sanitize_http_body(body)

        assert "secret123" not in result

    def test_large_body_truncated(self):
        """Test large body is truncated."""
        # Use a pattern that won't trigger sensitive pattern detection
        body = "Hello World! " * 10000
        result = sanitize_http_body(body, max_size=1000)

        assert "[TRUNCATED:" in result or len(result) <= 1100  # Allow some overhead

    def test_none_input(self):
        """Test None input returns None."""
        assert sanitize_http_body(None) is None


class TestSanitizeToolOutput:
    """Tests for sanitize_tool_output function."""

    def test_dict_output_scrubbed(self):
        """Test dictionary output is scrubbed."""
        output = {"result": "ok", "password": "secret"}
        result = sanitize_tool_output(output)

        assert result["result"] == "ok"
        assert result["password"] == REDACTED

    def test_list_of_dicts_scrubbed(self):
        """Test list of dicts is scrubbed and truncated."""
        output = [{"id": i, "token": f"token{i}"} for i in range(20)]
        result = sanitize_tool_output(output)

        # Should be truncated and scrubbed
        assert len(result) <= 11  # 10 + note
        assert result[0]["token"] == REDACTED

    def test_string_output_scrubbed(self):
        """Test string output is scrubbed."""
        output = "Token: Bearer abc123xyz"
        result = sanitize_tool_output(output)

        # Bearer token should be redacted
        assert "abc123xyz" not in result or REDACTED in result

    def test_none_input(self):
        """Test None input returns None."""
        assert sanitize_tool_output(None) is None
