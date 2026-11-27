# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``tools.sign_out`` op: an admin signs out one tool of a person.

A tenant admin ends the logins of one person for one tool kind (for
example Claude Desktop). The person's other tools keep working. It is the
admin's form of the "Your tools" card of the account page
(:mod:`meho_backplane.ui.routes.account_tools`), and runs the same code:
:func:`~meho_backplane.auth.tool_sign_out.sign_out_tool`. The command line
verb is ``meho admin tools sign-out --user <name or id> --tool <kind>``.

* ``tool`` is a key of the same fixed list as the account page (``cli``,
  ``claude-code``, ``claude-desktop``, ``other-mcp``). The web console has
  no sign-out here, and the approval clients are not on the list. Like the
  account page, the op works only while ``MCP_CLIENT_PER_TOOL_KIND`` is on.
  While the switch is off, the call is refused at once, before any
  approval request.
* ``user`` is the person's Keycloak user id or exact username in MEHO's
  own sign-in realm (:mod:`meho_backplane.auth.realm_users`). Only a
  person in a role group of the caller's tenant is found.
* The approval card names the person by id. The approved run signs out
  that id, and checks again that it is in the tenant. It never looks the
  name up again: the name could point at another person by then.
* The audit row ``auth.tool_sign_out`` names the admin who ran the call
  (``operator_sub``, the approver), the admin who asked (``requested_by``),
  the person (``user_id``, ``username``) and the login client.

Approval and role:

* ``safety_level="caution"`` + ``requires_approval=True``: every call
  waits until a person approves it. Every tenant admin may approve their
  own request, so no second person is needed.
* The role floor is ``tenant_admin``, and only a person asks (no agent,
  no service login, no call on someone's behalf).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
    KeycloakConsentNotFoundError,
    KeycloakForbiddenError,
)
from meho_backplane.auth.operator import is_human_principal
from meho_backplane.auth.realm_users import (
    RealmUser,
    RealmUserNotFoundError,
    find_realm_user,
    user_in_tenant,
)
from meho_backplane.auth.tool_sign_out import (
    ToolKind,
    ToolSignOutIncompleteError,
    sign_out_tool,
    tool_kinds,
)
from meho_backplane.operations._preview import approved_preview
from meho_backplane.operations.secondary_target_policy import approved_requester_sub
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import Settings, get_settings

if TYPE_CHECKING:
    import uuid

    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations._preview import PreviewContext
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TOOLS_CONNECTOR_ID",
    "TOOLS_SIGN_OUT_OP_ID",
    "TOOL_KEYS",
    "build_tools_sign_out_preview",
    "register_tools_sign_out_operation",
    "tools_sign_out",
    "tools_sign_out_requester_refusal",
]

_log = structlog.get_logger(__name__)

TOOLS_CONNECTOR_ID: Final[str] = "tools-admin-1.x"
TOOLS_SIGN_OUT_OP_ID: Final[str] = "tools.sign_out"

#: The tool kinds an admin can sign out: the kinds of the account page
#: that have a sign-out button.
TOOL_KEYS: Final[tuple[str, ...]] = ("cli", "claude-code", "claude-desktop", "other-mcp")

NEEDS_PERSON: Final[str] = (
    "tools.sign_out: only a person with the tenant_admin role can sign out "
    "a tool of someone else; an agent or a service login cannot"
)
SWITCH_OFF: Final[str] = (
    "tools.sign_out: signing out one tool is off on this MEHO (the "
    "MCP_CLIENT_PER_TOOL_KIND switch); nothing was changed"
)
NOT_FOUND: Final[str] = (
    "tools.sign_out: MEHO's sign-in realm has no person {user!r} in your tenant; "
    "nothing was changed"
)
NO_CARD_USER: Final[str] = (
    "tools.sign_out: the approval card shows no person, so MEHO cannot tell "
    "whose tool was approved. Nothing was changed. Send a new request."
)
NOT_CONFIGURED: Final[str] = (
    "tools.sign_out: the Keycloak admin client of MEHO is not set up; nothing was changed"
)
FORBIDDEN: Final[str] = (
    "tools.sign_out: Keycloak refused the call: MEHO's Keycloak admin client "
    "needs the right manage-users. Nothing was changed."
)
PARTLY: Final[str] = (
    "tools.sign_out: Keycloak ended some logins of the tool, then a call failed. "
    "The tool may still work. Try again later."
)
FAILED: Final[str] = (
    "tools.sign_out: Keycloak could not sign out the tool ({reason}). Try again later."
)

_GROUP_KEY: Final[str] = "logins"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Admin work on the logins of the people of your tenant in MEHO's own "
    "sign-in realm, for example ending one tool's login after a laptop is lost."
)

TOOLS_SIGN_OUT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": (
                "The person whose tool to sign out: their Keycloak user id or "
                "their exact username in MEHO's own sign-in realm. Only people "
                "of your tenant are found."
            ),
        },
        "tool": {
            "type": "string",
            "enum": list(TOOL_KEYS),
            "description": (
                "The tool kind to sign out: `cli` (the meho command line), "
                "`claude-code`, `claude-desktop` or `other-mcp` (any other MCP "
                "client). The web console cannot be signed out here."
            ),
        },
    },
    "required": ["user", "tool"],
    "additionalProperties": False,
    "examples": [{"user": "<username>", "tool": "claude-desktop"}],
}

TOOLS_SIGN_OUT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string", "description": "Keycloak user id of the person."},
        "username": {"type": "string", "description": "Username of the person."},
        "tool": {"type": "string", "description": "The tool kind that was signed out."},
        "client_id": {"type": "string", "description": "The login client of the tool kind."},
        "outcome": {
            "type": "string",
            "enum": ["signed_out", "partly_signed_out", "shared_only", "nothing_to_sign_out"],
            "description": (
                "`signed_out`: every login of the tool ended. `partly_signed_out`: "
                "a login the tool shares with another tool was kept. `shared_only`: "
                "the tool has only shared logins, nothing ended. "
                "`nothing_to_sign_out`: the person has no login of the tool."
            ),
        },
        "offline_login_ended": {
            "type": "boolean",
            "description": "Whether the tool's saved (offline) login ended.",
        },
        "sessions_ended": {
            "type": "integer",
            "description": "How many ordinary logins of the tool ended.",
        },
        "shared_with": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Login clients of the other tools in a kept, shared login.",
        },
        "audit_id": {
            "type": ["string", "null"],
            "description": "Id of the auth.tool_sign_out audit row; null when nothing ended.",
        },
    },
    "required": [
        "user_id",
        "username",
        "tool",
        "client_id",
        "outcome",
        "offline_login_ended",
        "sessions_ended",
        "shared_with",
        "audit_id",
    ],
}

_DESCRIPTION: Final[str] = (
    "Sign out one tool kind (for example Claude Desktop) of one person of "
    "your tenant. The person's other tools keep working. Needs "
    "`tenant_admin`; each call waits for approval."
)

_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "A tenant admin must end one tool's login of another person, for "
        "example after a laptop with Claude Desktop is lost."
    ),
    "preconditions": (
        "Only a person with the `tenant_admin` role asks; an agent is "
        "refused. Each call waits until a person approves it. The "
        "MCP_CLIENT_PER_TOOL_KIND switch must be on."
    ),
    "parameter_hints": {
        "user": "Required. Keycloak user id or exact username of the person.",
        "tool": "Required. cli, claude-code, claude-desktop or other-mcp.",
    },
    "output_shape": (
        "{'user_id', 'username', 'tool', 'client_id', 'outcome', "
        "'offline_login_ended', 'sessions_ended', 'shared_with', 'audit_id'}"
    ),
    "next_step": (
        "The tool's last access token works until it ends (about five "
        "minutes). The tool can sign in again; to stop the person, disable "
        "their account."
    ),
}


def tools_sign_out_requester_refusal(operator: Operator, params: dict[str, Any]) -> str | None:
    """Why *operator* may not ask for this sign-out, or ``None`` when they may.

    Only a person asks, never on someone's behalf. The role floor checks
    that the person is a ``tenant_admin``. While ``MCP_CLIENT_PER_TOOL_KIND``
    is off, every call is refused here, so no approval request is made
    that could only fail.
    """
    del params  # every sign-out has the same rule
    if not is_human_principal(operator) or resolve_actor_sub() is not None:
        return NEEDS_PERSON
    if not get_settings().mcp_client_per_tool_kind:
        return SWITCH_OFF
    return None


def _kind(settings: Settings, key: str) -> ToolKind:
    """The tool kind *key* with a sign-out, while the switch is on."""
    if not settings.mcp_client_per_tool_kind:
        raise ValueError(SWITCH_OFF)
    for kind in tool_kinds(settings):
        if kind.key == key and kind.can_sign_out:
            return kind
    raise ValueError(f"tools.sign_out: {key!r} is not a tool kind MEHO can sign out")


def _keycloak_error(exc: KeycloakAdminError) -> ValueError:
    """Turn a Keycloak failure into the plain error of the call."""
    if isinstance(exc, KeycloakAdminNotConfiguredError):
        return ValueError(NOT_CONFIGURED)
    if isinstance(exc, ToolSignOutIncompleteError):
        return ValueError(PARTLY)
    if isinstance(exc, KeycloakForbiddenError):
        return ValueError(FORBIDDEN)
    return ValueError(FAILED.format(reason=type(exc).__name__))


async def _find_in_tenant(
    settings: Settings, user: str, tenant_id: uuid.UUID, *, by_id: bool
) -> RealmUser:
    """Find *user* in MEHO's sign-in realm and in *tenant_id*, else raise."""
    async with KeycloakAdminClient.from_settings(settings) as kc:
        found = await find_realm_user(kc, user, by_id=by_id)
        if not await user_in_tenant(kc, found.id, tenant_id):
            raise RealmUserNotFoundError(f"{user!r} is not in the tenant")
    return found


def _approved_user_id() -> str | None:
    """The person id on the approval card, during the approved run; else ``None``."""
    card = approved_preview(TOOLS_SIGN_OUT_OP_ID)
    if card is None:
        return None
    user = card.get("user")
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise ValueError(NO_CARD_USER)
    return user_id


def _outcome(ended: Any) -> str:
    if not ended.ended_any:
        return "shared_only"
    return "partly_signed_out" if ended.shared_with else "signed_out"


async def tools_sign_out(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Sign out one tool kind of one person of the operator's tenant.

    Op-id: ``tools.sign_out``. Targetless. A person who is not found, a
    tool kind that cannot be signed out and every Keycloak failure is a
    :class:`ValueError` with a plain message; then MEHO wrote no
    "signed out" audit row.
    """
    del target
    settings = get_settings()
    kind = _kind(settings, str(params.get("tool", "")))
    card_id = _approved_user_id()
    asked = card_id if card_id is not None else str(params.get("user", ""))
    try:
        user = await _find_in_tenant(settings, asked, operator.tenant_id, by_id=card_id is not None)
    except RealmUserNotFoundError as exc:
        raise ValueError(NOT_FOUND.format(user=asked)) from exc
    except KeycloakAdminError as exc:
        raise _keycloak_error(exc) from exc
    try:
        ended = await sign_out_tool(
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            kind=kind,
            settings=settings,
            for_user=user,
            requested_by=approved_requester_sub() or operator.sub,
        )
    except KeycloakConsentNotFoundError:
        return _result(user, kind, None)
    except KeycloakAdminError as exc:
        _log.warning(
            "tools_sign_out_failed",
            operator_sub=operator.sub,
            client_id=kind.client_id,
            reason=type(exc).__name__,
        )
        raise _keycloak_error(exc) from exc
    return _result(user, kind, ended)


def _result(user: RealmUser, kind: ToolKind, ended: Any) -> dict[str, Any]:
    return {
        "user_id": user.id,
        "username": user.username,
        "tool": kind.key,
        "client_id": kind.client_id,
        "outcome": "nothing_to_sign_out" if ended is None else _outcome(ended),
        "offline_login_ended": bool(ended and ended.offline_login_ended),
        "sessions_ended": ended.sessions_ended if ended else 0,
        "shared_with": list(ended.shared_with) if ended else [],
        "audit_id": str(ended.audit_id) if ended and ended.audit_id else None,
    }


async def build_tools_sign_out_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Name the person (by id) and the tool the sign-out would end. Reads only.

    When the person is not found in the caller's tenant, the card says so
    and the call would fail. A Keycloak failure raises; then the card has
    no person and the approved run is refused.
    """
    settings = get_settings()
    key = str(ctx.params.get("tool", ""))
    user = str(ctx.params.get("user", ""))
    tool = next((k for k in tool_kinds(settings) if k.key == key and k.can_sign_out), None)
    card: dict[str, Any] = {
        "action": TOOLS_SIGN_OUT_OP_ID,
        "tool": {"key": key, "client_id": tool.client_id if tool else None},
    }
    try:
        found = await _find_in_tenant(settings, user, ctx.operator.tenant_id, by_id=False)
    except RealmUserNotFoundError:
        return {
            **card,
            "user": None,
            "result_if_run": "fails",
            "note": "no person with this id or username in your tenant",
        }
    return {
        **card,
        "user": {"id": found.id, "username": found.username},
        "result_if_run": "signed_out" if tool and settings.mcp_client_per_tool_kind else "fails",
    }


async def register_tools_sign_out_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``tools.sign_out`` typed op into ``endpoint_descriptor``. Safe to run twice."""
    await register_typed_operation(
        product="tools",
        version="1.x",
        impl_id="tools-admin",
        op_id=TOOLS_SIGN_OUT_OP_ID,
        handler=tools_sign_out,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Sign out one tool kind of one person of your tenant; other tools keep working.",
        description=_DESCRIPTION,
        parameter_schema=TOOLS_SIGN_OUT_PARAMETER_SCHEMA,
        response_schema=TOOLS_SIGN_OUT_RESPONSE_SCHEMA,
        tags=["tools", "write", "logins"],
        safety_level="caution",
        requires_approval=True,
        hard_to_undo="stops",
        changes_rights=True,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
