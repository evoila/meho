# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for ``scripts/generate-license-keypair.py`` (#521).

Loads the hyphenated script via ``importlib.util`` because its filename is not
a legal Python module name. Mocks ``google.cloud.secretmanager`` via
``sys.modules`` injection — the SDK is not a project dependency and tests must
not require it.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Iterator

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "generate-license-keypair.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("generate_license_keypair", SCRIPT_PATH)
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


@pytest.fixture
def fake_secretmanager() -> Iterator[MagicMock]:
    """Inject a fake ``google.cloud.secretmanager`` module for vault-write tests."""
    fake_module = MagicMock()
    fake_client = MagicMock()
    fake_module.SecretManagerServiceClient.return_value = fake_client
    fake_google = types.ModuleType("google")
    fake_cloud = types.ModuleType("google.cloud")
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.cloud": fake_cloud,
            "google.cloud.secretmanager": fake_module,
        },
    ):
        yield fake_module


class TestArgumentValidation:
    def test_no_flag_errors_with_clear_message(
        self, runner: CliRunner, script: types.ModuleType
    ) -> None:
        result = runner.invoke(script.app, [])
        assert result.exit_code == 1
        assert "choose exactly one" in result.output

    def test_two_flags_errors(
        self, runner: CliRunner, script: types.ModuleType, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            script.app,
            ["--output-private", str(tmp_path / "k"), "--unsafe-stdout"],
        )
        assert result.exit_code == 1
        assert "choose only one" in result.output

    def test_three_flags_errors(
        self, runner: CliRunner, script: types.ModuleType, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            script.app,
            [
                "--vault-write",
                "projects/p/secrets/s",
                "--output-private",
                str(tmp_path / "k"),
                "--unsafe-stdout",
            ],
        )
        assert result.exit_code == 1
        assert "choose only one" in result.output


class TestUnsafeStdout:
    def test_unsafe_stdout_emits_warning_and_keys(
        self, runner: CliRunner, script: types.ModuleType
    ) -> None:
        result = runner.invoke(script.app, ["--unsafe-stdout"])
        assert result.exit_code == 0
        assert "WARNING: --unsafe-stdout was used" in result.output
        assert "Public key" in result.output
        assert "Private key" in result.output


class TestOutputPrivateFile:
    def test_writes_file_with_mode_0600(
        self, runner: CliRunner, script: types.ModuleType, tmp_path: Path
    ) -> None:
        target = tmp_path / "private.key"
        result = runner.invoke(script.app, ["--output-private", str(target)])
        assert result.exit_code == 0, result.output
        assert target.exists()
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"
        assert "Public key" in result.output
        # Sanity: file content is non-empty base64url (no padding).
        content = target.read_text(encoding="utf-8").strip()
        assert content
        assert "=" not in content
        # PRIMARY SAFETY CONTRACT: private key file content MUST NOT appear on stdout.
        assert content not in result.output
        assert "Private key (KEEP SECRET" not in result.output

    def test_refuses_to_overwrite_existing_file(
        self, runner: CliRunner, script: types.ModuleType, tmp_path: Path
    ) -> None:
        target = tmp_path / "private.key"
        target.write_text("preexisting", encoding="utf-8")
        result = runner.invoke(script.app, ["--output-private", str(target)])
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output
        assert target.read_text(encoding="utf-8") == "preexisting"


class TestVaultWrite:
    def test_full_path_calls_sdk_with_correct_request(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
    ) -> None:
        parent = "projects/proj-123/secrets/meho-license-private-key"
        result = runner.invoke(script.app, ["--vault-write", parent])
        assert result.exit_code == 0, result.output

        client = fake_secretmanager.SecretManagerServiceClient.return_value
        # Pre-flight verify_vault_target → get_secret called first.
        client.get_secret.assert_called_once_with(name=parent)
        client.add_secret_version.assert_called_once()
        request = client.add_secret_version.call_args.kwargs["request"]
        assert request["parent"] == parent
        assert isinstance(request["payload"]["data"], bytes)
        private_b64 = request["payload"]["data"].decode("utf-8")
        assert private_b64  # non-empty
        # Output: public key + vault path appear; PRIVATE KEY NEVER ON STDOUT.
        assert "Public key" in result.output
        assert parent in result.output
        assert private_b64 not in result.output
        assert "Private key (KEEP SECRET" not in result.output

    def test_bare_name_uses_google_cloud_project_env(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-from-env")
        result = runner.invoke(script.app, ["--vault-write", "my-secret"])
        assert result.exit_code == 0, result.output

        client = fake_secretmanager.SecretManagerServiceClient.return_value
        request = client.add_secret_version.call_args.kwargs["request"]
        assert request["parent"] == "projects/proj-from-env/secrets/my-secret"
        # Same private-key-not-on-stdout assertion.
        private_b64 = request["payload"]["data"].decode("utf-8")
        assert private_b64 not in result.output

    def test_bare_name_without_project_env_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        result = runner.invoke(script.app, ["--vault-write", "my-secret"])
        assert result.exit_code == 1
        assert "GOOGLE_CLOUD_PROJECT" in result.output

    def test_malformed_path_errors(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
    ) -> None:
        result = runner.invoke(script.app, ["--vault-write", "not/a/valid/path"])
        assert result.exit_code == 1
        assert "expects either a bare secret name" in result.output

    @pytest.mark.parametrize(
        "bad_path",
        [
            "projects/p/secrets/s/versions/latest",
            "projects/p/secrets/s/versions/3",
            "projects/p/secrets/s/extra/garbage",
            "projects//secrets/s",
            "projects/p/secrets/",
            "projects/p/secrets",
            "",
        ],
    )
    def test_strict_validator_rejects_invalid_full_paths(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
        bad_path: str,
    ) -> None:
        result = runner.invoke(script.app, ["--vault-write", bad_path])
        assert result.exit_code != 0, (
            f"path {bad_path!r} should have been rejected; got exit "
            f"{result.exit_code}, output: {result.output}"
        )
        # SDK should NOT be reached when validation rejects the path.
        client = fake_secretmanager.SecretManagerServiceClient.return_value
        client.get_secret.assert_not_called()
        client.add_secret_version.assert_not_called()

    def test_pre_flight_blocks_when_secret_not_found_and_does_not_generate_key(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
    ) -> None:
        not_found_class = type("NotFound", (Exception,), {})
        client = fake_secretmanager.SecretManagerServiceClient.return_value
        client.get_secret.side_effect = not_found_class("secret missing")

        result = runner.invoke(script.app, ["--vault-write", "projects/p/secrets/missing"])
        assert result.exit_code == 1
        assert "NotFound" in result.output
        assert "gcloud secrets create" in result.output
        # PRIMARY SAFETY: keypair MUST NOT have been generated and discarded.
        client.add_secret_version.assert_not_called()

    def test_pre_flight_blocks_on_generic_api_error(
        self,
        runner: CliRunner,
        script: types.ModuleType,
        fake_secretmanager: MagicMock,
    ) -> None:
        permission_class = type("PermissionDenied", (Exception,), {})
        client = fake_secretmanager.SecretManagerServiceClient.return_value
        client.get_secret.side_effect = permission_class("nope")

        result = runner.invoke(script.app, ["--vault-write", "projects/p/secrets/s"])
        assert result.exit_code == 1
        assert "PermissionDenied" in result.output
        client.add_secret_version.assert_not_called()

    def test_missing_sdk_emits_install_hint(
        self, runner: CliRunner, script: types.ModuleType
    ) -> None:
        # Force ImportError by setting the module to None in sys.modules.
        with patch.dict(
            sys.modules,
            {
                "google.cloud.secretmanager": None,
            },
        ):
            result = runner.invoke(script.app, ["--vault-write", "projects/p/secrets/s"])
        assert result.exit_code == 1
        assert "google-cloud-secret-manager" in result.output


class TestPureKeypairGeneration:
    def test_generate_keypair_returns_named_tuple_with_distinct_b64url_halves(
        self, script: types.ModuleType
    ) -> None:
        kp = script.generate_keypair()
        # Named-field access prevents positional swap bugs.
        assert isinstance(kp.private_b64, str)
        assert isinstance(kp.public_b64, str)
        assert kp.private_b64 != kp.public_b64
        # base64url, no padding
        assert "=" not in kp.private_b64
        assert "=" not in kp.public_b64
        # 32-byte raw Ed25519 keys → 43 base64url chars (no padding)
        assert len(kp.private_b64) == 43
        assert len(kp.public_b64) == 43
        # Backwards-compatible positional unpacking still works.
        priv, pub = kp
        assert priv == kp.private_b64
        assert pub == kp.public_b64
