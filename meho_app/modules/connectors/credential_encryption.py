# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Credential encryption for user-provided credentials.

Uses Fernet symmetric encryption to securely store user credentials.
"""

import json

from cryptography.fernet import Fernet

from meho_app.core.config import get_config


class CredentialEncryption:
    """Encrypt/decrypt user credentials for connectors."""

    def __init__(self) -> None:
        """Initialize with encryption key from config."""
        config = get_config()
        # Key should be in environment variable, NOT in code
        self.cipher = Fernet(config.credential_encryption_key.encode())

    def encrypt(self, credentials: dict[str, str]) -> str:
        """
        Encrypt credentials dict to string.

        Args:
            credentials: Dict with credential fields (username, password, api_key, etc.)

        Returns:
            Encrypted string
        """
        json_str = json.dumps(credentials)
        encrypted_bytes = self.cipher.encrypt(json_str.encode())
        return encrypted_bytes.decode()

    def decrypt(self, encrypted: str) -> dict[str, str]:
        """
        Decrypt credentials string to dict.

        Args:
            encrypted: Encrypted credentials string

        Returns:
            Decrypted credentials dict
        """
        decrypted_bytes = self.cipher.decrypt(encrypted.encode())
        return json.loads(decrypted_bytes.decode())  # type: ignore[no-any-return]
