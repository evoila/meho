"""Tests for audit log writer with secret redaction."""

import json
from pathlib import Path

import pytest

from meho_claude.core.audit import _redact_secrets, audit_log


class TestRedactSecrets:
    def test_redacts_password(self):
        result = _redact_secrets({"password": "secret123"})
        assert result["password"] == "***REDACTED***"

    def test_redacts_token(self):
        result = _redact_secrets({"token": "abc"})
        assert result["token"] == "***REDACTED***"

    def test_redacts_secret(self):
        result = _redact_secrets({"secret": "xyz"})
        assert result["secret"] == "***REDACTED***"

    def test_redacts_api_key(self):
        result = _redact_secrets({"api_key": "k123"})
        assert result["api_key"] == "***REDACTED***"

    def test_redacts_apikey(self):
        result = _redact_secrets({"apikey": "k123"})
        assert result["apikey"] == "***REDACTED***"

    def test_redacts_authorization(self):
        result = _redact_secrets({"authorization": "Bearer ..."})
        assert result["authorization"] == "***REDACTED***"

    def test_case_insensitive(self):
        result = _redact_secrets({"PASSWORD": "secret", "Token": "abc"})
        assert result["PASSWORD"] == "***REDACTED***"
        assert result["Token"] == "***REDACTED***"

    def test_preserves_non_secret_keys(self):
        result = _redact_secrets({"username": "admin", "host": "example.com"})
        assert result["username"] == "admin"
        assert result["host"] == "example.com"

    def test_handles_empty_dict(self):
        assert _redact_secrets({}) == {}

    def test_handles_nested_dicts(self):
        """Nested dicts should have their secret keys redacted too."""
        result = _redact_secrets({"auth": {"token": "secret"}, "host": "x"})
        assert result["auth"]["token"] == "***REDACTED***"
        assert result["host"] == "x"


class TestAuditLog:
    def test_appends_json_line(self, tmp_path):
        log_path = tmp_path / "audit.log"
        audit_log(
            log_path=log_path,
            connector="my-api",
            operation="list_users",
            trust_tier="READ",
            params={"page": 1},
            result_status="success",
        )
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["connector"] == "my-api"
        assert entry["operation"] == "list_users"
        assert entry["trust_tier"] == "READ"
        assert entry["params"] == {"page": 1}
        assert entry["result"] == "success"
        assert "ts" in entry

    def test_appends_multiple_entries(self, tmp_path):
        log_path = tmp_path / "audit.log"
        for i in range(3):
            audit_log(
                log_path=log_path,
                connector="api",
                operation=f"op_{i}",
                trust_tier="READ",
                params={},
                result_status="ok",
            )
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_redacts_secrets_in_params(self, tmp_path):
        log_path = tmp_path / "audit.log"
        audit_log(
            log_path=log_path,
            connector="api",
            operation="login",
            trust_tier="WRITE",
            params={"username": "admin", "password": "secret123", "token": "abc"},
            result_status="ok",
        )
        entry = json.loads(log_path.read_text().strip())
        assert entry["params"]["username"] == "admin"
        assert entry["params"]["password"] == "***REDACTED***"
        assert entry["params"]["token"] == "***REDACTED***"

    def test_ts_is_iso8601(self, tmp_path):
        log_path = tmp_path / "audit.log"
        audit_log(
            log_path=log_path,
            connector="api",
            operation="test",
            trust_tier="READ",
            params={},
            result_status="ok",
        )
        entry = json.loads(log_path.read_text().strip())
        # ISO 8601 format check: should contain T and end with Z or +00:00
        assert "T" in entry["ts"]

    def test_compact_json_output(self, tmp_path):
        log_path = tmp_path / "audit.log"
        audit_log(
            log_path=log_path,
            connector="api",
            operation="test",
            trust_tier="READ",
            params={},
            result_status="ok",
        )
        line = log_path.read_text().strip()
        # Compact JSON should not have spaces after : or ,
        assert ": " not in line or line.count(": ") == 0  # Allow for ts value with :
        # More reliable: no ", " pattern after a key
        parsed = json.loads(line)
        repacked = json.dumps(parsed, separators=(",", ":"))
        assert line == repacked

    def test_creates_log_file_if_not_exists(self, tmp_path):
        log_path = tmp_path / "subdir" / "audit.log"
        # Parent dir exists via tmp_path, but subdir doesn't
        log_path.parent.mkdir(parents=True, exist_ok=True)
        audit_log(
            log_path=log_path,
            connector="api",
            operation="test",
            trust_tier="READ",
            params={},
            result_status="ok",
        )
        assert log_path.exists()
