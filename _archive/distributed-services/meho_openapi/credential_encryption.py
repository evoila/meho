"""
Credential encryption for user-provided credentials.

Uses Fernet symmetric encryption to securely store user credentials.
"""
from cryptography.fernet import Fernet
from meho_core.config import get_config
import json
from typing import Dict


class CredentialEncryption:
    """Encrypt/decrypt user credentials for RBAC systems"""
    
    def __init__(self) -> None:
        """Initialize with encryption key from config"""
        config = get_config()
        # Key should be in environment variable, NOT in code
        self.cipher = Fernet(config.credential_encryption_key.encode())
    
    def encrypt(self, credentials: Dict[str, str]) -> str:
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
    
    def decrypt(self, encrypted: str) -> Dict[str, str]:
        """
        Decrypt credentials string to dict.
        
        Args:
            encrypted: Encrypted credentials string
        
        Returns:
            Decrypted credentials dict
        """
        decrypted_bytes = self.cipher.decrypt(encrypted.encode())
        return json.loads(decrypted_bytes.decode())  # type: ignore[no-any-return]

