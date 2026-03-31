"""Tests for Fernet credential manager with key auto-generation."""

import os
import stat

import pytest
from pathlib import Path

from cryptography.fernet import Fernet


@pytest.fixture
def cred_state_dir(tmp_path: Path) -> Path:
    """Create a minimal state directory for credential testing."""
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    (state_dir / "credentials").mkdir()
    return state_dir


class TestKeyAutoGeneration:
    """Test encryption key creation and loading."""

    def test_auto_generates_key_file_on_first_init(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        key_file = cred_state_dir / ".key"
        assert not key_file.exists()

        CredentialManager(cred_state_dir)
        assert key_file.exists()

    def test_key_file_has_600_permissions(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        CredentialManager(cred_state_dir)
        key_file = cred_state_dir / ".key"
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_loads_existing_key_on_subsequent_init(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm1 = CredentialManager(cred_state_dir)
        cm1.store("test", {"api_key": "secret"})

        # Second init should load the same key and decrypt successfully
        cm2 = CredentialManager(cred_state_dir)
        creds = cm2.retrieve("test")
        assert creds is not None
        assert creds["api_key"] == "secret"

    def test_meho_key_env_var_overrides_file(self, cred_state_dir, monkeypatch):
        from meho_claude.core.credentials import CredentialManager

        # Generate a key to use as env var
        env_key = Fernet.generate_key().decode()
        monkeypatch.setenv("MEHO_KEY", env_key)

        cm = CredentialManager(cred_state_dir)
        cm.store("test", {"password": "hunter2"})

        # Verify the same env key can decrypt
        cm2 = CredentialManager(cred_state_dir)
        creds = cm2.retrieve("test")
        assert creds is not None
        assert creds["password"] == "hunter2"

    def test_meho_key_env_var_takes_priority_over_file(self, cred_state_dir, monkeypatch):
        from meho_claude.core.credentials import CredentialManager

        # Create a manager with auto-generated file key
        cm_file = CredentialManager(cred_state_dir)
        cm_file.store("file_creds", {"key": "from_file"})

        # Now set env var with different key
        env_key = Fernet.generate_key().decode()
        monkeypatch.setenv("MEHO_KEY", env_key)

        # New manager uses env key, cannot decrypt file-key data
        cm_env = CredentialManager(cred_state_dir)
        assert cm_env.retrieve("file_creds") is None  # Wrong key


class TestStoreRetrieve:
    """Test credential storage and retrieval."""

    def test_store_and_retrieve_roundtrip(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        original = {"api_key": "secret123", "password": "hunter2"}
        cm.store("kubernetes-prod", original)

        retrieved = cm.retrieve("kubernetes-prod")
        assert retrieved == original

    def test_retrieve_nonexistent_returns_none(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        assert cm.retrieve("does-not-exist") is None

    def test_retrieve_with_wrong_key_returns_none(self, cred_state_dir, tmp_path):
        from meho_claude.core.credentials import CredentialManager

        # Store with one key
        cm1 = CredentialManager(cred_state_dir)
        cm1.store("test", {"secret": "data"})

        # Create a different state dir with different key, copy enc file
        other_dir = tmp_path / ".meho-other"
        other_dir.mkdir()
        (other_dir / "credentials").mkdir()
        cm2 = CredentialManager(other_dir)

        # Copy the encrypted file to the other dir
        import shutil
        src_file = cred_state_dir / "credentials" / "test.enc"
        dst_file = other_dir / "credentials" / "test.enc"
        shutil.copy2(src_file, dst_file)

        # Retrieve with wrong key should return None, not crash
        result = cm2.retrieve("test")
        assert result is None

    def test_enc_file_has_600_permissions(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("test", {"key": "value"})
        enc_file = cred_state_dir / "credentials" / "test.enc"
        mode = stat.S_IMODE(enc_file.stat().st_mode)
        assert mode == 0o600

    def test_various_credential_types_roundtrip(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        complex_creds = {
            "string_val": "hello",
            "int_val": 42,
            "float_val": 3.14,
            "bool_val": True,
            "null_val": None,
            "nested": {"inner_key": "inner_val", "deep": {"level": 3}},
            "list_val": [1, "two", 3.0],
        }
        cm.store("complex", complex_creds)
        retrieved = cm.retrieve("complex")
        assert retrieved == complex_creds

    def test_store_overwrites_existing(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("test", {"version": 1})
        cm.store("test", {"version": 2})
        retrieved = cm.retrieve("test")
        assert retrieved["version"] == 2


class TestDelete:
    """Test credential deletion."""

    def test_delete_existing_returns_true(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("deleteme", {"key": "value"})
        assert cm.delete("deleteme") is True
        assert not (cred_state_dir / "credentials" / "deleteme.enc").exists()

    def test_delete_nonexistent_returns_false(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        assert cm.delete("nope") is False

    def test_retrieve_after_delete_returns_none(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("temp", {"key": "value"})
        cm.delete("temp")
        assert cm.retrieve("temp") is None


class TestListConnectors:
    """Test listing stored connector names."""

    def test_list_empty(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        assert cm.list_connectors() == []

    def test_list_returns_stored_names(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("alpha", {"key": "a"})
        cm.store("beta", {"key": "b"})
        cm.store("gamma", {"key": "c"})
        connectors = cm.list_connectors()
        assert sorted(connectors) == ["alpha", "beta", "gamma"]

    def test_list_excludes_deleted(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("keep", {"key": "k"})
        cm.store("remove", {"key": "r"})
        cm.delete("remove")
        assert cm.list_connectors() == ["keep"]


class TestCredentialManagerRepr:
    """Test that credentials never leak via str/repr."""

    def test_str_does_not_contain_credentials(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("test", {"api_key": "super_secret_key_12345"})
        s = str(cm)
        assert "super_secret_key_12345" not in s

    def test_repr_does_not_contain_credentials(self, cred_state_dir):
        from meho_claude.core.credentials import CredentialManager

        cm = CredentialManager(cred_state_dir)
        cm.store("test", {"api_key": "super_secret_key_12345"})
        r = repr(cm)
        assert "super_secret_key_12345" not in r


class TestCredentialsDirAutoCreation:
    """Test that credentials directory is auto-created if missing."""

    def test_creates_credentials_dir_if_missing(self, tmp_path):
        from meho_claude.core.credentials import CredentialManager

        state_dir = tmp_path / ".meho"
        state_dir.mkdir()
        # Do NOT create credentials subdir
        cm = CredentialManager(state_dir)
        assert (state_dir / "credentials").exists()
        cm.store("test", {"key": "val"})
        assert cm.retrieve("test") == {"key": "val"}
