# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense governed writes that turn one local user off or on (#3937).

Two typed ops change the ``disabled`` flag of ONE local user
(``config.xml`` ``<system><user>``):

* ``pfsense.user.disable`` -- set the flag. pfSense then refuses this
  user's login (web UI, VPN, and other local-database logins).
* ``pfsense.user.enable`` -- clear the flag again.

"Disable" is the safe step that can be undone. The account, its password,
its groups and its certificates stay as they are.

What the ops do on the firewall
-------------------------------

The same steps the pfSense web page does for the "disabled" box (checked
against pfSense 2.7.2 ``system_usermanager.php``): set or clear
``$user['disabled']``, call ``local_user_set()`` (it locks or unlocks the
system account), then ``write_config()``. No firewall reload. No other
field of the user changes.

The change runs through the same ``pfSsh.php playback`` path the other
pfSense writes use
(:func:`~meho_backplane.connectors.pfsense.ops_write._apply_playback`).

Governed
--------

Both ops are ``safety_level="dangerous"`` + ``requires_approval=True``: a
second person must approve every call. The approval card shows the user
name, the current state and the new state (preview builder below).

Checks before any change
------------------------

The handler reads ``config.xml`` first and refuses, without staging
anything:

* ``not_found`` -- no local user has this name;
* ``ambiguous`` -- more than one user has this name (a broken config);
* ``protected`` -- the built-in admin account (``uid`` 0 or ``scope``
  ``system``), so nobody locks out the web UI by mistake.

When the user already has the wanted state, the result is
``nothing_to_do: true`` and nothing is written (safe to run twice).

After the write the handler reads ``config.xml`` again. It reports success
only when the user's flag has the new value AND no other user's flag
changed. Otherwise it raises.

One edit at a time
------------------

Both handlers carry the
:func:`~meho_backplane.connectors.pfsense.ops_write.serialized_config_write`
decorator, like every other op that edits ``config.xml`` (#3544). It holds
the appliance's write lock from the first ``config.xml`` read to the last
read-back. ``write_config()`` saves the whole in-memory ``$config``, so a
user write that overlapped with, say, a gateway add on the same pfSense
could save an old copy and undo the other change. The lock makes the two
run one after the other.

Input safety
------------

The user name is checked against a strict allowlist
(:data:`USER_NAME_RE`: letters, digits, dot, dash, underscore; it may not
start with a dash) before any SSH call. The PHP fragment emits it only
through :func:`~meho_backplane.connectors.pfsense.ops_write._php_squote`
inside a quoted heredoc. No fragment line starts with ``!`` or ``=``:
``pfSsh.php playback`` turns such lines into shell commands.

Secrets
-------

The parser reads only ``name``, ``uid``, ``scope`` and whether a
``<disabled>`` child exists. It never reads password hashes, keys or
certificates, and the result never carries them.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_config_reads import _parse_config_root
from meho_backplane.connectors.pfsense.ops_read import _xml_text
from meho_backplane.connectors.pfsense.ops_write import (
    _apply_playback,
    _php_squote,
    _read_config_xml,
    serialized_config_write,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "USER_NAME_RE",
    "USER_WRITE_OPS",
    "build_user_flag_playback",
    "pfsense_user_disable",
    "pfsense_user_enable",
    "register_pfsense_user_preview_builders",
]

#: pfSense accepts ``[a-zA-Z0-9.-_]`` and at most 32 characters for a local
#: user name. A leading dash is refused here as well: it could be read as a
#: command-line option by the tools pfSense calls with the name.
USER_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,31}$")


def _validate_user_name(value: Any) -> str:
    """Return *value* if it is an allowed pfSense user name, else raise ``ValueError``."""
    if not isinstance(value, str) or not USER_NAME_RE.fullmatch(value):
        raise ValueError(
            "name must match ^[A-Za-z0-9._][A-Za-z0-9._-]{0,31}$ (letters, digits, "
            f"dot, dash, underscore; no leading dash); got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# config.xml parsing -- name / uid / scope / disabled only
# ---------------------------------------------------------------------------


def _user_rows(xml_text: str) -> list[dict[str, Any]]:
    """Return ``{name, uid, scope, disabled}`` for every local user.

    No other child of ``<user>`` is read. Raises
    :class:`~meho_backplane.connectors.pfsense.ops_config_reads.PfSenseConfigParseError`
    when the file is empty or cannot be parsed.
    """
    root = _parse_config_root(xml_text)
    system = root.find("system")
    if system is None:
        return []
    rows: list[dict[str, Any]] = []
    for user in system.findall("user"):
        rows.append(
            {
                "name": (_xml_text(user, "name") or "").strip(),
                "uid": (_xml_text(user, "uid") or "").strip(),
                "scope": (_xml_text(user, "scope") or "").strip(),
                "disabled": user.find("disabled") is not None,
            }
        )
    return rows


def _is_protected(row: dict[str, Any]) -> bool:
    """``True`` for the built-in admin account (uid 0 or a ``system`` scope)."""
    return bool(row["uid"] == "0" or row["scope"] == "system")


def _state(disabled: bool) -> str:
    return "disabled" if disabled else "enabled"


def _other_flags(rows: list[dict[str, Any]], name: str) -> Counter[tuple[str, bool]]:
    """Count ``(name, disabled)`` for every user except *name*."""
    return Counter((r["name"], r["disabled"]) for r in rows if r["name"] != name)


def _refusal(rows: list[dict[str, Any]], name: str) -> tuple[str | None, str | None]:
    """Return ``(status, guidance)`` when the change must be refused, else ``(None, None)``."""
    matches = [r for r in rows if r["name"] == name]
    if not matches:
        return "not_found", f"no local user named {name!r} in config.xml; nothing changed"
    if len(matches) > 1:
        return "ambiguous", (
            f"{len(matches)} local users share the name {name!r} (broken config); "
            "refusing to change any of them"
        )
    if _is_protected(matches[0]):
        return "protected", (
            f"{name!r} is the built-in admin account; MEHO does not turn it off or on, "
            "so nobody locks out the web UI by mistake"
        )
    return None, None


# ---------------------------------------------------------------------------
# PHP playback fragment
# ---------------------------------------------------------------------------


def build_user_flag_playback(name: str, *, disable: bool) -> str:
    """Return the raw-PHP playback fragment that sets or clears ONE user's flag.

    The fragment finds the user by exact name and changes only
    ``['disabled']`` of that one entry, and only when exactly one user
    matched and it is not uid 0. Then it calls ``local_user_set()`` and
    ``write_config()``, like the pfSense web page. ``$userindex`` is rebuilt
    first because ``local_user_set()`` reads the flag back through it.

    *name* must already have passed :func:`_validate_user_name`; it is
    validated again here so the fragment can never carry anything else.
    """
    name = _validate_user_name(name)
    action = "disable" if disable else "enable"
    change = (
        "  $config['system']['user'][$meho_idx]['disabled'] = true;"
        if disable
        else "  unset($config['system']['user'][$meho_idx]['disabled']);"
    )
    lines = [
        "global $config, $userindex;",
        "require_once('auth.inc');",
        f"$meho_name = {_php_squote(name)};",
        "$meho_matched = 0;",
        "$meho_idx = null;",
        "if (is_array($config['system']) && is_array($config['system']['user'])) {",
        "  foreach ($config['system']['user'] as $meho_i => $meho_u) {",
        "    if (isset($meho_u['name']) && (string)$meho_u['name'] === $meho_name) {",
        "      $meho_matched++;",
        "      $meho_idx = $meho_i;",
        "    }",
        "  }",
        "}",
        "if ($meho_matched === 1 && (string)$config['system']['user'][$meho_idx]['uid'] !== '0') {",
        change,
        "  $userindex = index_users();",
        "  local_user_set($config['system']['user'][$meho_idx]);",
        f"  write_config({_php_squote(f'meho: {action} user {name}')});",
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _set_user_disabled(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None,
    *,
    disable: bool,
) -> dict[str, Any]:
    """Shared body of ``pfsense.user.disable`` / ``pfsense.user.enable``."""
    name = _validate_user_name(params.get("name"))
    wanted = _state(disable)
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "user",
        "name": name,
        "action": "disable" if disable else "enable",
        "previous_state": None,
        "new_state": wanted,
        "nothing_to_do": False,
        "applied": False,
        "verified": False,
        "guidance": None,
    }

    before_rows = _user_rows(await _read_config_xml(self, target, operator))
    status, guidance = _refusal(before_rows, name)
    if status is not None:
        return {**result, "status": status, "new_state": None, "guidance": guidance}

    current = next(r for r in before_rows if r["name"] == name)
    result["previous_state"] = _state(current["disabled"])
    if current["disabled"] == disable:
        return {**result, "status": "nothing_to_do", "nothing_to_do": True, "verified": True}

    await _apply_playback(
        self,
        target,
        script_name=f"meho_user_{result['action']}_{re.sub(r'[^A-Za-z0-9]', '_', name)}",
        script_body=build_user_flag_playback(name, disable=disable),
        operator=operator,
    )

    after_rows = _user_rows(await _read_config_xml(self, target, operator))
    after = [r for r in after_rows if r["name"] == name]
    if len(after) != 1 or after[0]["disabled"] != disable:
        raise RuntimeError(
            f"user.{result['action']} verification failed for {name!r}: the user is not "
            f"{wanted} in config.xml after write_config; the pfSsh.php playback did not persist"
        )
    if _other_flags(after_rows, name) != _other_flags(before_rows, name):
        raise RuntimeError(
            f"user.{result['action']} verification failed for {name!r}: another user's "
            "disabled flag or the user list changed during the write; check config.xml"
        )
    return {**result, "status": wanted, "applied": True, "verified": True}


@serialized_config_write
async def pfsense_user_disable(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.user.disable`` -- set ONE user's ``disabled`` flag."""
    return await _set_user_disabled(self, target, params, operator, disable=True)


@serialized_config_write
async def pfsense_user_enable(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.user.enable`` -- clear ONE user's ``disabled`` flag."""
    return await _set_user_disabled(self, target, params, operator, disable=False)


# ---------------------------------------------------------------------------
# Approval preview -- user, current state, new state
# ---------------------------------------------------------------------------


def _user_preview(disable: bool) -> Any:
    """Return the preview builder for the disable (``True``) or enable op."""

    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        name = ctx.params.get("name")
        if not isinstance(name, str) or not USER_NAME_RE.fullmatch(name):
            return None
        preview: dict[str, Any] = {
            "user": name,
            "current_state": None,
            "new_state": _state(disable),
            "nothing_to_do": False,
            "refusal": None,
        }
        if ctx.connector_instance is None:
            # The egress-free preview path: no live read, so the current
            # state is unknown here. The approval card reads it live.
            return preview
        xml_text = await _read_config_xml(
            ctx.connector_instance,  # type: ignore[arg-type]  # PfSenseConnector at runtime
            ctx.target,
            ctx.operator,
        )
        rows = _user_rows(xml_text)
        status, _ = _refusal(rows, name)
        if status is not None:
            return {**preview, "new_state": None, "refusal": status}
        current = next(r for r in rows if r["name"] == name)
        return {
            **preview,
            "current_state": _state(current["disabled"]),
            "nothing_to_do": current["disabled"] == disable,
        }

    return _builder


def register_pfsense_user_preview_builders() -> None:
    """Wire the approval-card preview for both ops. Called at import time."""
    register_preview_builder("pfsense.user.disable", _user_preview(True))
    register_preview_builder("pfsense.user.enable", _user_preview(False))


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": USER_NAME_RE.pattern,
            "description": (
                "Exact name of the ONE local user, as ``pfsense.user.list`` shows it. "
                "Letters, digits, dot, dash, underscore; at most 32 characters."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["user"]},
        "status": {
            "type": "string",
            "enum": [
                "disabled",
                "enabled",
                "nothing_to_do",
                "not_found",
                "ambiguous",
                "protected",
            ],
        },
        "name": {"type": "string"},
        "action": {"type": "string", "enum": ["disable", "enable"]},
        "previous_state": {"type": ["string", "null"], "enum": ["disabled", "enabled", None]},
        "new_state": {"type": ["string", "null"], "enum": ["disabled", "enabled", None]},
        "nothing_to_do": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "name",
        "action",
        "previous_state",
        "new_state",
        "nothing_to_do",
        "applied",
        "verified",
        "guidance",
    ],
    "additionalProperties": False,
}

_OUTPUT_SHAPE = (
    "``{op_class: 'write', resource: 'user', status, name, action, "
    "previous_state, new_state, nothing_to_do, applied, verified, guidance}``. "
    "``status`` is the new state (``disabled`` / ``enabled``) after a verified "
    "change; ``nothing_to_do`` when the user already had that state (nothing "
    "written); ``not_found`` / ``ambiguous`` / ``protected`` when the call was "
    "refused before any change (``guidance`` says why). No secret field of the "
    "user is ever returned."
)


def _llm_instructions(disable: bool) -> dict[str, Any]:
    if disable:
        when = (
            "Turn OFF one pfSense local user, for example a VPN user whose event or "
            "contract has ended. pfSense then refuses this user's login. The account, "
            "password, groups and certificates stay, so ``pfsense.user.enable`` can "
            "turn it on again. Prefer this over deleting a user. VPN sessions that "
            "are already open are not closed by this op."
        )
    else:
        when = (
            "Turn ON again one pfSense local user that was disabled (for example with "
            "``pfsense.user.disable``). Nothing else about the user changes."
        )
    return {
        "when_to_use": (
            f"{when} A second person must approve every call (safety_level=dangerous). "
            "Check the user first with ``pfsense.user.list``. Refused for an unknown "
            "name, a name used by more than one user, and the built-in admin account. "
            "Safe to run twice: a user already in the wanted state returns "
            "``nothing_to_do: true``."
        ),
        "parameter_hints": {"name": "Required. Exact user name from ``pfsense.user.list``."},
        "output_shape": _OUTPUT_SHAPE,
    }


def _description(disable: bool) -> str:
    verb = "Sets" if disable else "Clears"
    return (
        f"{verb} the ``disabled`` flag of ONE pfSense local user (``config.xml`` "
        "``<system><user>``), found by exact name. Same steps as the pfSense web "
        "page: change the flag, ``local_user_set()``, ``write_config()``. No "
        "firewall reload; no change to password, groups or certificates. Needs "
        "approval from a second person; the approval card shows the user, the "
        "current state and the new state. Refused before any change for an "
        "unknown name (``not_found``), a duplicate name (``ambiguous``) and the "
        "built-in admin account (``protected``). A user already in the wanted "
        "state returns ``nothing_to_do: true`` and nothing is written. After the "
        "write, config.xml is read again: success only when the flag has the new "
        "value and no other user's flag changed."
    )


USER_WRITE_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.user.disable",
        handler_attr="user_disable",
        summary="Turn off ONE pfSense local user (set the disabled flag; needs approval).",
        description=_description(True),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="users",
        tags=("write", "users", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_llm_instructions(True),
    ),
    PfSenseOp(
        op_id="pfsense.user.enable",
        handler_attr="user_enable",
        summary="Turn on ONE pfSense local user again (clear the disabled flag; needs approval).",
        description=_description(False),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="users",
        tags=("write", "users", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_llm_instructions(False),
    ),
)


# Import-time side effect: register the approval-card previews (mirrors
# ``ops_delete.register_pfsense_delete_preview_builders``).
register_pfsense_user_preview_builders()
