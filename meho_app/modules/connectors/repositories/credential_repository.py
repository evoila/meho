# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for user-specific connector credentials.

Handles storage and retrieval of encrypted user credentials.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.credential_encryption import CredentialEncryption
from meho_app.modules.connectors.models import UserCredentialModel
from meho_app.modules.connectors.schemas import UserCredentialProvide


class CredentialRepository:
    """Repository for user connector credentials."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.encryption = CredentialEncryption()

    async def store_credentials(self, user_id: str, credential: UserCredentialProvide) -> None:
        """
        Store or update user's credentials for a connector.

        Args:
            user_id: User identifier
            credential: Credential data to store
        """
        # Encrypt credentials
        encrypted = self.encryption.encrypt(credential.credentials)

        # Check if credentials already exist
        existing = await self._get_credential_record(user_id, credential.connector_id)

        if existing:
            # Update existing
            existing.encrypted_credentials = encrypted  # type: ignore[assignment]
            existing.credential_type = credential.credential_type  # type: ignore[assignment]
            existing.updated_at = datetime.now(tz=UTC)  # type: ignore[assignment]
            existing.is_active = True  # type: ignore[assignment]
        else:
            # Create new
            new_cred = UserCredentialModel(
                id=uuid.uuid4(),
                user_id=user_id,
                connector_id=uuid.UUID(credential.connector_id),
                credential_type=credential.credential_type,
                encrypted_credentials=encrypted,
            )
            self.session.add(new_cred)

        await self.session.flush()

    async def get_credentials(self, user_id: str, connector_id: str) -> dict[str, str] | None:
        """
        Get decrypted credentials for user-connector pair.

        Args:
            user_id: User identifier
            connector_id: Connector identifier

        Returns:
            Decrypted credentials dict or None if not found
        """
        record = await self._get_credential_record(user_id, connector_id)

        if not record or not record.is_active:
            return None

        # Update last_used_at
        record.last_used_at = datetime.now(tz=UTC)  # type: ignore[assignment]

        # Decrypt and return
        return self.encryption.decrypt(record.encrypted_credentials)  # type: ignore[arg-type]

    async def delete_credentials(self, user_id: str, connector_id: str) -> bool:
        """
        Delete user's credentials for a connector.

        Returns:
            True if deleted, False if not found
        """
        record = await self._get_credential_record(user_id, connector_id)

        if not record:
            return False

        await self.session.delete(record)
        await self.session.flush()
        return True

    async def get_session_state(self, user_id: str, connector_id: str) -> dict[str, Any] | None:
        """
        Get session state for SESSION auth.

        Returns:
            {
                "session_token": str,
                "session_expires_at": datetime,
                "session_state": str  # LOGGED_IN, LOGGED_OUT, EXPIRED
            }
        """
        record = await self._get_credential_record(user_id, connector_id)

        if not record or not record.is_active:
            return None

        if not record.session_token:
            return {
                "session_token": None,
                "session_expires_at": None,
                "session_state": "LOGGED_OUT",
            }

        # Decrypt session token
        decrypted_token = (
            self.encryption.decrypt(record.session_token) if record.session_token else None
        )  # type: ignore[arg-type]

        # Decrypt refresh token if exists
        decrypted_refresh = (
            self.encryption.decrypt(record.session_refresh_token)
            if record.session_refresh_token
            else None
        )  # type: ignore[arg-type]

        return {
            "session_token": decrypted_token.get("token") if decrypted_token else None,
            "session_expires_at": record.session_token_expires_at,
            "refresh_token": decrypted_refresh.get("token") if decrypted_refresh else None,
            "refresh_expires_at": record.session_refresh_expires_at,
            "session_state": record.session_state or "LOGGED_OUT",
        }

    async def update_session_state(
        self,
        user_id: str,
        connector_id: str,
        session_token: str,
        session_expires_at: datetime,
        session_state: str,
        refresh_token: str | None = None,
        refresh_expires_at: datetime | None = None,
    ) -> None:
        """
        Update session state for SESSION auth.

        Args:
            user_id: User identifier
            connector_id: Connector identifier
            session_token: New session token
            session_expires_at: Session expiry time
            session_state: Session state (LOGGED_IN, LOGGED_OUT, EXPIRED)
            refresh_token: New refresh token (optional)
            refresh_expires_at: Refresh token expiry time (optional)
        """
        record = await self._get_credential_record(user_id, connector_id)

        if not record:
            raise ValueError(
                f"No credentials found for user {user_id} and connector {connector_id}"
            )

        # Encrypt session token
        encrypted_token = self.encryption.encrypt({"token": session_token})

        # Update session fields
        record.session_token = encrypted_token  # type: ignore[assignment]
        record.session_token_expires_at = session_expires_at  # type: ignore[assignment]
        record.session_state = session_state  # type: ignore[assignment]
        record.updated_at = datetime.now(tz=UTC)  # type: ignore[assignment]

        # Update refresh token if provided
        if refresh_token:
            encrypted_refresh = self.encryption.encrypt({"token": refresh_token})
            record.session_refresh_token = encrypted_refresh  # type: ignore[assignment]
            record.session_refresh_expires_at = refresh_expires_at  # type: ignore[assignment]

        await self.session.flush()

    async def get_credential_age_days(
        self,
        user_id: str,
        connector_id: str,
    ) -> int | None:
        """
        Get the age of credentials in days since last update.

        Args:
            user_id: User identifier
            connector_id: Connector identifier

        Returns:
            Number of days since credentials were last updated, or None
            if no active credentials exist.
        """
        record = await self._get_credential_record(user_id, connector_id)
        if not record or not record.is_active:
            return None

        updated_at = record.updated_at
        if updated_at is None:
            return None

        return (datetime.now(tz=UTC) - updated_at).days

    async def _get_credential_record(
        self, user_id: str, connector_id: str
    ) -> UserCredentialModel | None:
        """Get credential record from database."""
        try:
            query = select(UserCredentialModel).where(
                UserCredentialModel.user_id == user_id,
                UserCredentialModel.connector_id == uuid.UUID(connector_id),
            )
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
        except ValueError:
            return None


# Backward compatibility alias
UserCredentialRepository = CredentialRepository
