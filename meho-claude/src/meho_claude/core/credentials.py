"""Fernet-encrypted credential storage in ~/.meho/credentials/.

Auto-generates encryption key on first use. MEHO_KEY env var overrides
the file-based key for portability between machines and CI.

Credentials are stored as individual .enc files per connector.
File permissions are 0o600 (owner-only read/write).
"""

import os
from pathlib import Path

import msgspec
from cryptography.fernet import Fernet, InvalidToken


class CredentialManager:
    """Manages Fernet-encrypted credential files in ~/.meho/credentials/."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._creds_dir = state_dir / "credentials"
        self._creds_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        """Load encryption key from MEHO_KEY env var or ~/.meho/.key file."""
        env_key = os.environ.get("MEHO_KEY")
        if env_key:
            return env_key.encode()

        key_file = self._state_dir / ".key"
        if key_file.exists():
            return key_file.read_bytes().strip()

        # First run: generate and store key
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        key_file.chmod(0o600)
        return key

    def store(self, connector_name: str, credentials: dict) -> None:
        """Encrypt and store credentials for a connector."""
        plaintext = msgspec.json.encode(credentials)
        encrypted = self._fernet.encrypt(plaintext)
        cred_file = self._creds_dir / f"{connector_name}.enc"
        cred_file.write_bytes(encrypted)
        cred_file.chmod(0o600)

    def retrieve(self, connector_name: str) -> dict | None:
        """Decrypt and return credentials for a connector.

        Returns None if the file doesn't exist or if decryption fails
        (wrong key or corrupted file). Never raises on bad decrypt.
        """
        cred_file = self._creds_dir / f"{connector_name}.enc"
        if not cred_file.exists():
            return None
        try:
            encrypted = cred_file.read_bytes()
            plaintext = self._fernet.decrypt(encrypted)
            return msgspec.json.decode(plaintext)
        except InvalidToken:
            return None

    def delete(self, connector_name: str) -> bool:
        """Remove stored credentials for a connector."""
        cred_file = self._creds_dir / f"{connector_name}.enc"
        if cred_file.exists():
            cred_file.unlink()
            return True
        return False

    def list_connectors(self) -> list[str]:
        """List connector names that have stored credentials."""
        return sorted(f.stem for f in self._creds_dir.glob("*.enc"))

    def __str__(self) -> str:
        return f"CredentialManager(state_dir={self._state_dir})"

    def __repr__(self) -> str:
        return f"CredentialManager(state_dir={self._state_dir!r})"
