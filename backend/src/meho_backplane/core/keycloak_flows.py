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
``meho-admin`` account of the realm file has it only when the company login
is on (docs/codebase/core-bootstrap.md, "Rights"). Without it the stage says
so in one line and goes on.

:func:`flow_overrides` turns a client's ``authenticationFlowBindingOverrides``
from the realm file (flow ids of the file) into flow ids of the realm.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from meho_backplane.core.keycloak_api import REALM, Admin, expect, id_from

__all__ = ["OVERRIDES", "ensure_flows", "flow_overrides"]

#: The client field that binds a client to its own login flow.
OVERRIDES = "authenticationFlowBindingOverrides"

_BASE = f"/admin/realms/{REALM}/authentication"


def _say(line: str) -> None:
    print(line, flush=True)


def _flow_url(alias: str) -> str:
    return f"{_BASE}/flows/{quote(alias, safe='')}/executions"


def _requirements(flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> dict[str, str]:
    """The requirement of every step of *flow* and its sub-flows, by step name."""
    wanted: dict[str, str] = {}
    for step in flow.get("authenticationExecutions") or []:
        if step.get("authenticatorFlow"):
            wanted[step["flowAlias"]] = step["requirement"]
            wanted.update(_requirements(by_alias[step["flowAlias"]], by_alias))
        else:
            wanted[step["authenticator"]] = step["requirement"]
    return wanted


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
    steps = sorted(flow.get("authenticationExecutions") or [], key=lambda s: s.get("priority", 0))
    for step in steps:
        if step.get("authenticatorFlow"):
            sub = by_alias[step["flowAlias"]]
            if sub["alias"] not in have:
                body = {
                    "alias": sub["alias"],
                    "type": sub["providerId"],
                    "description": sub.get("description", ""),
                }
                expect(
                    await admin.call("POST", f"{_flow_url(flow['alias'])}/flow", json=body),
                    201,
                    what=f"cannot add the sub-flow {sub['alias']} to login flow {flow['alias']}",
                )
                added = True
            added = await _add_missing_steps(admin, sub, by_alias, have) or added
        elif step["authenticator"] not in have:
            expect(
                await admin.call(
                    "POST",
                    f"{_flow_url(flow['alias'])}/execution",
                    json={"provider": step["authenticator"]},
                ),
                201,
                what=f"cannot add step {step['authenticator']} to login flow {flow['alias']}",
            )
            added = True
    return added


def _step_name(step: Mapping[str, Any]) -> str:
    """The name of a listed step: the alias of a sub-flow, else the step's provider."""
    name = step.get("displayName") if step.get("authenticationFlow") else step.get("providerId")
    return str(name)


async def _read_steps(admin: Admin, alias: str) -> dict[str, str] | None:
    """The requirement of every step of the realm's flow *alias*, by step name.

    ``None`` when the account may not read them.
    """
    reply = await admin.call("GET", _flow_url(alias))
    if reply.status == 403:
        return None
    steps = expect(reply, 200, what=f"cannot list the steps of login flow {alias}").data
    return {_step_name(step): step.get("requirement") for step in steps}


async def _set_requirements(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]
) -> bool:
    """Set the requirement of the realm file on every step; whether any changed."""
    wanted = _requirements(flow, by_alias)
    alias = flow["alias"]
    steps = expect(
        await admin.call("GET", _flow_url(alias)),
        200,
        what=f"cannot list the steps of login flow {alias}",
    ).data
    changed = False
    for step in steps:
        name = _step_name(step)
        want = wanted.get(name)
        if want is None or step.get("requirement") == want:
            continue
        expect(
            await admin.call("PUT", _flow_url(alias), json={**step, "requirement": want}),
            204,
            what=f"cannot set the requirement of step {name} in login flow {alias}",
        )
        changed = True
    return changed


async def _finish_flow(admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> str:
    """Finish the realm's *flow* so it has the steps of the realm file.

    Adds the steps the flow lacks and sets their requirements: a run that
    stopped half-way leaves both. Returns ``"kept"`` when the flow already
    matched the file (or its steps cannot be read), ``"completed"`` when it
    matches after this, and ``"differs"`` when it still has other steps
    than the file.
    """
    wanted = _requirements(flow, by_alias)
    have = await _read_steps(admin, flow["alias"])
    if have is None or have == wanted:
        return "kept"
    added = await _add_missing_steps(admin, flow, by_alias, set(have))
    changed = await _set_requirements(admin, flow, by_alias)
    if added or changed:
        have = await _read_steps(admin, flow["alias"])
    return "completed" if have == wanted else "differs"


async def _make_flow(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any]
) -> str | None:
    """Make *flow* with its steps; ``None`` when the account may not."""
    body = {
        "alias": flow["alias"],
        "description": flow.get("description", ""),
        "providerId": flow["providerId"],
        "topLevel": True,
        "builtIn": False,
    }
    reply = await admin.call("POST", f"{_BASE}/flows", json=body)
    if reply.status == 403:
        return None
    flow_id = id_from(expect(reply, 201, what=f"cannot create login flow {flow['alias']}"))
    await _add_missing_steps(admin, flow, by_alias, set())
    await _set_requirements(admin, flow, by_alias)
    return flow_id


async def ensure_flows(admin: Admin, flows: list[Mapping[str, Any]]) -> dict[str, str]:
    """Make the top-level login flows of the realm file that the realm lacks.

    A flow the realm has is finished when an earlier run left it half-made
    (:func:`_finish_flow`). Returns the flow id of the realm file -> the flow
    id in the realm, for every flow that has the steps of the file after
    this step. A flow with other steps is left out, so no client is bound
    to it.
    """
    top = [flow for flow in flows if flow.get("topLevel")]
    if not top:
        return {}
    reply = await admin.call("GET", f"{_BASE}/flows")
    if reply.status == 403:
        for flow in top:
            _say(f"login flow {flow['alias']}: not checked (no right to read login flows)")
        return {}
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
            made = await _make_flow(admin, flow, by_alias)
            if made is None:
                _say(f"login flow {alias}: not made (no right to change login flows)")
                continue
            have[alias] = made
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
