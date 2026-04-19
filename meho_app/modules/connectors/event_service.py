# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Event service layer for MEHO event infrastructure.

Provides CRUD operations for event registrations, HMAC-SHA256 signature
verification, Redis-based deduplication, rate limiting, and event logging.

Security design:
- HMAC secrets generated via secrets.token_hex(32)
- Secrets encrypted at rest via Fernet (CredentialEncryption)
- Signature verification uses hmac.compare_digest for constant-time comparison
- Tenant isolation enforced via denormalized tenant_id (never from payload)
"""

import hashlib
import hmac as hmac_module
import secrets
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.credential_encryption import CredentialEncryption
from meho_app.modules.connectors.models import (
    EventHistoryModel,
    EventRegistrationModel,
)

logger = get_logger(__name__)


class EventService:
    """
    Service layer for event registration management and security primitives.

    Handles CRUD for event registrations, HMAC-SHA256 signature verification,
    Redis-based payload deduplication, per-registration rate limiting, and event
    audit logging.

    Args:
        db: Async SQLAlchemy session for database operations.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._encryption = CredentialEncryption()

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    async def create_event_registration(
        self,
        connector_id: str,
        tenant_id: str,
        name: str,
        prompt_template: str,
        description: str | None = None,
        rate_limit_per_hour: int = 10,
        require_signature: bool = True,
        created_by_user_id: str | None = None,
        allowed_connector_ids: list[str] | None = None,
        notification_targets: list[dict[str, str]] | None = None,
        response_config: dict | None = None,
    ) -> tuple[EventRegistrationModel, str]:
        """
        Create a new event registration with an auto-generated HMAC secret.

        The plaintext secret is returned **only** at creation time (display-once
        pattern). After this call the secret is only stored in encrypted form.

        Args:
            connector_id: UUID of the parent connector.
            tenant_id: Tenant identifier (denormalized from connector).
            name: Human label (e.g. "Alertmanager Alerts").
            prompt_template: Jinja2 template with {{ payload }}.
            description: Optional longer description.
            rate_limit_per_hour: Max events per hour before throttling (default 10).
            require_signature: Whether to require HMAC signature verification.
                Set to False for systems that cannot sign events (e.g. Jira).
            created_by_user_id: JWT user_id of the creating user (Phase 74).
            allowed_connector_ids: Connector IDs this registration can access (Phase 74).
            notification_targets: Notification targets for approval alerts (Phase 75).

        Returns:
            Tuple of (saved registration model, plaintext HMAC secret).
        """
        plaintext_secret = secrets.token_hex(32)
        encrypted_secret = self._encryption.encrypt({"hmac_secret": plaintext_secret})

        registration = EventRegistrationModel(
            connector_id=connector_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            encrypted_secret=encrypted_secret,
            prompt_template=prompt_template,
            rate_limit_per_hour=rate_limit_per_hour,
            require_signature=require_signature,
            created_by_user_id=created_by_user_id,
            allowed_connector_ids=allowed_connector_ids,
            notification_targets=notification_targets,
            response_config=response_config,
        )
        self.db.add(registration)
        await self.db.flush()

        logger.info(f"Event registration created: {registration.id} for connector {connector_id}")
        return registration, plaintext_secret

    async def get_event_registration(self, registration_id: str) -> EventRegistrationModel | None:
        """
        Look up an event registration by UUID.

        Eagerly loads the connector relationship so that ``registration.connector``
        is available without a second query.

        Args:
            registration_id: UUID of the event registration.

        Returns:
            The registration model or None if not found.
        """
        stmt = (
            select(EventRegistrationModel)
            .options(selectinload(EventRegistrationModel.connector))
            .where(EventRegistrationModel.id == registration_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_event_registrations_for_connector(
        self, connector_id: str
    ) -> list[EventRegistrationModel]:
        """
        Return all event registrations for a given connector.

        Args:
            connector_id: UUID of the parent connector.

        Returns:
            List of registration models (may be empty).
        """
        stmt = (
            select(EventRegistrationModel)
            .where(EventRegistrationModel.connector_id == connector_id)
            .order_by(EventRegistrationModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def update_event_registration(
        self, registration_id: str, **kwargs: object
    ) -> EventRegistrationModel:
        """
        Update mutable fields on an event registration.

        Allowed fields: name, description, prompt_template, rate_limit_per_hour,
        is_active. The HMAC secret is **not** updatable -- rotation is achieved
        by deleting and re-creating the registration.

        Args:
            registration_id: UUID of the event registration.
            **kwargs: Field-value pairs to update.

        Returns:
            The updated registration model.

        Raises:
            ValueError: If the registration is not found or if disallowed
                fields are included.
        """
        allowed_fields = {
            "name",
            "description",
            "prompt_template",
            "rate_limit_per_hour",
            "is_active",
            "require_signature",
            "allowed_connector_ids",
            "notification_targets",
        }
        invalid = set(kwargs.keys()) - allowed_fields
        if invalid:
            raise ValueError(f"Cannot update fields: {invalid}")

        registration = await self.get_event_registration(registration_id)
        if registration is None:
            raise ValueError(f"Event registration {registration_id} not found")

        for field, value in kwargs.items():
            setattr(registration, field, value)

        await self.db.flush()
        logger.info(f"Event registration updated: {registration_id}, fields={list(kwargs.keys())}")
        return registration

    async def delete_event_registration(self, registration_id: str) -> bool:
        """
        Hard-delete an event registration and all associated history (CASCADE).

        Args:
            registration_id: UUID of the event registration.

        Returns:
            True if the registration was found and deleted, False otherwise.
        """
        registration = await self.get_event_registration(registration_id)
        if registration is None:
            return False

        await self.db.delete(registration)
        await self.db.flush()
        logger.info(f"Event registration deleted: {registration_id}")
        return True

    # ------------------------------------------------------------------ #
    # Security
    # ------------------------------------------------------------------ #

    def decrypt_secret(self, encrypted_secret: str) -> str:
        """
        Decrypt a Fernet-encrypted HMAC secret.

        Args:
            encrypted_secret: The encrypted string stored on the registration.

        Returns:
            Plaintext HMAC secret.
        """
        decrypted = self._encryption.decrypt(encrypted_secret)
        return decrypted["hmac_secret"]

    @staticmethod
    def verify_hmac_signature(raw_body: bytes, signature_header: str, secret: str) -> None:
        """
        Verify an HMAC-SHA256 signature against the raw request body.

        Uses ``hmac.compare_digest`` for constant-time comparison to prevent
        timing attacks.

        Args:
            raw_body: The raw request body bytes.
            signature_header: Hex-encoded HMAC-SHA256 from X-Event-Signature.
            secret: Plaintext HMAC secret for this registration.

        Raises:
            ValueError: If the signature header is missing or the signature
                does not match.
        """
        if not signature_header:
            raise ValueError("Missing X-Event-Signature header")

        expected = hmac_module.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac_module.compare_digest(expected, signature_header):
            raise ValueError("Invalid event signature")

    # ------------------------------------------------------------------ #
    # Dedup & Rate Limiting (Redis)
    # ------------------------------------------------------------------ #

    @staticmethod
    async def is_duplicate(
        redis_client: object,
        registration_id: str,
        payload_hash: str,
    ) -> bool:
        """
        Check whether this payload has been seen within the dedup window.

        Uses ``SET NX EX 300`` (5-minute TTL). If the key already exists the
        payload is a duplicate. On duplicate detection, increments a suppressed
        counter so event history can display "N duplicates suppressed".

        Args:
            redis_client: Async Redis client instance.
            registration_id: UUID of the event registration.
            payload_hash: SHA-256 hex digest of the raw request body.

        Returns:
            True if the payload is a duplicate, False if it is new.
        """
        dedup_key = f"meho:event:dedup:{registration_id}:{payload_hash}"
        # SET NX EX returns True on first set, None if key already exists
        was_set = await redis_client.set(dedup_key, "1", nx=True, ex=300)  # type: ignore[attr-defined]

        if was_set:
            # First time seeing this payload -- not a duplicate
            return False

        # Duplicate -- increment suppressed counter (600s TTL to outlive dedup window)
        suppressed_key = f"meho:event:suppressed:{registration_id}"
        await redis_client.incr(suppressed_key)  # type: ignore[attr-defined]
        # Ensure TTL is set (idempotent if already set)
        await redis_client.expire(suppressed_key, 600)  # type: ignore[attr-defined]

        return True

    @staticmethod
    async def is_rate_limited(
        redis_client: object,
        registration_id: str,
        limit: int,
    ) -> bool:
        """
        Check whether the registration has exceeded its per-hour rate limit.

        Uses ``INCR`` on a key scoped to ``{registration_id}:{current_hour}``.
        On the first increment the key gets a 3600s TTL so it auto-expires
        at the end of the hour window.

        Args:
            redis_client: Async Redis client instance.
            registration_id: UUID of the event registration.
            limit: Maximum events allowed per hour.

        Returns:
            True if the limit has been exceeded, False otherwise.
        """
        # Scope to the current clock-hour (e.g. 2026030619)
        hour_ts = datetime.now(tz=UTC).strftime("%Y%m%d%H")
        rate_key = f"meho:event:rate:{registration_id}:{hour_ts}"

        count = await redis_client.incr(rate_key)  # type: ignore[attr-defined]

        if count == 1:
            # First event this hour -- set TTL
            await redis_client.expire(rate_key, 3600)  # type: ignore[attr-defined]

        return count > limit  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ #
    # Event Logging
    # ------------------------------------------------------------------ #

    async def log_event(
        self,
        registration_id: str,
        tenant_id: str,
        status: str,
        payload_hash: str,
        payload_size_bytes: int,
        session_id: str | None = None,
        error_message: str | None = None,
    ) -> EventHistoryModel:
        """
        Create an event audit record and update registration counters.

        Increments ``total_events_received`` on every call. Additionally
        increments ``total_events_processed`` or ``total_events_deduplicated``
        based on the event status.

        Args:
            registration_id: UUID of the event registration.
            tenant_id: Tenant identifier.
            status: Event outcome -- "processed", "deduplicated", "rate_limited", or "failed".
            payload_hash: SHA-256 hex digest of the raw request body.
            payload_size_bytes: Size of the raw request body in bytes.
            session_id: UUID of the created session (when status is "processed").
            error_message: Error details (when status is "failed").

        Returns:
            The saved EventHistoryModel.
        """
        event = EventHistoryModel(
            event_registration_id=registration_id,
            tenant_id=tenant_id,
            status=status,
            payload_hash=payload_hash,
            payload_size_bytes=payload_size_bytes,
            session_id=session_id,
            error_message=error_message,
        )
        self.db.add(event)

        # Update registration counters atomically via UPDATE SET
        now = datetime.now(tz=UTC)
        counter_updates: dict[str, object] = {
            "total_events_received": EventRegistrationModel.total_events_received + 1,
            "last_event_at": now,
        }

        if status == "processed":
            counter_updates["total_events_processed"] = (
                EventRegistrationModel.total_events_processed + 1
            )
        elif status == "deduplicated":
            counter_updates["total_events_deduplicated"] = (
                EventRegistrationModel.total_events_deduplicated + 1
            )

        stmt = (
            update(EventRegistrationModel)
            .where(EventRegistrationModel.id == registration_id)
            .values(**counter_updates)
        )
        await self.db.execute(stmt)
        await self.db.flush()

        logger.info(
            f"Event logged: registration={registration_id} status={status} "
            f"hash={payload_hash[:12]}... size={payload_size_bytes}B"
        )
        return event
