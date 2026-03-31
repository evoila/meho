# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Notification dispatcher for automated session approval alerts.

Fire-and-forget: failures are logged but never block the approval flow.
Uses the email connector's send_email operation directly (not via agent tool call).
"""
from __future__ import annotations

import json
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


async def dispatch_approval_notification(
    notification_targets: list[dict[str, str]],
    session_id: str,
    session_title: str,
    tool_name: str,
    danger_level: str,
    tenant_id: str,
    trigger_source: str,
) -> None:
    """Send notification to all configured targets for a pending approval.

    Each target is {"connector_id": "...", "contact": "..."}.
    Currently only email connectors are supported -- future connectors
    (Slack, Teams, WhatsApp) will use the same interface.

    Fire-and-forget: all errors are caught and logged, never raised.
    """
    for target in notification_targets:
        connector_id = target.get("connector_id", "")
        contact = target.get("contact", "")
        if not connector_id or not contact:
            continue

        try:
            await _send_via_email_connector(
                connector_id=connector_id,
                recipient=contact,
                session_id=session_id,
                session_title=session_title,
                tool_name=tool_name,
                danger_level=danger_level,
                tenant_id=tenant_id,
                trigger_source=trigger_source,
            )
            logger.info(
                f"Approval notification sent via connector {connector_id} to {contact}"
            )
        except Exception:
            logger.warning(
                f"Failed to send approval notification via connector {connector_id} "
                f"to {contact}",
                exc_info=True,
            )


async def _send_via_email_connector(
    connector_id: str,
    recipient: str,
    session_id: str,
    session_title: str,
    tool_name: str,
    danger_level: str,
    tenant_id: str,
    trigger_source: str,
) -> None:
    """Send approval notification via email connector.

    Loads the connector, instantiates EmailConnector, and calls send_email
    directly (bypassing the agent tool call path).
    """
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.models import ConnectorModel
    from meho_app.modules.connectors.email.connector import EmailConnector
    from meho_app.modules.connectors.credential_resolver import (
        CredentialResolver, SessionType,
    )
    from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from sqlalchemy import select

    session_maker = get_session_maker()
    async with session_maker() as db:
        # Load connector config
        stmt = select(ConnectorModel).where(ConnectorModel.id == connector_id)
        result = await db.execute(stmt)
        connector = result.scalar_one_or_none()
        if not connector:
            logger.warning(f"Notification connector {connector_id} not found")
            return

        # Resolve credentials using service credential path
        cred_repo = UserCredentialRepository(db)
        from meho_app.api.config import get_api_config
        config = get_api_config()
        keycloak_checker = KeycloakUserChecker(
            keycloak_url=config.keycloak_url,
            admin_username=config.keycloak_admin_username,
            admin_password=config.keycloak_admin_password,
        )
        resolver = CredentialResolver(cred_repo, keycloak_checker)

        try:
            resolved = await resolver.resolve(
                session_type=SessionType.INTERACTIVE,
                user_id=CredentialResolver.SENTINEL_SERVICE_USER,
                connector_id=str(connector.id),
            )
        except Exception:
            logger.warning(
                f"No service credential for notification connector {connector_id}. "
                f"Skipping notification.",
                exc_info=True,
            )
            return

        # Build and send email
        email_config = {
            **(connector.protocol_config or {}),
            "connector_name": connector.name,
            "tenant_id": tenant_id,
        }
        email_connector = EmailConnector(
            connector_id=str(connector.id),
            config=email_config,
            credentials=resolved.credentials,
        )
        await email_connector.connect()
        try:
            body_md = (
                f"## Pending Approval Required\n\n"
                f"An automated investigation needs your review.\n\n"
                f"- **Session:** {session_title}\n"
                f"- **Triggered by:** {trigger_source}\n"
                f"- **Action:** {tool_name}\n"
                f"- **Risk level:** {danger_level.upper()}\n\n"
                f"Please log in to MEHO to review and approve or reject this action."
            )
            await email_connector.execute("send_email", {
                "subject": f"[MEHO] Approval needed: {session_title}",
                "body_markdown": body_md,
                "to_emails": [recipient],
            })
        finally:
            await email_connector.disconnect()
