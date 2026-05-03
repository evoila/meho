# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for ``scripts/issue-license.py`` (#519).

Loads the hyphenated script via ``importlib.util`` because its filename is not
a legal Python module name. Mocks ``op`` CLI calls and the audit-log repository
so tests run without a real 1Password session or database.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "issue-license.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("issue_license", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> types.ModuleType:
    return _load_script()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _raw_private_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def fresh_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Fresh Ed25519 keypair plus the base64url-encoded public half."""
    private = Ed25519PrivateKey.generate()
    return private, _public_b64(private)


@pytest.fixture
def patch_public_key(
    monkeypatch: pytest.MonkeyPatch,
    fresh_keypair: tuple[Ed25519PrivateKey, str],
) -> str:
    """Monkeypatch ``licensing._PUBLIC_KEY_B64`` so verify tests roundtrip cleanly."""
    from meho_app.core import licensing

    _, public_b64 = fresh_keypair
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_B64", public_b64)
    monkeypatch.setattr(licensing, "_TEST_PUBLIC_KEY_B64", public_b64)
    return public_b64


@pytest.fixture
def stub_op(monkeypatch: pytest.MonkeyPatch, script: types.ModuleType) -> None:
    """No-op ``op`` pre-flight + dummy secret-reference env var.

    Tests that exercise vault failure modes don't use this fixture; they set
    up their own ``op`` mocks and ``MEHO_LICENSE_SIGNING_KEY_REF`` value.
    """
    monkeypatch.setattr(script, "_check_op_available", lambda: None)
    monkeypatch.setenv(script._SECRET_REF_ENV, "op://test-vault/test-item/password")


@pytest.fixture
def stub_record_issuance_success(
    monkeypatch: pytest.MonkeyPatch, script: types.ModuleType
) -> list[dict[str, Any]]:
    """Capture audit-log calls without touching the DB.

    Returns a list that tests can inspect to assert ``record_issuance`` was
    invoked with the right payload + issuer.
    """
    captured: list[dict[str, Any]] = []

    async def _capture(payload_dict: dict[str, Any], issuer: str) -> None:
        captured.append({"payload": payload_dict, "issuer": issuer})

    monkeypatch.setattr(script, "_record_issuance", _capture)
    return captured


@pytest.fixture
def stub_record_issuance_failure(monkeypatch: pytest.MonkeyPatch, script: types.ModuleType) -> None:
    """Force the audit-log path to raise — exercises the fail-closed contract."""

    async def _raise(payload_dict: dict[str, Any], issuer: str) -> None:
        raise RuntimeError("simulated audit-log failure")

    monkeypatch.setattr(script, "_record_issuance", _raise)


@pytest.fixture
def stub_private_key(
    monkeypatch: pytest.MonkeyPatch,
    script: types.ModuleType,
    fresh_keypair: tuple[Ed25519PrivateKey, str],
) -> Ed25519PrivateKey:
    """Replace vault retrieval with the fresh test keypair's raw private bytes."""
    private, _ = fresh_keypair
    raw = _raw_private_bytes(private)
    monkeypatch.setattr(script, "_read_private_key_from_op", lambda _ref: raw)
    return private


_BASE_ISSUE_ARGS = [
    "issue",
    "--org",
    "Acme Corp",
    "--tier",
    "enterprise",
    "--features",
    "multi_tenant,sso",
    "--issuer",
    "ops@evoila.com",
]


# ============================================================================
# Argument validation
# ============================================================================


class TestArgumentValidation:
    def test_missing_required_org_exits_non_zero(
        self, runner: CliRunner, script: types.ModuleType
    ) -> None:
        result = runner.invoke(
            script.app,
            [
                "issue",
                "--tier",
                "enterprise",
                "--features",
                "x",
                "--issuer",
                "ops",
            ],
        )
        assert result.exit_code != 0

    def test_missing_required_issuer_exits_non_zero(
        self, runner: CliRunner, script: types.ModuleType
    ) -> None:
        result = runner.invoke(
            script.app,
            [
                "issue",
                "--org",
                "X",
                "--tier",
                "enterprise",
                "--features",
                "x",
            ],
        )
        assert result.exit_code != 0

    def test_empty_features_string_errors(
        self, runner: CliRunner, script: types.ModuleType, stub_op: None
    ) -> None:
        result = runner.invoke(
            script.app,
            [
                "issue",
                "--org",
                "X",
                "--tier",
                "enterprise",
                "--features",
                "  ,  ",
                "--issuer",
                "ops",
            ],
        )
        assert result.exit_code == 1
        assert "--features" in result.output
        assert "non-empty" in result.output

    def test_bad_expires_at_format_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_record_issuance_success: list[dict[str, Any]],
        stub_private_key: Ed25519PrivateKey,
    ) -> None:
        result = runner.invoke(
            script.app,
            [*_BASE_ISSUE_ARGS, "--expires-at", "not-a-date"],
        )
        assert result.exit_code == 1
        assert "ISO 8601" in result.output
        assert stub_record_issuance_success == [], "audit-log should not be called on bad input"


# ============================================================================
# Vault retrieval (op CLI)
# ============================================================================


class TestVaultRetrieval:
    def test_op_not_on_path_errors_with_install_hint(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(script.shutil, "which", lambda _name: None)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert "1Password CLI" in result.output
        assert "PATH" in result.output

    def test_op_whoami_failure_errors_with_reauth_hint(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/local/bin/op")

        def _fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
            assert argv[:2] == ["op", "whoami"]
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(script.subprocess, "run", _fake_run)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert "session is not active" in result.output

    def test_op_read_failure_does_not_echo_stderr(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``op read`` non-zero is surfaced cleanly; raw stderr (which can echo
        the secret reference URI) MUST NOT appear in operator-visible output."""
        monkeypatch.setenv(script._SECRET_REF_ENV, "op://test-vault/test-item/password")
        monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/local/bin/op")
        secret_in_stderr = "op://meho-x-vault/<SHOULD-NOT-LEAK>/password"

        def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["op", "whoami"]:
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            if argv[:2] == ["op", "read"]:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=argv, output="", stderr=secret_in_stderr
                )
            raise AssertionError(f"unexpected invocation: {argv}")

        monkeypatch.setattr(script.subprocess, "run", _fake_run)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert "`op read` failed" in result.output
        assert secret_in_stderr not in result.output

    def test_non_ascii_value_errors_with_friendly_message(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-ASCII vault content triggers UnicodeEncodeError on b64 decode.

        ``base64.urlsafe_b64decode`` permissively strips invalid alphabet chars,
        so most "looks-wrong" ASCII input still returns bytes (caught by the
        length check). Non-ASCII input is the realistic failure mode that hits
        the format-error branch.
        """
        monkeypatch.setenv(script._SECRET_REF_ENV, "op://test-vault/test-item/password")
        monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/local/bin/op")

        def _fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["op", "whoami"]:
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            if argv[:2] == ["op", "read"]:
                # 'ñ' is non-ASCII; base64.urlsafe_b64decode raises
                # UnicodeEncodeError (a ValueError subclass) when ascii-encoding it.
                return subprocess.CompletedProcess(argv, 0, stdout="ñ\n", stderr="")
            raise AssertionError(f"unexpected invocation: {argv}")

        monkeypatch.setattr(script.subprocess, "run", _fake_run)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert "base64url" in result.output

    def test_wrong_length_value_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # base64url of 30 bytes (not 32) = 40 chars without padding
        thirty_bytes = base64.urlsafe_b64encode(b"\x00" * 30).rstrip(b"=").decode()
        monkeypatch.setenv(script._SECRET_REF_ENV, "op://test-vault/test-item/password")
        monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/local/bin/op")

        def _fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["op", "whoami"]:
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            if argv[:2] == ["op", "read"]:
                return subprocess.CompletedProcess(argv, 0, stdout=thirty_bytes + "\n", stderr="")
            raise AssertionError(f"unexpected invocation: {argv}")

        monkeypatch.setattr(script.subprocess, "run", _fake_run)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert "30 bytes" in result.output
        assert "Ed25519 expects 32" in result.output

    def test_resolve_secret_ref_returns_env_value(
        self,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom = "op://test-vault/test-item/password"
        monkeypatch.setenv(script._SECRET_REF_ENV, custom)
        assert script._resolve_secret_ref() == custom

    def test_unset_env_errors_with_runbook_pointer(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Vault layout (vault name + item title) is NOT in the public mirror;
        the env var is the boundary that keeps it that way."""
        monkeypatch.delenv(script._SECRET_REF_ENV, raising=False)
        monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/local/bin/op")

        def _fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["op", "whoami"]:
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            raise AssertionError(f"unexpected invocation: {argv}")

        monkeypatch.setattr(script.subprocess, "run", _fake_run)
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 1
        assert script._SECRET_REF_ENV in result.output
        assert "license-key-custody.md" in result.output

    def test_unset_env_errors_when_resolve_called_directly(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(script._SECRET_REF_ENV, raising=False)
        with pytest.raises(typer.Exit) as exc_info:
            script._resolve_secret_ref()
        assert exc_info.value.exit_code == 1


# ============================================================================
# Pure signing
# ============================================================================


class TestSigning:
    def test_token_has_three_dot_separated_segments(
        self,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        private, _ = fresh_keypair
        payload = {"license_id": "lic-1", "org": "X", "tier": "enterprise", "features": []}
        token = script._sign_token(_raw_private_bytes(private), payload)
        assert token.count(".") == 2

    def test_token_header_decodes_to_eddsa_meho_license(
        self,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        private, _ = fresh_keypair
        token = script._sign_token(
            _raw_private_bytes(private),
            {"license_id": "x", "org": "x", "tier": "x", "features": []},
        )
        header_b64 = token.split(".")[0]
        header = json.loads(script._b64url_decode(header_b64))
        assert header == {"alg": "EdDSA", "typ": "MEHO-LICENSE"}

    def test_signature_is_64_raw_bytes(
        self,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        private, _ = fresh_keypair
        token = script._sign_token(
            _raw_private_bytes(private),
            {"license_id": "x", "org": "x", "tier": "x", "features": []},
        )
        sig_b64 = token.split(".")[2]
        sig_raw = script._b64url_decode(sig_b64)
        assert len(sig_raw) == 64

    def test_token_roundtrips_through_validator(
        self,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
        patch_public_key: str,
    ) -> None:
        from meho_app.core.licensing import _validate_license_key

        private, _ = fresh_keypair
        payload = {
            "license_id": "lic-roundtrip",
            "org": "Acme",
            "tier": "enterprise",
            "features": ["multi_tenant", "sso"],
            "issued_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "max_tenants": 5,
        }
        token = script._sign_token(_raw_private_bytes(private), payload)

        validated = _validate_license_key(token)

        assert validated is not None
        assert validated.org == "Acme"
        assert validated.tier == "enterprise"
        assert validated.features == ["multi_tenant", "sso"]
        assert validated.max_tenants == 5

    def test_token_with_wrong_private_key_fails_validation(
        self,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
        patch_public_key: str,
    ) -> None:
        from meho_app.core.licensing import _validate_license_key

        # Sign with a DIFFERENT private key than the patched-in public half.
        wrong = Ed25519PrivateKey.generate()
        token = script._sign_token(
            _raw_private_bytes(wrong),
            {
                "license_id": "x",
                "org": "x",
                "tier": "x",
                "features": [],
                "issued_at": datetime.now(UTC).isoformat(),
            },
        )
        assert _validate_license_key(token) is None


# ============================================================================
# Audit-log integration (success + fail-closed)
# ============================================================================


class TestAuditLogIntegration:
    def test_record_issuance_called_with_payload_and_issuer(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.token"
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code == 0, result.output
        assert len(stub_record_issuance_success) == 1
        call = stub_record_issuance_success[0]
        assert call["issuer"] == "ops@evoila.com"
        assert call["payload"]["org"] == "Acme Corp"
        assert call["payload"]["tier"] == "enterprise"
        assert call["payload"]["features"] == ["multi_tenant", "sso"]
        assert "license_id" in call["payload"]

    def test_audit_failure_aborts_issuance_and_writes_no_file(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_failure: None,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "should-not-exist.token"
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code != 0
        assert not target.exists(), "fail-closed: no file when audit-log raised"

    def test_audit_failure_aborts_before_token_reaches_stdout(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_failure: None,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code != 0
        # No 3-segment token-shaped string on stdout when audit fails.
        # (We can't check the exact token because UUID is random per call,
        # but we can assert the output contains no 3-dot segment that decodes.)
        for line in result.output.splitlines():
            assert line.count(".") < 2 or " " in line, (
                f"suspicious token-shaped line on stdout: {line!r}"
            )


# ============================================================================
# Output modes (stdout / --output)
# ============================================================================


class TestOutputModes:
    def test_stdout_default_emits_token_on_stdout(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        patch_public_key: str,
    ) -> None:
        from meho_app.core.licensing import _validate_license_key

        result = runner.invoke(script.app, _BASE_ISSUE_ARGS)
        assert result.exit_code == 0, result.output
        # The token is the only stdout line shaped like "x.y.z" with no spaces.
        token_lines = [
            line.strip()
            for line in result.output.splitlines()
            if line.count(".") == 2 and " " not in line.strip()
        ]
        assert len(token_lines) == 1, f"expected one token line, got: {token_lines}"
        token = token_lines[0]
        assert _validate_license_key(token) is not None

    def test_output_file_writes_0600(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.token"
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code == 0, result.output
        assert target.exists()
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"

    def test_output_file_contains_token_and_stdout_does_not(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        patch_public_key: str,
        tmp_path: Path,
    ) -> None:
        from meho_app.core.licensing import _validate_license_key

        target = tmp_path / "out.token"
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code == 0
        token_in_file = target.read_text(encoding="utf-8").strip()
        # Token validates against the embedded public key (patched).
        assert _validate_license_key(token_in_file) is not None
        # Token MUST NOT appear anywhere in CLI output when --output is set.
        assert token_in_file not in result.output

    def test_output_refuses_to_overwrite_existing_file(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.token"
        target.write_text("preexisting", encoding="utf-8")
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output
        assert target.read_text(encoding="utf-8") == "preexisting"
        # Fail-closed regression lock-in: the audit log must not record
        # orphan rows for tokens the operator never received. If a future
        # change re-orders audit before output preflight, this assertion
        # will catch it.
        assert stub_record_issuance_success == [], (
            "audit-log was called for a token that was never written"
        )

    def test_license_id_surfaced_to_stderr(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        stub_op: None,
        stub_private_key: Ed25519PrivateKey,
        stub_record_issuance_success: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.token"
        result = runner.invoke(script.app, [*_BASE_ISSUE_ARGS, "--output", str(target)])
        assert result.exit_code == 0
        assert "license_id=" in result.output


# ============================================================================
# Verify subcommand
# ============================================================================


class TestVerifySubcommand:
    def test_valid_token_returns_zero_with_payload(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
        patch_public_key: str,
    ) -> None:
        private, _ = fresh_keypair
        token = script._sign_token(
            _raw_private_bytes(private),
            {
                "license_id": "lic-verify",
                "org": "Acme",
                "tier": "enterprise",
                "features": ["sso"],
                "issued_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
                "max_tenants": 10,
            },
        )

        result = runner.invoke(script.app, ["verify", "--token", token])

        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert "Acme" in result.output

    def test_invalid_token_exits_one(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        patch_public_key: str,
    ) -> None:
        # Tampered: valid 3 segments but signature won't match the patched key.
        wrong = Ed25519PrivateKey.generate()
        token = script._sign_token(
            _raw_private_bytes(wrong),
            {
                "license_id": "x",
                "org": "x",
                "tier": "x",
                "features": [],
                "issued_at": datetime.now(UTC).isoformat(),
            },
        )

        result = runner.invoke(script.app, ["verify", "--token", token])

        assert result.exit_code == 1
        assert "INVALID" in result.output


# ============================================================================
# Decode subcommand
# ============================================================================


class TestDecodeSubcommand:
    def test_valid_token_decodes_header_and_payload(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        private, _ = fresh_keypair
        token = script._sign_token(
            _raw_private_bytes(private),
            {
                "license_id": "lic-decode",
                "org": "Acme",
                "tier": "enterprise",
                "features": ["sso"],
            },
        )

        result = runner.invoke(script.app, ["decode", "--token", token])

        assert result.exit_code == 0, result.output
        assert "header" in result.output
        assert "payload" in result.output
        assert "Acme" in result.output
        assert "EdDSA" in result.output

    def test_decode_does_not_verify_signature(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fresh_keypair: tuple[Ed25519PrivateKey, str],
    ) -> None:
        """Support-case affordance: tampered tokens still decode."""
        private, _ = fresh_keypair
        token = script._sign_token(
            _raw_private_bytes(private),
            {"license_id": "lic", "org": "Original", "tier": "enterprise", "features": []},
        )
        # Replace signature segment with an unrelated base64url-shaped string.
        header_b64, payload_b64, _sig = token.split(".")
        tampered = f"{header_b64}.{payload_b64}.AAAA"

        result = runner.invoke(script.app, ["decode", "--token", tampered])

        assert result.exit_code == 0
        assert "Original" in result.output

    def test_malformed_token_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
    ) -> None:
        result = runner.invoke(script.app, ["decode", "--token", "not-a-token"])
        assert result.exit_code == 1
        assert "three dot-separated segments" in result.output

    def test_token_with_bad_b64_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
    ) -> None:
        result = runner.invoke(script.app, ["decode", "--token", "!!!.!!!.!!!"])
        assert result.exit_code == 1
        assert "malformed" in result.output


# ============================================================================
# Pure helpers
# ============================================================================


class TestParseFeatures:
    def test_strips_whitespace_and_drops_empties(self, script: types.ModuleType) -> None:
        assert script._parse_features("a, b ,  ,c ") == ["a", "b", "c"]

    def test_single_feature(self, script: types.ModuleType) -> None:
        assert script._parse_features("multi_tenant") == ["multi_tenant"]

    def test_empty_string_yields_empty_list(self, script: types.ModuleType) -> None:
        assert script._parse_features("") == []


class TestParseExpiresAt:
    def test_none_returns_none(self, script: types.ModuleType) -> None:
        assert script._parse_expires_at(None) is None

    def test_date_only_defaults_to_utc_midnight(self, script: types.ModuleType) -> None:
        result = script._parse_expires_at("2027-05-01")
        assert result is not None
        assert result.year == 2027
        assert result.tzinfo is not None
        utcoffset = result.utcoffset()
        assert utcoffset is not None
        assert utcoffset.total_seconds() == 0

    def test_full_datetime_with_tz_preserved(self, script: types.ModuleType) -> None:
        result = script._parse_expires_at("2027-05-01T12:00:00+00:00")
        assert result is not None
        assert result.year == 2027
        assert result.hour == 12
