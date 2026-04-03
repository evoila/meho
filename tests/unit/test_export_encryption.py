# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for password-based export encryption.
"""

import base64

import pytest

from meho_app.modules.connectors.export_encryption import (
    DecryptionError,
    PasswordBasedEncryption,
    PasswordTooShortError,
)


@pytest.fixture
def encryption() -> PasswordBasedEncryption:
    """Create encryption instance for testing."""
    return PasswordBasedEncryption()


# --- Roundtrip Tests ---


@pytest.mark.unit
def test_encrypt_decrypt_roundtrip(encryption: PasswordBasedEncryption) -> None:
    """Test that encrypt/decrypt roundtrip works."""
    original = '{"username": "alice", "password": "secret123"}'
    password = "my-secure-password"

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


@pytest.mark.unit
def test_roundtrip_with_empty_string(encryption: PasswordBasedEncryption) -> None:
    """Test that empty string encrypts/decrypts correctly."""
    original = ""
    password = "my-secure-password"

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


@pytest.mark.unit
def test_roundtrip_with_unicode(encryption: PasswordBasedEncryption) -> None:
    """Test encryption works with unicode characters."""
    original = '{"name": "日本語", "emoji": "🔐", "special": "àéïõü"}'
    password = "my-secure-password"

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


@pytest.mark.unit
def test_roundtrip_with_special_characters(encryption: PasswordBasedEncryption) -> None:
    """Test encryption works with special characters."""
    original = '{"password": "p@$$w0rd!#%&*()[]{}|\\\\;:\'\\",.<>?/"}'
    password = "my-secure-password"

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


@pytest.mark.unit
def test_roundtrip_with_large_data(encryption: PasswordBasedEncryption) -> None:
    """Test encryption works with large payloads (multi-connector exports)."""
    # Simulate a large export with many connectors
    original = '{"connectors": [' + ",".join(['{"name": "connector"}'] * 1000) + "]}"
    password = "my-secure-password"

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


# --- Password Validation Tests ---


@pytest.mark.unit
def test_encrypt_rejects_short_password(encryption: PasswordBasedEncryption) -> None:
    """Test that encrypt rejects passwords shorter than 8 characters."""
    with pytest.raises(PasswordTooShortError) as exc_info:
        encryption.encrypt("data", "short")

    assert "at least 8 characters" in str(exc_info.value)


@pytest.mark.unit
def test_decrypt_rejects_short_password(encryption: PasswordBasedEncryption) -> None:
    """Test that decrypt rejects passwords shorter than 8 characters."""
    # First encrypt with valid password
    encrypted = encryption.encrypt("data", "valid-password")

    # Then try to decrypt with short password
    with pytest.raises(PasswordTooShortError) as exc_info:
        encryption.decrypt(encrypted, "short")

    assert "at least 8 characters" in str(exc_info.value)


@pytest.mark.unit
def test_password_exactly_8_chars_works(encryption: PasswordBasedEncryption) -> None:
    """Test that exactly 8 character password is accepted."""
    original = "test data"
    password = "12345678"  # Exactly 8 chars

    encrypted = encryption.encrypt(original, password)
    decrypted = encryption.decrypt(encrypted, password)

    assert decrypted == original


# --- Wrong Password Tests ---


@pytest.mark.unit
def test_wrong_password_raises_error(encryption: PasswordBasedEncryption) -> None:
    """Test that decryption with wrong password raises DecryptionError."""
    original = "secret data"
    encrypted = encryption.encrypt(original, "correct-password")

    with pytest.raises(DecryptionError) as exc_info:
        encryption.decrypt(encrypted, "wrong-password")

    assert "Decryption failed" in str(exc_info.value)


@pytest.mark.unit
def test_similar_password_fails(encryption: PasswordBasedEncryption) -> None:
    """Test that even similar passwords fail decryption."""
    original = "secret data"
    encrypted = encryption.encrypt(original, "my-password-123")

    with pytest.raises(DecryptionError):
        encryption.decrypt(encrypted, "my-password-124")


# --- Tampering Detection Tests ---


@pytest.mark.unit
def test_tampered_ciphertext_detected(encryption: PasswordBasedEncryption) -> None:
    """Test that modified ciphertext is detected (GCM authentication)."""
    encrypted = encryption.encrypt("secret", "my-password-123")

    # Decode, modify one byte in the ciphertext area, re-encode
    blob = bytearray(base64.b64decode(encrypted))
    # Modify a byte after salt+nonce (in ciphertext area)
    blob[30] = (blob[30] + 1) % 256
    tampered = base64.b64encode(bytes(blob)).decode()

    with pytest.raises(DecryptionError):
        encryption.decrypt(tampered, "my-password-123")


@pytest.mark.unit
def test_tampered_salt_detected(encryption: PasswordBasedEncryption) -> None:
    """Test that modified salt is detected (causes wrong key derivation)."""
    encrypted = encryption.encrypt("secret", "my-password-123")

    # Modify first byte (in salt area)
    blob = bytearray(base64.b64decode(encrypted))
    blob[0] = (blob[0] + 1) % 256
    tampered = base64.b64encode(bytes(blob)).decode()

    with pytest.raises(DecryptionError):
        encryption.decrypt(tampered, "my-password-123")


@pytest.mark.unit
def test_invalid_base64_raises_error(encryption: PasswordBasedEncryption) -> None:
    """Test that invalid base64 input raises DecryptionError."""
    with pytest.raises(DecryptionError):
        encryption.decrypt("not-valid-base64!!!", "my-password-123")


@pytest.mark.unit
def test_truncated_data_raises_error(encryption: PasswordBasedEncryption) -> None:
    """Test that truncated encrypted data raises DecryptionError."""
    encrypted = encryption.encrypt("secret", "my-password-123")

    # Truncate to just a few bytes
    truncated = encrypted[:20]

    with pytest.raises(DecryptionError):
        encryption.decrypt(truncated, "my-password-123")


# --- Output Uniqueness Tests ---


@pytest.mark.unit
def test_same_data_different_passwords_different_output(
    encryption: PasswordBasedEncryption,
) -> None:
    """Test that same data with different passwords produces different output."""
    data = "same data"

    encrypted1 = encryption.encrypt(data, "password-one")
    encrypted2 = encryption.encrypt(data, "password-two")

    assert encrypted1 != encrypted2


@pytest.mark.unit
def test_salt_uniqueness(encryption: PasswordBasedEncryption) -> None:
    """Test that same data encrypted twice produces different output (random salt)."""
    data = "same data"
    password = "same-password"

    encrypted1 = encryption.encrypt(data, password)
    encrypted2 = encryption.encrypt(data, password)

    # Different encryptions should produce different output due to random salt/nonce
    assert encrypted1 != encrypted2

    # But both should decrypt to same data
    assert encryption.decrypt(encrypted1, password) == data
    assert encryption.decrypt(encrypted2, password) == data


@pytest.mark.unit
def test_encrypted_data_is_not_plaintext(encryption: PasswordBasedEncryption) -> None:
    """Test that encrypted data doesn't contain plaintext."""
    data = '{"username": "alice", "password": "secret123"}'
    password = "my-password-123"

    encrypted = encryption.encrypt(data, password)

    # Encrypted string should not contain plaintext
    assert "alice" not in encrypted
    assert "secret123" not in encrypted
    assert "username" not in encrypted


# --- Format Tests ---


@pytest.mark.unit
def test_output_is_valid_base64(encryption: PasswordBasedEncryption) -> None:
    """Test that encrypted output is valid base64."""
    encrypted = encryption.encrypt("test", "my-password-123")

    # Should not raise
    decoded = base64.b64decode(encrypted)

    # Should contain at least salt + nonce + some ciphertext
    assert len(decoded) >= encryption.SALT_SIZE + encryption.NONCE_SIZE + 1


@pytest.mark.unit
def test_output_contains_expected_components(
    encryption: PasswordBasedEncryption,
) -> None:
    """Test that encrypted output has expected structure."""
    data = "test data"
    encrypted = encryption.encrypt(data, "my-password-123")

    decoded = base64.b64decode(encrypted)

    # Minimum size: salt (16) + nonce (12) + ciphertext (at least data length) + tag (16)
    min_size = encryption.SALT_SIZE + encryption.NONCE_SIZE + len(data) + 16
    assert len(decoded) >= min_size


# --- Constants Tests ---


@pytest.mark.unit
def test_encryption_constants() -> None:
    """Test that encryption uses expected security parameters."""
    enc = PasswordBasedEncryption()

    assert enc.SALT_SIZE == 16  # 128 bits
    assert enc.NONCE_SIZE == 12  # 96 bits (GCM standard)
    assert enc.KEY_SIZE == 32  # 256 bits (AES-256)
    assert enc.ITERATIONS == 100_000  # Strong PBKDF2
    assert enc.MIN_PASSWORD_LENGTH == 8
