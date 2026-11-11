# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login flows of the realm file, for the realm stage (#5515).

Keycloak makes the flows of the realm file at its first start. On a realm
made before a flow was in the file, :func:`ensure_flows` makes it through
the Admin REST API: the flow, its steps and their requirements. A run that
stops half-way (a timeout, a restart) can leave a flow with missing steps
or steps that are still off. The next run finishes such a flow: it adds the
missing steps and sets the requirements of the realm file. A flow with
other steps than the file is kept as it is, the stage says so in one line,
and no client is bound to it: a flow that is not the one of the file must
not guard the approval sign-in.

Reading and changing login flows needs the ``manage-realm`` role. The
``meho-admin`` account of the realm file has it. An install made before the
realm file gave it to ``meho-admin`` may lack it (docs/codebase/core-bootstrap.md,
"Rights"). Then the stage stops with :data:`NO_FLOW_RIGHT`, which names the
role and the one-time fix: without the role, the stage cannot make sure that
the approval sign-in has its own flow (#6280).

:func:`flow_overrides` turns a client's ``authenticationFlowBindingOverrides``
from the realm file (flow ids of the file) into flow ids of the realm.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from meho_backplane.core.keycloak_api import (
    REALM,
    Admin,
    RealmStageError,
    Reply,
    expect,
    id_from,
)

__all__ = ["NO_FLOW_RIGHT", "OVERRIDES", "ensure_flows", "flow_overrides"]

#: The client field that binds a client to its own login flow.
OVERRIDES = "authenticationFlowBindingOverrides"

_BASE = f"/admin/realms/{REALM}/authentication"

#: Why the stage stops when ``meho-admin`` may not read or change login flows.
NO_FLOW_RIGHT = (
    "meho-admin may not read or change login flows, so the stage cannot keep the "
    "approval sign-in flow up to date. Give meho-admin the realm-management role "
    "manage-realm once, then run the upgrade again; see docs-site/install/upgrades.md, "
    "'The bootstrap Job needs manage-realm'"
)


def _no_right(alias: str) -> RealmStageError:
    return RealmStageError(f"login flow {alias}: {NO_FLOW_RIGHT}")


def _expect(reply: Reply, status: int, alias: str, what: str) -> Reply:
    """:func:`expect` for a call to the flow *alias*; HTTP 403 names the missing role."""
    if reply.status == 403:
        raise _no_right(alias)
    return expect(reply, status, what=what)


#: One step of a flow as Keycloak lists it: its level, its name, its requirement.
_Row = tuple[int, str, str]


def _say(line: str) -> None:
    print(line, flush=True)


def _flow_url(alias: str) -> str:
    return f"{_BASE}/flows/{quote(alias, safe='')}/executions"


def _ordered(flow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The steps of *flow* in the realm file, in the order Keycloak runs them."""
    return sorted(flow.get("authenticationExecutions") or [], key=lambda s: s.get("priority", 0))


def _requirements(flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> dict[str, str]:
    """The requirement of every step of *flow* and its sub-flows, by step name."""
    return {name: requirement for _, name, requirement in _wanted_rows(flow, by_alias)}


def _wanted_rows(
    flow: Mapping[str, Any], by_alias: Mapping[str, Any], level: int = 0
) -> list[_Row]:
    """The steps of *flow* as Keycloak lists them: a sub-flow's steps follow it."""
    rows: list[_Row] = []
    for step in _ordered(flow):
        if step.get("authenticatorFlow"):
            rows.append((level, step["flowAlias"], step["requirement"]))
            rows += _wanted_rows(by_alias[step["flowAlias"]], by_alias, level + 1)
        else:
            rows.append((level, step["authenticator"], step["requirement"]))
    return rows


async def _add_missing_steps(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any], have: set[str]
) -> bool:
    """Add the steps of the realm file that *flow* lacks; whether any was added.

    *have* names the steps the realm's flow has (sub-flows by alias, other
    steps by provider). Keycloak puts a new step after the existing ones,
    so a run that stopped half-way, which leaves the first steps, gets the
    rest in the order of the file.
    """
    added = False
    for step in _ordered(flow):
        if step.get("authenticatorFlow"):
            sub = by_alias[step["flowAlias"]]
            if sub["alias"] not in have:
                body = {
                    "alias": sub["alias"],
                    "type": sub["providerId"],
                    "description": sub.get("description", ""),
                }
                _expect(
                    await admin.call("POST", f"{_flow_url(flow['alias'])}/flow", json=body),
                    201,
                    flow["alias"],
                    f"cannot add the sub-flow {sub['alias']} to login flow {flow['alias']}",
                )
                added = True
            added = await _add_missing_steps(admin, sub, by_alias, have) or added
        elif step["authenticator"] not in have:
            _expect(
                await admin.call(
                    "POST",
                    f"{_flow_url(flow['alias'])}/execution",
                    json={"provider": step["authenticator"]},
                ),
                201,
                flow["alias"],
                f"cannot add step {step['authenticator']} to login flow {flow['alias']}",
            )
            added = True
    return added


def _step_name(step: Mapping[str, Any]) -> str:
    """The name of a listed step: the alias of a sub-flow, else the step's provider."""
    name = step.get("displayName") if step.get("authenticationFlow") else step.get("providerId")
    return str(name)


async def _list_steps(admin: Admin, alias: str) -> list[Mapping[str, Any]]:
    """The steps of the realm's flow *alias*, as Keycloak lists them."""
    reply = await admin.call("GET", _flow_url(alias))
    steps: list[Mapping[str, Any]] = _expect(
        reply, 200, alias, f"cannot list the steps of login flow {alias}"
    ).data
    return steps


async def _read_rows(admin: Admin, alias: str) -> list[_Row]:
    """The steps of the realm's flow *alias* in their order, with level and requirement."""
    return [
        (step.get("level", -1), _step_name(step), str(step.get("requirement")))
        for step in await _list_steps(admin, alias)
    ]


async def _set_requirements(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]
) -> bool:
    """Set the requirement of the realm file on every step; whether any changed."""
    wanted = _requirements(flow, by_alias)
    alias = flow["alias"]
    changed = False
    for step in await _list_steps(admin, alias):
        name = _step_name(step)
        want = wanted.get(name)
        if want is None or step.get("requirement") == want:
            continue
        _expect(
            await admin.call("PUT", _flow_url(alias), json={**step, "requirement": want}),
            204,
            alias,
            f"cannot set the requirement of step {name} in login flow {alias}",
        )
        changed = True
    return changed


async def _finish_flow(admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> str:
    """Finish the realm's *flow* so it has the steps of the realm file.

    Adds the steps the flow lacks and sets their requirements: a run that
    stopped half-way leaves both. Returns ``"kept"`` when the flow already
    matched the file, ``"completed"`` when it
    matches after this, and ``"differs"`` when it still has other steps
    than the file. A match is the whole list as Keycloak shows it: the same
    steps, in the same order, at the same level, with the same requirement.
    A step twice or a step in the wrong sub-flow is ``"differs"``.
    """
    wanted = _wanted_rows(flow, by_alias)
    have = await _read_rows(admin, flow["alias"])
    if have == wanted:
        return "kept"
    added = await _add_missing_steps(admin, flow, by_alias, {name for _, name, _ in have})
    changed = await _set_requirements(admin, flow, by_alias)
    if added or changed:
        have = await _read_rows(admin, flow["alias"])
    return "completed" if have == wanted else "differs"


async def _make_flow(admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> str:
    """Make *flow* with its steps."""
    body = {
        "alias": flow["alias"],
        "description": flow.get("description", ""),
        "providerId": flow["providerId"],
        "topLevel": True,
        "builtIn": False,
    }
    reply = await admin.call("POST", f"{_BASE}/flows", json=body)
    flow_id = id_from(
        _expect(reply, 201, flow["alias"], f"cannot create login flow {flow['alias']}")
    )
    await _add_missing_steps(admin, flow, by_alias, set())
    await _set_requirements(admin, flow, by_alias)
    return flow_id


async def ensure_flows(admin: Admin, flows: list[Mapping[str, Any]]) -> dict[str, str]:
    """Make the top-level login flows of the realm file that the realm lacks.

    A flow the realm has is finished when an earlier run left it half-made
    (:func:`_finish_flow`). Returns the flow id of the realm file -> the flow
    id in the realm, for every flow that has the steps of the file after
    this step. A flow with other steps is left out, so no client is bound
    to it. Raises :class:`RealmStageError` (:data:`NO_FLOW_RIGHT`) when the
    account may not read or change login flows.
    """
    top = [flow for flow in flows if flow.get("topLevel")]
    if not top:
        return {}
    reply = await admin.call("GET", f"{_BASE}/flows")
    if reply.status == 403:
        raise _no_right(top[0]["alias"])
    have = {
        item["alias"]: item["id"]
        for item in expect(reply, 200, what="cannot list the login flows").data
    }
    by_alias = {flow["alias"]: flow for flow in flows}
    ids: dict[str, str] = {}
    for flow in top:
        alias = flow["alias"]
        if alias in have:
            state = await _finish_flow(admin, flow, by_alias)
            if state == "differs":
                _say(
                    f"login flow {alias}: kept, but its steps differ from the realm file; "
                    "no client is bound to it"
                )
                continue
            if state == "completed":
                _say(f"login flow {alias}: completed (an earlier run had not finished it)")
            else:
                _say(f"login flow {alias}: kept")
        else:
            have[alias] = await _make_flow(admin, flow, by_alias)
            _say(f"login flow {alias}: created")
        ids[flow["id"]] = have[alias]
    return ids


def flow_overrides(want: Mapping[str, Any], flow_ids: Mapping[str, str]) -> dict[str, str]:
    """The client's flow bindings with realm flow ids; unknown flows are left out."""
    return {
        binding: flow_ids[file_id]
        for binding, file_id in (want.get(OVERRIDES) or {}).items()
        if file_id in flow_ids
    }
