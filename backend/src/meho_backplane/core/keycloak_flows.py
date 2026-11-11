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

A step with settings in the realm file (``authenticatorConfig``, such as
the level step of ``meho approval sign-in level``) gets them too: the stage
adds the settings a step lacks. A step whose settings have other values
than the file makes the flow one with other steps (#6281).

:func:`ensure_levels` adds the named sign-in levels of the realm file
(the realm attribute ``acr.loa.map``) to the realm. Names the realm has
already are kept, so a level another part added stays (#6281). It stops
before it writes when another name already has one of the levels, or when
a client bound to a flow of the realm file has its own ``acr.loa.map``.

:func:`stage_flows` picks the flows this stage makes: a top-level flow only
when a client of the realm file other than ``meho-web`` binds it. A login
flow of the console client (such as the passkey touch flow) is never made or
bound here; it has its own explicit step. :func:`bound_clients` names the
clients that bind a flow.

:func:`flow_overrides` turns a client's ``authenticationFlowBindingOverrides``
from the realm file (flow ids of the file) into flow ids of the realm.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
from meho_backplane.core.keycloak_levels import (
    LEVEL_MAP,
    client_level_map,
    level_conflict,
    parse_level_map,
)

__all__ = [
    "LEVEL_MAP",
    "NO_FLOW_RIGHT",
    "OVERRIDES",
    "bound_clients",
    "ensure_flows",
    "ensure_levels",
    "flow_overrides",
    "stage_flows",
]

#: The client field that binds a client to its own login flow.
OVERRIDES = "authenticationFlowBindingOverrides"

_BASE = f"/admin/realms/{REALM}/authentication"

#: The console client. The stage never makes or binds a login flow for it.
_WEB_CLIENT = "meho-web"

#: The settings of a step, by the alias the realm file gives them.
_Configs = Mapping[str, Mapping[str, Any]]

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


def _wanted_settings(flow: Mapping[str, Any], by_alias: Mapping[str, Any]) -> list[str | None]:
    """The settings alias of every step of *flow*, in the order of :func:`_wanted_rows`."""
    aliases: list[str | None] = []
    for step in _ordered(flow):
        if step.get("authenticatorFlow"):
            aliases.append(None)
            aliases += _wanted_settings(by_alias[step["flowAlias"]], by_alias)
        else:
            aliases.append(step.get("authenticatorConfig"))
    return aliases


async def _ensure_settings(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any], configs: _Configs
) -> str:
    """Give each step of *flow* the settings of the realm file.

    Runs on a flow whose steps match the file. A step without settings gets
    them. Returns ``"kept"`` when every step had them, ``"set"`` when some
    were added, and ``"differs"`` when a step has settings with other
    values than the file (they are not changed).
    """
    alias = flow["alias"]
    result = "kept"
    steps = await _list_steps(admin, alias)
    for step, name in zip(steps, _wanted_settings(flow, by_alias), strict=True):
        if name is None:
            continue
        want = {key: str(value) for key, value in (configs[name].get("config") or {}).items()}
        config_id = step.get("authenticationConfig")
        if config_id:
            reply = await admin.call("GET", f"{_BASE}/config/{quote(str(config_id), safe='')}")
            have = _expect(reply, 200, alias, f"cannot read the settings {name}").data
            if any((have.get("config") or {}).get(key) != value for key, value in want.items()):
                return "differs"
            continue
        _expect(
            await admin.call(
                "POST",
                f"{_BASE}/executions/{quote(str(step['id']), safe='')}/config",
                json={"alias": name, "config": want},
            ),
            201,
            alias,
            f"cannot add the settings {name} to login flow {alias}",
        )
        result = "set"
    return result


async def _finish_flow(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any], configs: _Configs
) -> str:
    """Finish the realm's *flow* so it has the steps of the realm file.

    Adds the steps the flow lacks, sets their requirements and adds the
    settings a step lacks: a run that stopped half-way leaves all three.
    Returns ``"kept"`` when the flow already matched the file,
    ``"completed"`` when it matches after this, and ``"differs"`` when it still has other steps
    than the file. A match is the whole list as Keycloak shows it: the same
    steps, in the same order, at the same level, with the same requirement.
    A step twice or a step in the wrong sub-flow is ``"differs"``, and so
    is a step whose settings have other values than the file.
    """
    wanted = _wanted_rows(flow, by_alias)
    have = await _read_rows(admin, flow["alias"])
    added = changed = False
    if have != wanted:
        added = await _add_missing_steps(admin, flow, by_alias, {name for _, name, _ in have})
        changed = await _set_requirements(admin, flow, by_alias)
        if added or changed:
            have = await _read_rows(admin, flow["alias"])
    if have != wanted:
        return "differs"
    settings = await _ensure_settings(admin, flow, by_alias, configs)
    if settings == "differs":
        return "differs"
    return "completed" if added or changed or settings == "set" else "kept"


async def _make_flow(
    admin: Admin, flow: Mapping[str, Any], by_alias: Mapping[str, Any], configs: _Configs
) -> str:
    """Make *flow* with its steps and their settings."""
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
    await _ensure_settings(admin, flow, by_alias, configs)
    return flow_id


async def ensure_flows(
    admin: Admin,
    flows: list[Mapping[str, Any]],
    settings: list[Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """Make the top-level login flows of the realm file that the realm lacks.

    A flow the realm has is finished when an earlier run left it half-made
    (:func:`_finish_flow`). Returns the flow id of the realm file -> the flow
    id in the realm, for every flow that has the steps of the file after
    this step. A flow with other steps is left out, so no client is bound
    to it. *settings* is the ``authenticatorConfig`` list of the realm
    file: the settings of the steps, by alias. Raises
    :class:`RealmStageError` (:data:`NO_FLOW_RIGHT`) when the account may
    not read or change login flows.
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
    configs = {item["alias"]: item for item in settings or []}
    ids: dict[str, str] = {}
    for flow in top:
        alias = flow["alias"]
        if alias in have:
            state = await _finish_flow(admin, flow, by_alias, configs)
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
            have[alias] = await _make_flow(admin, flow, by_alias, configs)
            _say(f"login flow {alias}: created")
        ids[flow["id"]] = have[alias]
    return ids


async def _check_client_levels(admin: Admin, clients: Sequence[str]) -> None:
    """Stop when one of *clients* has its own ``acr.loa.map``; a missing client is fine."""
    for client_id in clients:
        reply = await admin.call(
            "GET", f"/admin/realms/{REALM}/clients", params={"clientId": client_id}
        )
        if reply.status == 403:
            raise RealmStageError(f"sign-in levels: {NO_FLOW_RIGHT}")
        for client in expect(reply, 200, what=f"cannot read client {client_id}").data:
            own_map = client_level_map(client)
            if own_map is not None:
                raise RealmStageError(
                    f"sign-in levels: {own_map}, so Keycloak would not use the realm's "
                    f"levels for it. Remove the client's {LEVEL_MAP} in the Keycloak admin "
                    "console (the client's advanced settings) and run again"
                )


async def ensure_levels(
    admin: Admin, wanted: Mapping[str, Any], clients: Sequence[str] = ()
) -> None:
    """Add the named sign-in levels of the realm file to the realm (#6281).

    *wanted* is the ``attributes`` of the realm file. Its ``acr.loa.map``
    names levels, such as ``{"meho-approve": 2}``. A name the realm lacks,
    or has with another level, is set; other names of the realm are kept.
    Keycloak puts this name in the ``acr`` claim of a sign-in that reached
    the level. The stage stops before it writes when another name of the
    realm already has one of the levels, or when one of *clients* (the
    clients the realm file binds to a flow) has its own ``acr.loa.map``:
    the ``acr`` claim would then not match the levels of the realm file.
    Changing the realm needs ``manage-realm``; without it the stage stops
    with :data:`NO_FLOW_RIGHT`.
    """
    levels = json.loads(wanted.get(LEVEL_MAP) or "{}")
    if not levels:
        return
    await _check_client_levels(admin, clients)
    url = f"/admin/realms/{REALM}"
    reply = await admin.call("GET", url)
    if reply.status == 403:
        raise RealmStageError(f"sign-in levels: {NO_FLOW_RIGHT}")
    attributes = dict(
        expect(reply, 200, what=f"cannot read realm {REALM}").data.get("attributes") or {}
    )
    have = parse_level_map(attributes.get(LEVEL_MAP))
    if have is None:
        raise RealmStageError(
            f"the realm attribute {LEVEL_MAP} is not a JSON object; fix it in the "
            "Keycloak admin console (Realm settings, Sign-in levels) and run again"
        )
    conflict = level_conflict(have, levels)
    if conflict is not None:
        raise RealmStageError(
            f"sign-in levels: {conflict}. Give that name another level in the Keycloak "
            "admin console (Realm settings, Sign-in levels) and run again"
        )
    if all(have.get(name) == level for name, level in levels.items()):
        _say(f"sign-in levels: kept ({', '.join(sorted(levels))})")
        return
    attributes[LEVEL_MAP] = json.dumps({**have, **levels}, separators=(",", ":"))
    reply = await admin.call("PUT", url, json={"attributes": attributes})
    if reply.status == 403:
        raise RealmStageError(f"sign-in levels: {NO_FLOW_RIGHT}")
    expect(reply, 204, what=f"cannot set the sign-in levels of realm {REALM}")
    _say(f"sign-in levels: set ({', '.join(sorted(levels))})")


def bound_clients(clients: Sequence[Mapping[str, Any]]) -> list[str]:
    """The ``clientId`` of every client of the realm file that binds a login flow."""
    return [client["clientId"] for client in clients if client.get(OVERRIDES)]


def stage_flows(
    flows: Sequence[Mapping[str, Any]], clients: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """The login flows of the realm file that this stage makes and checks.

    A top-level flow is kept only when a client of the realm file other
    than ``meho-web`` binds it; sub-flows are kept. A flow left out gets one
    line: no client is bound to it by this stage.
    """
    bound = {
        flow_id
        for client in clients
        if client.get("clientId") != _WEB_CLIENT
        for flow_id in (client.get(OVERRIDES) or {}).values()
    }
    kept: list[Mapping[str, Any]] = []
    for flow in flows:
        if flow.get("topLevel") and flow.get("id") not in bound:
            _say(
                f"login flow {flow['alias']}: not made here (no client of the realm file "
                f"but {_WEB_CLIENT} binds it); no client is bound to it by this stage"
            )
            continue
        kept.append(flow)
    return kept


def flow_overrides(want: Mapping[str, Any], flow_ids: Mapping[str, str]) -> dict[str, str]:
    """The client's flow bindings with realm flow ids; unknown flows are left out."""
    return {
        binding: flow_ids[file_id]
        for binding, file_id in (want.get(OVERRIDES) or {}).items()
        if file_id in flow_ids
    }
