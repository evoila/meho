# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Credential resolution service for automated identity model.

Provides unified credential lookup with session-type-aware fallback chains
and delegation flagging for deactivated users.

Types exported:
- CredentialResolver: Main service class
- SessionType: Interactive vs automated session types
- CredentialSource: Where the credential came from (user_own, service, delegated)
- ResolvedCredential: Result dataclass with credentials + metadata
- CredentialScopeError: Raised when connector is out of scope
- CredentialNotFoundError: Raised when no credential found in the chain
- DelegationFlagCallback: Callback type for writing delegation_active flag
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Awaitable

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker
    from meho_app.modules.connectors.repositories.credential_repository import CredentialRepository

logger = get_logger(__name__)


class SessionType(Enum):
    INTERACTIVE = "interactive"
    AUTOMATED_EVENT = "automated_event"
    AUTOMATED_SCHEDULER = "automated_scheduler"


class CredentialSource(Enum):
    USER_OWN = "user_own"
    SERVICE = "service"
    DELEGATED = "delegated"


@dataclass
class ResolvedCredential:
    credentials: dict[str, str]
    source: CredentialSource
    delegated_by_user_id: str | None = None


class CredentialScopeError(Exception):
    def __init__(self, connector_id: str, allowed: list[str]):
        self.connector_id = connector_id
        self.allowed = allowed
        super().__init__(f"Connector {connector_id} not in allowed scope: {allowed}")


class CredentialNotFoundError(Exception):
    def __init__(
        self,
        connector_id: str,
        chain: list[str],
        trigger_type: str | None = None,
        trigger_id: str | None = None,
    ):
        self.connector_id = connector_id
        self.chain = chain
        self.trigger_type = trigger_type
        self.trigger_id = trigger_id
        super().__init__(
            f"No credentials found for connector {connector_id}. "
            f"Attempted sources: {', '.join(chain)}."
            + (f" Trigger: {trigger_type}:{trigger_id}" if trigger_type else "")
        )


# Callback protocol for writing delegation_active flag back to the trigger model.
# Called with (trigger_type, trigger_id, is_active) where:
#   trigger_type = "event" or "scheduler"
#   trigger_id = the UUID string of the event registration or task row
#   is_active = True to unflag (user reactivated), False to flag (user deactivated)
DelegationFlagCallback = Callable[[str, str, bool], Awaitable[None]]


class CredentialResolver:
    """
    Unified credential resolution with session-type-aware fallback chains.

    Interactive chain: user_own -> service -> CredentialNotFoundError
    Automated chain:  service -> delegated (with Keycloak check) -> CredentialNotFoundError

    Scope check (allowed_connector_ids) runs BEFORE any credential lookup.
    Delegation flagging writes delegation_active=False when user is deactivated
    and delegation_active=True when a previously-flagged user is found active again.
    """

    SENTINEL_SERVICE_USER = "__service__"

    def __init__(
        self,
        cred_repo: CredentialRepository,
        keycloak_checker: KeycloakUserChecker,
        delegation_flag_callback: DelegationFlagCallback | None = None,
    ):
        self.cred_repo = cred_repo
        self.keycloak_checker = keycloak_checker
        self._delegation_flag_callback = delegation_flag_callback

    async def resolve(
        self,
        *,
        session_type: SessionType,
        user_id: str,
        connector_id: str,
        created_by_user_id: str | None = None,
        allowed_connector_ids: list[str] | None = None,
        trigger_type: str | None = None,
        trigger_id: str | None = None,
        tenant_id: str | None = None,
        delegation_active: bool = True,
    ) -> ResolvedCredential:
        """
        Resolve credentials using the appropriate fallback chain.

        Args:
            session_type: Whether this is interactive or automated.
            user_id: Current session user_id (real user or system:event etc).
            connector_id: Which connector needs credentials.
            created_by_user_id: For automated sessions, the original creator's user_id.
            allowed_connector_ids: Connector scope (None = all allowed).
            trigger_type: "event" or "scheduler" (for audit/flagging context).
            trigger_id: UUID of the event registration/task row (for audit/flagging context).
            tenant_id: Tenant ID (Keycloak realm) for user status checks.
            delegation_active: Current delegation_active value from trigger model row.

        Returns:
            ResolvedCredential with credentials, source, and delegation metadata.

        Raises:
            CredentialScopeError: If connector_id is not in allowed_connector_ids.
            CredentialNotFoundError: If no credential found in the fallback chain.
        """
        # Pre-check: connector scope (BEFORE any credential lookup)
        if allowed_connector_ids is not None and connector_id not in allowed_connector_ids:
            raise CredentialScopeError(
                connector_id=connector_id,
                allowed=allowed_connector_ids,
            )

        if session_type == SessionType.INTERACTIVE:
            return await self._resolve_interactive(user_id, connector_id)
        else:
            return await self._resolve_automated(
                connector_id=connector_id,
                created_by_user_id=created_by_user_id,
                trigger_type=trigger_type,
                trigger_id=trigger_id,
                tenant_id=tenant_id,
                delegation_active=delegation_active,
            )

    async def _resolve_interactive(
        self, user_id: str, connector_id: str
    ) -> ResolvedCredential:
        """Interactive chain: user_own -> service -> fail."""
        # 1. User's own credential
        creds = await self.cred_repo.get_credentials(user_id, connector_id)
        if creds:
            return ResolvedCredential(credentials=creds, source=CredentialSource.USER_OWN)

        # 2. Service credential fallback
        service_creds = await self.cred_repo.get_credentials(
            self.SENTINEL_SERVICE_USER, connector_id
        )
        if service_creds:
            return ResolvedCredential(credentials=service_creds, source=CredentialSource.SERVICE)

        raise CredentialNotFoundError(connector_id=connector_id, chain=["user_own", "service"])

    async def _resolve_automated(
        self,
        *,
        connector_id: str,
        created_by_user_id: str | None,
        trigger_type: str | None,
        trigger_id: str | None,
        tenant_id: str | None,
        delegation_active: bool,
    ) -> ResolvedCredential:
        """Automated chain: service -> delegated (with Keycloak check) -> fail."""
        # 1. Service credential first for automations
        service_creds = await self.cred_repo.get_credentials(
            self.SENTINEL_SERVICE_USER, connector_id
        )
        if service_creds:
            return ResolvedCredential(credentials=service_creds, source=CredentialSource.SERVICE)

        # 2. Creator's delegated credential (with Keycloak active check)
        if created_by_user_id:
            is_active = await self.keycloak_checker.is_user_active(
                created_by_user_id, tenant_id
            )

            if not is_active:
                # User is deactivated -- flag the trigger model
                await self._call_delegation_flag(trigger_type, trigger_id, False)
                # Skip delegated credential, fall through to error
            else:
                # User is active
                if not delegation_active:
                    # Was previously flagged -- auto-unflag (self-healing)
                    await self._call_delegation_flag(trigger_type, trigger_id, True)

                creds = await self.cred_repo.get_credentials(
                    created_by_user_id, connector_id
                )
                if creds:
                    return ResolvedCredential(
                        credentials=creds,
                        source=CredentialSource.DELEGATED,
                        delegated_by_user_id=created_by_user_id,
                    )

        raise CredentialNotFoundError(
            connector_id=connector_id,
            chain=["service", "delegated"],
            trigger_type=trigger_type,
            trigger_id=trigger_id,
        )

    async def _call_delegation_flag(
        self,
        trigger_type: str | None,
        trigger_id: str | None,
        is_active: bool,
    ) -> None:
        """
        Call the delegation flag callback, wrapped in try/except.

        A failure to write the flag should not break credential resolution.
        """
        if self._delegation_flag_callback is None:
            return
        if not trigger_type or not trigger_id:
            return
        try:
            await self._delegation_flag_callback(trigger_type, trigger_id, is_active)
        except Exception:
            logger.warning(
                f"Failed to {'unflag' if is_active else 'flag'} delegation for "
                f"{trigger_type}:{trigger_id}",
                exc_info=True,
            )
