# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Salt key changes: accept, reject and delete minion keys.

Each change names its keys as exact ``(master_id, minion_id)`` pairs. The
schema refuses glob characters, so a change never reaches more keys than the
caller named. The body reads the key state of every pair first and refuses
the whole call when a pair has no key. Then it calls
``minions.set_minion_key_state`` once with all pairs, and reads the states
again so the answer shows what changed.

The read after the change runs when the change is already done, so its
failure must not make the dispatcher run the change again. A ``401`` there
(the login expired between the change and the read) drops the session and
reads once more. When the read still fails, the answer keeps
``status: requested``, leaves ``changed_count`` and each ``key_state_after``
``null``, and says in ``reason`` why the states could not be read.

``salt.key.delete`` is ``destructive``. Its preview builder reads the keys
and puts them, with their states, on the approval card. The approved run
reads them again and stops with ``precondition_failed`` when a key or its
state is not what the approver saw.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx

from meho_backplane.connectors.salt.connector import (
    MINION_KEY_STATE_RPC,
    SET_KEY_STATE_RPC,
    SaltChangeRefusedError,
)
from meho_backplane.connectors.salt.reads import _key_row, _results
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.salt.connector import SaltConnector
    from meho_backplane.connectors.salt.session import SaltTargetLike

__all__ = ["DELETE_OP_ID", "KeyAction", "key_change_impl", "read_key_states"]

KeyAction = Literal["accept", "reject", "delete"]

DELETE_OP_ID: Final[str] = "salt.key.delete"

#: Rows one key read asks for; the body then picks the exact pair.
_KEY_READ_LIMIT: Final[int] = 100

#: Longest error text the answer keeps when the read after a change fails.
_READ_ERROR_LIMIT: Final[int] = 300

#: The HTTP status RaaS answers when the login expired.
_LOGIN_EXPIRED: Final[int] = 401

#: The include flags each action takes, as the vendor page lists them.
_INCLUDE_FLAGS: Final[dict[str, tuple[str, ...]]] = {
    "accept": ("include_rejected", "include_denied"),
    "reject": ("include_accepted", "include_denied"),
    "delete": (),
}

_DELETE_IRREVERSIBILITY: Final[str] = (
    "The keys are removed from the master. The minions get no jobs until each one "
    "sends its key again (restart its salt-minion service) and a person accepts it."
)


def _pairs(params: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(master_id, minion_id)`` pairs of the call, without repeats, in order."""
    seen: dict[tuple[str, str], None] = {}
    for item in params.get("minions") or []:
        seen[(str(item["master_id"]), str(item["minion_id"]))] = None
    return list(seen)


async def read_key_states(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    pairs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """The key state of each pair; ``key_state`` is ``None`` when the master has no key."""
    rows = []
    for master_id, minion_id in pairs:
        ret = await connector.rpc(
            target,
            operator,
            MINION_KEY_STATE_RPC,
            {"master_id": master_id, "minion_id": minion_id, "limit": _KEY_READ_LIMIT},
        )
        found, _ = _results(ret)
        state = next(
            (
                key["key_state"]
                for key in map(_key_row, found)
                if key["minion_id"] == minion_id and key["master_id"] in (master_id, None)
            ),
            None,
        )
        rows.append({"master_id": master_id, "minion_id": minion_id, "key_state": state})
    return rows


def _missing(rows: list[dict[str, Any]]) -> str | None:
    gone = [
        f"{row['minion_id']} on master {row['master_id']}" for row in rows if not row["key_state"]
    ]
    if not gone:
        return None
    more = f" and {len(gone) - 10} more" if len(gone) > 10 else ""
    return (
        f"there is no key for minion {', '.join(gone[:10])}{more}. "
        "salt.key.list shows the keys and their masters."
    )


def keys_sha256(rows: list[dict[str, Any]]) -> str:
    """A hash of the keys and their states, in a fixed order."""
    triples = sorted([row["master_id"], row["minion_id"], row["key_state"]] for row in rows)
    return hashlib.sha256(json.dumps(triples).encode()).hexdigest()


def _delete_card(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "blast_radius": {
            "object": {
                "kind": "minion_keys",
                "count": len(rows),
                "keys_sha256": keys_sha256(rows),
            },
            "children": [{"kind": "minion_key", **row} for row in rows],
            "irreversibility": _DELETE_IRREVERSIBILITY,
        }
    }


async def _key_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``salt.key.delete``. Read-only; raises to refuse.

    Declines (``None``) without a live connector or target: then there is no
    card, and the destructive gate refuses the call.
    """
    from meho_backplane.connectors.salt.connector import SaltConnector

    connector = ctx.connector_instance
    if not isinstance(connector, SaltConnector) or ctx.target is None:
        return None
    rows = await read_key_states(connector, ctx.operator, ctx.target, _pairs(ctx.params))
    refusal = _missing(rows)
    if refusal is not None:
        raise SaltChangeRefusedError(f"Nothing to delete: {refusal}")
    return _delete_card(rows)


def _delete_drift(rows: list[dict[str, Any]]) -> str | None:
    """Why the keys now differ from what the approver saw, or ``None``."""
    seen = approved_preview(DELETE_OP_ID)
    seen_object = ((seen or {}).get("blast_radius") or {}).get("object") or {}
    seen_hash = seen_object.get("keys_sha256")
    if not seen_hash:
        return "the approval holds no key list to compare with. Preview and ask again."
    if seen_hash != keys_sha256(rows):
        return (
            "the keys or their states changed since the approver saw them. Preview and ask again."
        )
    return None


def _set_kwarg(
    action: KeyAction, pairs: list[tuple[str, str]], params: dict[str, Any]
) -> dict[str, Any]:
    kwarg: dict[str, Any] = {
        "state": action,
        "minions": [[master_id, minion_id] for master_id, minion_id in pairs],
    }
    for flag in _INCLUDE_FLAGS[action]:
        if params.get(flag) is True:
            kwarg[flag] = True
    return kwarg


async def _states_after_change(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    pairs: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The key states after the change, or ``None`` and why they could not be read.

    Raises nothing the RPC path raises: the change is done, and an error here
    would make the dispatcher run it again. A ``401`` drops the session and
    reads once more, like the dispatcher does for a whole call.
    """
    logged_in_again = False
    while True:
        try:
            return await read_key_states(connector, operator, target, pairs), None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == _LOGIN_EXPIRED and not logged_in_again:
                logged_in_again = True
                await connector.invalidate_session(target, rejected=exc.request)
                continue
            failure: Exception = exc
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            failure = exc
        return None, f"{type(failure).__name__}: {failure}"[:_READ_ERROR_LIMIT]


def _unread_answer(action: KeyAction, before: list[dict[str, Any]], error: str) -> dict[str, Any]:
    """The answer of a change RaaS took, whose states could not be read afterwards."""
    return {
        "status": "requested",
        "action": action,
        "reason": (
            f"RaaS took the change, but the read of the key states after it failed: "
            f"{error}. Do not send the change again; salt.key.list shows the states now."
        ),
        "changed_count": None,
        "keys": [
            {
                "master_id": row["master_id"],
                "minion_id": row["minion_id"],
                "key_state_before": row["key_state"],
                "key_state_after": None,
            }
            for row in before
        ],
    }


async def key_change_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
    *,
    action: KeyAction,
) -> dict[str, Any]:
    """Read the keys, check them, set the new state once, read them again."""
    pairs = _pairs(params)
    before = await read_key_states(connector, operator, target, pairs)
    refusal = _missing(before)
    if refusal is not None:
        raise SaltChangeRefusedError(f"Nothing was changed: {refusal}")
    if action == "delete":
        drift = _delete_drift(before)
        if drift is not None:
            return {
                "status": "precondition_failed",
                "action": action,
                "reason": f"Nothing was deleted: {drift}",
                "changed_count": 0,
                "keys": [
                    {
                        "master_id": row["master_id"],
                        "minion_id": row["minion_id"],
                        "key_state_before": row["key_state"],
                        "key_state_after": row["key_state"],
                    }
                    for row in before
                ],
            }
    await connector.rpc(target, operator, SET_KEY_STATE_RPC, _set_kwarg(action, pairs, params))
    after, error = await _states_after_change(connector, operator, target, pairs)
    if after is None:
        return _unread_answer(action, before, error or "unknown error")
    keys = [
        {
            "master_id": old["master_id"],
            "minion_id": old["minion_id"],
            "key_state_before": old["key_state"],
            "key_state_after": new["key_state"],
        }
        for old, new in zip(before, after, strict=True)
    ]
    return {
        "status": "requested",
        "action": action,
        "reason": None,
        "changed_count": sum(
            1 for key in keys if key["key_state_before"] != key["key_state_after"]
        ),
        "keys": keys,
    }


register_preview_builder(DELETE_OP_ID, _key_delete_preview)
