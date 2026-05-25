# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resource-server-enforced delegation context (G11.2-T2 #816).

When a human triggers an agent run, the synchronous audit row must record
*both* the human initiator and the agent that acted — the RFC 8693
``sub`` (subject) + ``act`` (actor) two-claim shape
(https://datatracker.ietf.org/doc/html/rfc8693#section-1.1). The IdP
cannot mint that token: Keycloak's standard token exchange is
impersonation-only and the delegation path (RFC 8693's second, *actor*
token) is unimplemented (keycloak#38279). So MEHO synthesises the binding
**at the resource server** — where it already records attribution, in the
synchronous append-only audit log.

This module is the seam. :func:`actor_delegation` binds the acting agent's
principal into structlog's contextvars for the lifetime of a user-initiated
agent run; :func:`resolve_actor_sub` reads it back at each audit write path
(chassis HTTP, MCP, dispatcher). The agent's tool calls run *in process*
under the human's :class:`~meho_backplane.auth.operator.Operator`
(``operator_sub``=human), so binding the actor here makes every audit row
the run produces carry ``operator_sub``=human + ``actor_sub``=agent without
a second token over the wire.

Autonomous (cron / no-human) runs do **not** use this seam: the agent
authenticates as itself via ``client_credentials`` (see
:mod:`meho_backplane.auth.agent_token`), so it is the subject
(``operator_sub``=agent) with no separate actor — ``actor_sub`` stays
``None``.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import structlog

__all__ = ["ACTOR_SUB_KEY", "actor_delegation", "resolve_actor_sub"]

#: structlog contextvar key carrying the RFC 8693 actor (agent) principal.
#: Shared by the binder (:func:`actor_delegation`) and the readers
#: (:func:`resolve_actor_sub`) so the key never drifts between them.
ACTOR_SUB_KEY = "actor_sub"


@contextmanager
def actor_delegation(actor_sub: str) -> Iterator[None]:
    """Bind *actor_sub* as the RFC 8693 actor for the duration of the block.

    Wrap a user-initiated agent run with this so every audit row the run
    produces records the acting agent alongside the human ``operator_sub``.
    Exception-safe: structlog's :func:`~structlog.contextvars.bound_contextvars`
    restores the prior context on exit (including across an ``await`` inside
    the block, since the run executes in a single asyncio task whose context
    is this one).

    Fails closed: an empty *actor_sub* raises :class:`ValueError` rather than
    silently running with no actor recorded — a delegated run must always
    attribute the agent.
    """
    if not actor_sub:
        raise ValueError(
            "actor_sub must be a non-empty agent principal reference; "
            "a delegated agent run cannot silently drop the actor"
        )
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: actor_sub}):
        yield


def resolve_actor_sub() -> str | None:
    """Return the bound RFC 8693 actor sub, or ``None`` when unbound.

    ``None`` is the correct value for direct human requests and for
    autonomous agent runs (agent is the subject, no separate actor). A
    present-but-blank contextvar value normalises to ``None``.
    """
    raw = structlog.contextvars.get_contextvars().get(ACTOR_SUB_KEY)
    return raw if isinstance(raw, str) and raw else None
