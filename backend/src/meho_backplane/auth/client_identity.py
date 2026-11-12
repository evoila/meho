# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``client`` block of an audit row: through which login client a call came.

With the ``AUDIT_CLIENT_IDENTITY`` switch on, every audit row gets one
payload block ``client``::

    {"azp": "meho-cli", "channel": "token", "principal_kind": "user",
     "sid_hash": "3f1c0a9b2d4e5f60"}

* ``azp`` -- the login client the token was made for (the ``azp`` claim).
  ``None`` when the token has none.
* ``channel`` -- ``"web"`` for a call from a person's web console session,
  ``"token"`` for every call that came with a token (command line, MCP,
  REST, a service login).
* ``principal_kind`` -- user, agent, service or runner.
* ``sid_hash`` -- the first 16 hex characters of the SHA-256 of the
  ``sid`` claim (the sign-in session). It groups the rows of one sign-in.
  The raw ``sid`` is never stored. ``None`` when the token has no ``sid``.

With the switch off (the default) no row gets the block, so every row
stays exactly as before.

Three audit writers take the :class:`~meho_backplane.auth.operator.Operator`
directly and call :func:`apply_client_block`: the MCP rows
(:mod:`meho_backplane.mcp.audit`), the operation rows
(:mod:`meho_backplane.operations._audit`) and the approval rows
(:mod:`meho_backplane.operations.approval_queue`). The REST request row
(:mod:`meho_backplane.audit`) is written by a middleware that has no
``Operator``. For it, the login check of the request calls
:func:`bind_request_client`, and the middleware reads the block back with
:func:`request_client_block`.

A web console page checks its token in the session middleware, which runs
before the audit middleware resets the block. So that check keeps the
block per session (:mod:`meho_backplane.ui.auth.revalidation`), the
session context carries it, and the page dependency binds it again with
:func:`bind_request_client_block`.
"""

from __future__ import annotations

import hashlib
import unicodedata
from contextvars import ContextVar
from typing import Any, Final, Literal

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

#: The payload key of the block.
CLIENT_PAYLOAD_KEY: Final[str] = "client"

#: How many hex characters of the SHA-256 of ``sid`` are kept.
_SID_HASH_LENGTH: Final[int] = 16

#: The block of the current request, for the REST request row. Set by the
#: login check, reset by the audit middleware at the start of each request.
_request_client: ContextVar[dict[str, Any] | None] = ContextVar(
    "audit_request_client", default=None
)


#: Longest client text kept by default: the client name, version and
#: ``User-Agent`` on the MCP session row.
CLIENT_TEXT_MAX_CHARS: Final[int] = 200

#: Unicode groups removed from client texts: control (``Cc``), format
#: (``Cf``), line and paragraph separators (``Zl`` U+2028, ``Zp`` U+2029),
#: and lone surrogates (``Cs``).
_DROPPED_CHAR_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


def clean_client_text(value: object, max_chars: int = CLIENT_TEXT_MAX_CHARS) -> str | None:
    """Return *value* without unsafe characters, cut to *max_chars* characters.

    A tool sends these texts about itself (its MCP client name, its
    ``User-Agent``), so they are not trusted. A control character or a line
    separator could break a log line or a terminal. A lone surrogate cannot
    be stored as UTF-8, so the row write would fail. A value that is not a
    string, or is empty after cleaning, gives ``None``. Every stored client
    text goes through this one function.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        ch for ch in value if unicodedata.category(ch) not in _DROPPED_CHAR_CATEGORIES
    )
    return cleaned[:max_chars] or None


def client_identity_enabled() -> bool:
    """Return ``True`` when the ``AUDIT_CLIENT_IDENTITY`` switch is on."""
    return get_settings().audit_client_identity


def channel_of(operator: Operator) -> Literal["web", "token"]:
    """Return ``"web"`` for a web console session, else ``"token"``."""
    return "web" if operator.web_session else "token"


def sid_hash(sid: str | None) -> str | None:
    """Return the first 16 hex characters of the SHA-256 of *sid*, or ``None``."""
    if not sid:
        return None
    return hashlib.sha256(sid.encode()).hexdigest()[:_SID_HASH_LENGTH]


def build_client_block(operator: Operator) -> dict[str, Any]:
    """Build the ``client`` block for *operator*, whatever the switch says."""
    return {
        "azp": operator.azp,
        "channel": channel_of(operator),
        "principal_kind": operator.principal_kind.value,
        "sid_hash": sid_hash(operator.sid),
    }


def apply_client_block(payload: dict[str, Any], operator: Operator) -> None:
    """Put the ``client`` block of *operator* into *payload* when the switch is on.

    The block always comes from the checked login, so it replaces any
    ``client`` key that came another way.
    """
    if client_identity_enabled():
        payload[CLIENT_PAYLOAD_KEY] = build_client_block(operator)


def bind_request_client(operator: Operator) -> None:
    """Keep the ``client`` block of *operator* for the REST request row."""
    if client_identity_enabled():
        _request_client.set(build_client_block(operator))


def bind_request_client_block(block: dict[str, Any] | None) -> None:
    """Keep a block that an earlier login check of this request built."""
    if client_identity_enabled() and block is not None:
        _request_client.set(block)


def request_client_block() -> dict[str, Any] | None:
    """Return the block bound by :func:`bind_request_client`, or ``None``."""
    if not client_identity_enabled():
        return None
    return _request_client.get()


def reset_request_client() -> None:
    """Forget the block of an earlier request. Called at request start."""
    _request_client.set(None)


def via_label(payload: object) -> str | None:
    """Return ``"<client> (<channel>)"`` for an audit row payload, or ``None``.

    The audit detail views show it as "via". ``None`` when the row has no
    ``client`` block (an older row, or the switch was off).
    """
    if not isinstance(payload, dict):
        return None
    block = payload.get(CLIENT_PAYLOAD_KEY)
    if not isinstance(block, dict):
        return None
    azp = block.get("azp")
    channel = block.get("channel")
    client = azp if isinstance(azp, str) and azp else "unknown client"
    if isinstance(channel, str) and channel:
        return f"{client} ({channel})"
    return client
