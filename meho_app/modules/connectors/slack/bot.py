# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Bot -- slash command handler and Socket Mode lifecycle.

Manages the /meho slash command that triggers MEHO investigations from Slack.
The bot runs as a background task in FastAPI's lifespan using Socket Mode
(non-blocking connect_async/close_async).

Design:
- await ack() immediately (Slack 3-second timeout)
- Post a visible channel message (ack is ephemeral, cannot be threaded)
- Use asyncio.create_task() for the investigation (NOT lazy listeners)
- Thread the result under the visible message using thread_ts
- Use _run_agent_investigation from event_executor (accepted intra-package coupling)

Socket Mode: uses connect_async() NOT start_async() (start_async blocks the event loop).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

logger = get_logger(__name__)


class SlackBot:
    """Manages Slack bot lifecycle -- slash commands, Socket Mode, Events API."""

    def __init__(
        self,
        bot_token: str,
        app_token: str | None,
        connector_id: str,
        tenant_id: str,
        mode: str = "socket",
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._connector_id = connector_id
        self._tenant_id = tenant_id
        self._mode = mode
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None

    async def start(self) -> None:
        """Start the Slack bot (Socket Mode or HTTP Events API)."""
        from slack_bolt.async_app import AsyncApp

        self._app = AsyncApp(token=self._bot_token)

        # Register /meho command handler
        self._app.command("/meho")(self._handle_meho_command)

        if self._mode == "socket":
            if not self._app_token:
                raise ValueError(
                    "slack_app_token (xapp-*) required for Socket Mode. "
                    "Create an App-Level Token with connections:write scope "
                    "at Slack App > Basic Information > App-Level Tokens."
                )

            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )

            self._handler = AsyncSocketModeHandler(self._app, self._app_token)
            # connect_async() is non-blocking (returns immediately).
            # Do NOT use start_async() which blocks the event loop.
            await self._handler.connect_async()
            logger.info("Slack bot started (Socket Mode)")

        elif self._mode == "http":
            logger.warning(
                "HTTP Events API mode not yet implemented -- use socket mode. "
                "Set MEHO_SLACK_MODE=socket (default)."
            )
        else:
            raise ValueError(f"Unknown Slack mode: {self._mode}. Use 'socket' or 'http'.")

    async def stop(self) -> None:
        """Stop the Slack bot and close Socket Mode connection."""
        if self._handler:
            await self._handler.close_async()
        logger.info("Slack bot stopped")

    async def _handle_meho_command(
        self,
        ack: Any,
        body: dict[str, Any],
        client: Any,
        respond: Any,
    ) -> None:
        """Handle /meho slash command.

        Must call ack() within 3 seconds (Slack timeout).
        Posts a visible channel message for threading, then dispatches
        the investigation as an async task.
        """
        text = body.get("text", "").strip()

        if not text:
            await ack(":x: Usage: /meho <investigation prompt>")
            return

        # Immediate ack (ephemeral, only visible to the invoking user)
        await ack(f":mag: Starting investigation: _{text}_...")

        # Post a visible channel message for threading
        # (ack is ephemeral and cannot be threaded)
        msg = await client.chat_postMessage(
            channel=body["channel_id"],
            text=(f":mag: MEHO investigation started by <@{body['user_id']}>: _{text}_"),
        )

        # Dispatch investigation as async task (fire and forget)
        asyncio.create_task(
            self._run_investigation(
                body=body,
                client=client,
                thread_ts=msg["ts"],
            )
        )

    async def _run_investigation(
        self,
        body: dict[str, Any],
        client: Any,
        thread_ts: str,
    ) -> None:
        """Run MEHO investigation in the background.

        Creates a session, runs _run_agent_investigation from event_executor,
        and posts the result as a threaded reply.
        """
        text = body.get("text", "").strip()
        user_id = body.get("user_id", "unknown")
        user_name = body.get("user_name", "Unknown")
        channel_id = body["channel_id"]

        try:
            from meho_app.database import get_session_maker
            from meho_app.modules.agents.service import AgentService
            from meho_app.modules.connectors.event_executor import (
                _run_agent_investigation,
            )

            session_maker = get_session_maker()
            async with session_maker() as db:
                agent_service = AgentService(db)

                # Create a group session for this investigation
                connector_name = f"Slack ({user_name})"
                session = await agent_service.create_chat_session(
                    tenant_id=self._tenant_id,
                    user_id="system:event",
                    title=f"Slack: {text[:60]}",
                    visibility="tenant",
                    created_by_name=connector_name,
                    trigger_source=connector_name,
                )
                session_id = str(session.id)

                # Add user message to session
                await agent_service.add_chat_message(
                    session_id=session_id,
                    role="user",
                    content=text,
                    sender_id="system:event",
                    sender_name=connector_name,
                )

                # Build response_config for thread reply via response channel
                response_config = {
                    "connector_id": self._connector_id,
                    "operation_id": "post_message",
                    "parameter_mapping": {
                        "channel": channel_id,
                        "text": "{{ result }}",
                        "thread_ts": thread_ts,
                    },
                }

                # Run the investigation pipeline
                await _run_agent_investigation(
                    db=db,
                    session_id=session_id,
                    tenant_id=self._tenant_id,
                    connector_name=connector_name,
                    rendered_prompt=text,
                    agent_service=agent_service,
                    response_config=response_config,
                    payload={"text": text, "user_id": user_id, "channel_id": channel_id},
                    session_title=f"Slack: {text[:60]}",
                )

                logger.info(
                    f"Slack investigation complete: session={session_id[:8]}... "
                    f"user={user_name} prompt={text[:60]}"
                )

        except Exception as e:
            logger.error(f"Slack investigation failed: {e}", exc_info=True)
            # Post error as thread reply so the user sees it
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: Investigation failed: {str(e)[:200]}",
                    thread_ts=thread_ts,
                )
            except Exception as post_err:
                logger.error(f"Failed to post error to Slack: {post_err}")
