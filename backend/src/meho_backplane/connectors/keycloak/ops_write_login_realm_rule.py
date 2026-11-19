# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In MEHO's own login realm, a platform admin approves every Keycloak write.

The one exception: a write whose every key is on :data:`HARMLESS_KEYS`, a
short list of keys that cannot touch sign-in or rights (display names and
descriptions). A key that is not on the list counts as strict.

:func:`register_login_realm_rule` adds a last step to the approval card of
every Keycloak write (:data:`~.rights_changes.RIGHTS_CHANGE_OPS` and
:data:`~.rights_changes.NOT_RIGHTS_CHANGE_OPS`). The step puts the
``grant`` block on the card when the write has none, and:

* sets :data:`~meho_backplane.operations.grant_approval.HARMLESS_KEYS_ONLY`
  to whether the call sets only harmless keys, in every realm;
* in the login realm, when the call sets another key and the other checks
  of the write found no reason for a platform admin, sets
  ``platform_admin`` with the reason :data:`REASON_KEYS`.

The approval check (:mod:`meho_backplane.operations.grant_approval`) then
asks for a platform admin. The same step runs again on the card built
right before the approved change runs, so a change that now needs a
stricter approval does not run. A request approved with a card that has
no ``harmless_keys_only`` fact does not run either, in every realm.

Which realm a write changes: the ``realm`` param (and for
``keycloak.realm.create`` the ``realm`` of the representation) when the
write has one, else the target's managed realm
(:func:`~.ops_write_grant.is_login_realm`). When MEHO cannot read it, the
realm counts as the login realm.

A caller (``preview_operation``, ``meho_approvals_get``,
``meho_approvals_list``) reads the new facts only while
``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak.ops_write_grant import (
    GRANT_OPS,
    is_login_realm,
    is_login_realm_name,
)
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    PLATFORM_OPS,
    REASON_KEYS,
    SCOPE_MAPPER_UPDATE,
    agent_cards_on,
)
from meho_backplane.connectors.keycloak.rights_changes import (
    NOT_RIGHTS_CHANGE_OPS,
    RIGHTS_CHANGE_OPS,
)
from meho_backplane.operations._preview import (
    register_agent_preview_filter,
    register_preview_last_step,
)
from meho_backplane.operations.grant_approval import (
    HARMLESS_KEYS_ONLY,
    register_grant_op,
    register_harmless_keys_rule,
)

if TYPE_CHECKING:
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "HARMLESS_KEYS",
    "apply_keys_rule",
    "register_login_realm_rule",
    "sets_only_harmless_keys",
]

#: op_id -> (the param that holds the keys the call sets, the harmless keys,
#: the params that only name the object). Every other write, and every key
#: not named here, is strict.
HARMLESS_KEYS: Final[dict[str, tuple[str, frozenset[str], frozenset[str]]]] = {
    "keycloak.realm.update": ("representation", frozenset({"displayName"}), frozenset({"realm"})),
    "keycloak.client.update": (
        "representation",
        frozenset({"name", "description"}),
        frozenset({"client_id", "id"}),
    ),
    "keycloak.user.update": (
        "changes",
        frozenset({"firstName", "lastName"}),
        frozenset({"id", "username"}),
    ),
}

#: The writes whose ``realm`` param names the realm they change.
_NAMED_REALM_OPS: Final[frozenset[str]] = frozenset(
    {
        "keycloak.realm.create",
        "keycloak.realm.delete",
        "keycloak.realm.update",
        "keycloak.user.check_login",
    }
)


def sets_only_harmless_keys(op_id: str, params: dict[str, Any]) -> bool:
    """True when the call of *op_id* sets one key or more, all on :data:`HARMLESS_KEYS`."""
    entry = HARMLESS_KEYS.get(op_id)
    if entry is None:
        return False
    body_param, harmless, address = entry
    body = params.get(body_param)
    if not isinstance(body, dict) or not body:
        return False
    others = {
        key
        for key, value in params.items()
        if value is not None and key != body_param and key not in address
    }
    return not others and set(body) <= harmless


def apply_keys_rule(grant: dict[str, Any], harmless: bool) -> None:
    """Put the harmless keys fact on *grant*; ask for a platform admin in the login realm."""
    grant[HARMLESS_KEYS_ONLY] = harmless
    if harmless or grant.get("login_realm") is False or grant.get("platform_admin") is not False:
        return
    grant["platform_admin"] = True
    grant["platform_admin_reason"] = REASON_KEYS


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _changes_login_realm(op_id: str, ctx: PreviewContext) -> bool:
    """Whether the write changes a realm of MEHO's own login; ``True`` when that cannot be read."""
    try:
        if op_id in _NAMED_REALM_OPS:
            names = [_text(ctx.params.get("realm"))]
            representation = ctx.params.get("representation")
            if op_id == "keycloak.realm.create" and isinstance(representation, dict):
                names.append(_text(representation.get("realm")))
            given = [name for name in names if name is not None]
            if given:
                return any(is_login_realm_name(name) for name in given)
            if op_id == "keycloak.realm.create":
                return True
        return await is_login_realm(ctx)
    except Exception:
        return True


def _last_step(op_id: str) -> Any:
    async def _step(ctx: PreviewContext, preview: dict[str, Any]) -> None:
        if ctx.agent_preview and not agent_cards_on():
            return
        grant = preview.get("grant")
        if not isinstance(grant, dict):
            grant = {
                "user_id": None,
                "group_id": None,
                "login_realm": await _changes_login_realm(op_id, ctx),
                "platform_admin": False,
            }
        apply_keys_rule(grant, sets_only_harmless_keys(op_id, ctx.params))
        preview["grant"] = grant

    return _step


def _no_grant_for_agent(preview: dict[str, Any]) -> dict[str, Any]:
    """The stored preview as a caller reads it: no ``grant`` block while the switch is off."""
    if agent_cards_on() or "grant" not in preview:
        return preview
    return {key: value for key, value in preview.items() if key != "grant"}


#: The writes whose card has a ``grant`` block from another module already.
_HAS_GRANT: Final[frozenset[str]] = frozenset({*GRANT_OPS, *PLATFORM_OPS, SCOPE_MAPPER_UPDATE})


def register_login_realm_rule() -> None:
    """Apply the rule to every Keycloak write. Safe to call twice.

    A write with no ``grant`` block of its own is registered for the
    approval check here. It waits for a person's approval call by call only
    when it is a change of rights (:data:`~.rights_changes.RIGHTS_CHANGE_OPS`).
    """
    for op_id in (*RIGHTS_CHANGE_OPS, *NOT_RIGHTS_CHANGE_OPS):
        register_preview_last_step(op_id, _last_step(op_id))
        register_harmless_keys_rule(op_id)
        if op_id not in _HAS_GRANT:
            register_grant_op(op_id, per_call=op_id in RIGHTS_CHANGE_OPS)
            register_agent_preview_filter(op_id, _no_grant_for_agent)


register_login_realm_rule()
