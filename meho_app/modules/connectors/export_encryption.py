# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Password-based encryption for connector export/import.

Uses AES-256-GCM with PBKDF2 key derivation for secure, portable encrypted files.
Unlike credential_encryption.py (which uses server-side Fernet keys), this module
uses user-provided passwords so exported files can be decrypted on any system.
"""

import base64
import os
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class PasswordTooShortError(ValueError):
    """Raised when password doesn't meet minimum length requirement."""

    pass


class DecryptionError(ValueError):
    """Raised when decryption fails (wrong password or tampered data)."""

    pass


class PasswordBasedEncryption:
    """
    Encrypt/decrypt data using a user-provided password.

    Uses AES-256-GCM for authenticated encryption with PBKDF2 for key derivation.
    The encrypted output includes salt and nonce for self-contained decryption.

    Binary format: | Salt (16 bytes) | Nonce (12 bytes) | Ciphertext + Auth Tag |
    Output: Base64-encoded string

    Example:
        encryption = PasswordBasedEncryption()
        encrypted = encryption.encrypt('{"secret": "data"}', "my-password")
        decrypted = encryption.decrypt(encrypted, "my-password")
    """

    SALT_SIZE: Final[int] = 16  # 128 bits
    NONCE_SIZE: Final[int] = 12  # 96 bits (GCM standard)
    KEY_SIZE: Final[int] = 32  # 256 bits
    ITERATIONS: Final[int] = 100_000  # PBKDF2 iterations
    MIN_PASSWORD_LENGTH: Final[int] = 8

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        """
        Derive an AES-256 key from password using PBKDF2.

        Args:
            password: User-provided password
            salt: Random salt (must be same for encrypt/decrypt)

        Returns:
            32-byte key suitable for AES-256
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_SIZE,
            salt=salt,
            iterations=self.ITERATIONS,
        )
        return kdf.derive(password.encode("utf-8"))

    def _validate_password(self, password: str) -> None:
        """
        Validate password meets minimum requirements.

        Args:
            password: Password to validate

        Raises:
            PasswordTooShortError: If password is too short
        """
        if len(password) < self.MIN_PASSWORD_LENGTH:
            raise PasswordTooShortError(
                f"Password must be at least {self.MIN_PASSWORD_LENGTH} characters"
            )

    def encrypt(self, data: str, password: str) -> str:
        """
        Encrypt data with a password.

        Args:
            data: String data to encrypt (typically JSON)
            password: User-provided password (minimum 8 characters)

        Returns:
            Base64-encoded string containing salt + nonce + ciphertext + auth tag

        Raises:
            PasswordTooShortError: If password is too short
        """
        self._validate_password(password)

        # Generate random salt and nonce
        salt = os.urandom(self.SALT_SIZE)
        nonce = os.urandom(self.NONCE_SIZE)

        # Derive key from password
        key = self._derive_key(password, salt)

        # Encrypt with AES-256-GCM (includes authentication tag)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, data.encode("utf-8"), None)

        # Combine: salt + nonce + ciphertext (includes auth tag)
        encrypted_blob = salt + nonce + ciphertext

        # Return as base64 string
        return base64.b64encode(encrypted_blob).decode("utf-8")

    def decrypt(self, encrypted: str, password: str) -> str:
        """
        Decrypt data with a password.

        Args:
            encrypted: Base64-encoded encrypted string from encrypt()
            password: Password used during encryption

        Returns:
            Original decrypted string

        Raises:
            PasswordTooShortError: If password is too short
            DecryptionError: If decryption fails (wrong password or tampered data)
        """
        self._validate_password(password)

        try:
            # Decode base64
            encrypted_blob = base64.b64decode(encrypted.encode("utf-8"))

            # Extract components
            salt = encrypted_blob[: self.SALT_SIZE]
            nonce = encrypted_blob[self.SALT_SIZE : self.SALT_SIZE + self.NONCE_SIZE]
            ciphertext = encrypted_blob[self.SALT_SIZE + self.NONCE_SIZE :]

            # Derive key from password using same salt
            key = self._derive_key(password, salt)

            # Decrypt with AES-256-GCM (validates auth tag)
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)

            return plaintext.decode("utf-8")

        except Exception as e:
            # Wrap all decryption failures in DecryptionError
            # This prevents leaking information about why decryption failed
            raise DecryptionError(
                "Decryption failed. Check password and ensure data is not corrupted."
            ) from e
