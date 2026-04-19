# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for credential encryption.
"""

import pytest
from cryptography.fernet import InvalidToken

from meho_app.core.config import reset_config
from meho_app.modules.connectors.credential_encryption import CredentialEncryption


@pytest.fixture
def encryption(monkeypatch):
    """Create encryption instance with test key"""
    from cryptography.fernet import Fernet

    # Generate a proper Fernet key (URL-safe base64-encoded 32 bytes)
    test_fernet_key = Fernet.generate_key().decode()

    # Set all required config
    for key, value in {
        "DATABASE_URL": "postgresql://test",
        "VECTOR_DB_URL": "http://test",
        "OBJECT_STORAGE_ENDPOINT": "localhost",
        "OBJECT_STORAGE_BUCKET": "test",
        "OBJECT_STORAGE_ACCESS_KEY": "key",
        "OBJECT_STORAGE_SECRET_KEY": "secret",
        "REDIS_URL": "redis://localhost",
        "MESSAGE_BROKER_URL": "amqp://localhost",
        "OPENAI_API_KEY": "sk-test",
        "CREDENTIAL_ENCRYPTION_KEY": test_fernet_key,  # Proper Fernet key
    }.items():
        monkeypatch.setenv(key, value)

    reset_config()

    # Return encryption instance
    enc = CredentialEncryption()
    yield enc

    # Cleanup
    reset_config()


@pytest.mark.unit
def test_encrypt_decrypt_roundtrip(encryption):
    """Test that encrypt/decrypt roundtrip works"""
    original = {"username": "alice", "password": "secret123"}

    encrypted = encryption.encrypt(original)
    decrypted = encryption.decrypt(encrypted)

    assert decrypted == original


@pytest.mark.unit
def test_encrypted_data_is_not_readable(encryption):
    """Test that encrypted data doesn't contain plaintext"""
    credentials = {"username": "alice", "password": "secret123"}

    encrypted = encryption.encrypt(credentials)

    # Encrypted string should not contain plaintext
    assert "alice" not in encrypted
    assert "secret123" not in encrypted
    assert "username" not in encrypted


@pytest.mark.unit
def test_decrypt_invalid_data_raises_error(encryption):
    """Test that decrypting invalid data raises error"""
    with pytest.raises((InvalidToken, Exception)):
        encryption.decrypt("not-encrypted-data")


@pytest.mark.unit
def test_encrypt_different_data_produces_different_output(encryption):
    """Test that different credentials produce different encrypted output"""
    cred1 = {"username": "alice", "password": "pass1"}
    cred2 = {"username": "bob", "password": "pass2"}

    encrypted1 = encryption.encrypt(cred1)
    encrypted2 = encryption.encrypt(cred2)

    assert encrypted1 != encrypted2


@pytest.mark.unit
def test_encrypt_handles_special_characters(encryption):
    """Test encryption works with special characters"""
    credentials = {
        "username": "user@domain.com",
        "password": "p@$$w0rd!#%&",
        "api_key": "sk-proj-xyz_ABC-123",
    }

    encrypted = encryption.encrypt(credentials)
    decrypted = encryption.decrypt(encrypted)

    assert decrypted == credentials
