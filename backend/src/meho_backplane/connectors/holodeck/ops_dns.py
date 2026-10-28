# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DNS name operations for the Holodeck 9.0 lab router (#4066).

A Holodeck 9.0 router runs its DNS service as dnsmasq inside its own
Kubernetes. The names of the lab's servers live in one value: the
ConfigMap ``default/dnsmasq``, key ``hosts``, one ``<address> <name>``
line per server. dnsmasq answers the reverse question (address to name)
from the same line.

============================  =========  ==================================
op_id                         safety     what it does
============================  =========  ==================================
``holodeck.dns.host.list``    safe       list the lines of the ``hosts`` key
``holodeck.dns.host.add``     caution    add one ``<address> <name>`` line
``holodeck.dns.host.remove``  dangerous  remove one ``<address> <name>`` line
============================  =========  ==================================

Both writes wait for a person to approve them. Each write:

1. reads the live ConfigMap and plans the change in Python;
2. saves the ConfigMap to ``/root/dnsmasq-cm-pre-<op>-<time>.yaml``;
3. writes only the ``hosts`` key with a JSON Patch whose first step
   (``test``) checks that the key still holds the value MEHO read, so a
   change made by someone else in between is never overwritten;
4. reads the ConfigMap again and checks the new value and that no other
   key changed;
5. restarts the dnsmasq deployment (the pods read the file only at start),
   waits for the rollout and then until no old pod is left;
6. asks every dnsmasq pod for the name and the address (``dig``).

Adding a line that is already there, or removing one that is not there,
changes nothing (no save, no restart) and reports ``unchanged`` /
``absent``. A name or address that is already used with another partner
is refused.

Shell safety: every command is a fixed ``kubectl`` / ``dig`` template.
The only inputs are an IP address (checked with :mod:`ipaddress`) and a
DNS name (checked against a label pattern). The JSON Patch is built by
:func:`json.dumps` and passed as one :func:`shlex.quote`-d argument.

References: kubectl patch (``--type json`` is RFC 6902 JSON Patch,
including the ``test`` operation)
https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/ ,
RFC 6902 section 4.6 https://www.rfc-editor.org/rfc/rfc6902#section-4.6 .
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.holodeck._response_schemas import HOLODECK_RESPONSE_SCHEMAS
from meho_backplane.connectors.holodeck.dns_hosts import (
    DEPLOYMENT,
    NAME_MAX,
    HolodeckDnsError,
    bound_dns_entry,
    canonical_address,
    dig_answers,
    dnsmasq_pods,
    parse_hosts,
    plan_add,
    plan_remove,
    public_rows,
)
from meho_backplane.connectors.holodeck.ops import SSH_TRANSPORT_NOTE, HolodeckOp
from meho_backplane.connectors.holodeck.ops_write import _run_text

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "DNS_OPS",
    "HOLODECK_WHEN_TO_USE_DNS_BY_GROUP",
    "holodeck_dns_host_add",
    "holodeck_dns_host_list",
    "holodeck_dns_host_remove",
]

_GET_CM_JSON = "kubectl get configmap dnsmasq -n default -o json"
_ROLLOUT_RESTART = f"kubectl rollout restart deployment/{DEPLOYMENT} -n default"
_ROLLOUT_STATUS = f"kubectl rollout status deployment/{DEPLOYMENT} -n default --timeout=180s"
_GET_PODS_JSON = "kubectl get pods -n default -o json"

#: How long to wait for the old dnsmasq pods to go after the rollout.
_POD_WAIT_INTERVAL_S: float = 2.0
_POD_WAIT_ATTEMPTS: int = 45


# ---------------------------------------------------------------------------
# Router steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Router:
    """The connector, target and operator one op call runs against."""

    connector: HolodeckConnector
    target: Any
    operator: Operator | None

    async def run(self, cmd: str) -> dict[str, Any]:
        """Run *cmd*; return ``{stdout, stderr, exit_status}``."""
        return await _run_text(self.connector, self.target, cmd, self.operator)

    async def step(self, cmd: str, step: str) -> str:
        """Run *cmd*; return stdout, or raise :class:`HolodeckDnsError` on failure."""
        try:
            outcome = await self.run(cmd)
        except Exception as exc:
            raise HolodeckDnsError(step, str(exc)) from exc
        if outcome["exit_status"] != 0:
            detail = outcome["stderr"].strip()[:500] or f"exit status {outcome['exit_status']}"
            raise HolodeckDnsError(step, detail)
        return str(outcome["stdout"])

    async def read_configmap(self) -> dict[str, Any]:
        """Return the ConfigMap's ``data``; the ``hosts`` key must be a string."""
        stdout = await self.step(_GET_CM_JSON, "read")
        try:
            data = json.loads(stdout).get("data") or {}
        except (ValueError, AttributeError):
            raise HolodeckDnsError("read", "the ConfigMap answer is not JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("hosts"), str):
            raise HolodeckDnsError("read", "ConfigMap default/dnsmasq has no 'hosts' key")
        return data

    async def pods(self, step: str) -> list[dict[str, Any]]:
        """Return the dnsmasq pods (see :func:`dnsmasq_pods`)."""
        stdout = await self.step(_GET_PODS_JSON, step)
        try:
            return dnsmasq_pods(json.loads(stdout))
        except ValueError:
            raise HolodeckDnsError(step, "the pod list answer is not JSON") from None


async def _wait_for_new_pods(router: _Router) -> None:
    """Wait until all dnsmasq pods share one template and none is shutting down.

    ``kubectl rollout status`` returns while an old pod may still answer
    with the old file, so MEHO waits for the old pods to be gone.
    """
    for attempt in range(_POD_WAIT_ATTEMPTS):
        pods = await router.pods("wait")
        if pods and len({p["hash"] for p in pods}) == 1 and not any(p["deleting"] for p in pods):
            return
        if attempt + 1 < _POD_WAIT_ATTEMPTS:
            await asyncio.sleep(_POD_WAIT_INTERVAL_S)
    raise HolodeckDnsError("wait", "old dnsmasq pods were still running after the wait")


async def _check_pod(
    router: _Router, pod: dict[str, Any], entry: tuple[str, str], expect_present: bool
) -> dict[str, Any]:
    """Ask one pod for the name and the address; compare with what we expect."""
    address, name = entry
    result: dict[str, Any] = {"pod": pod["name"], "pod_ip": pod["ip"], "forward": [], "reverse": []}
    try:
        server = str(ipaddress.ip_address(pod["ip"] or ""))
    except ValueError:
        return {**result, "ok": False, "error": "pod has no IP address yet"}
    rtype = "AAAA" if ":" in address else "A"
    base = f"dig +short +time=2 +tries=1 @{shlex.quote(server)}"
    forward = await router.run(f"{base} {shlex.quote(name)} {rtype}")
    reverse = await router.run(f"{base} -x {shlex.quote(address)}")
    result["forward"] = dig_answers(forward["stdout"])
    result["reverse"] = [a.rstrip(".").lower() for a in dig_answers(reverse["stdout"])]
    if forward["exit_status"] != 0 or reverse["exit_status"] != 0:
        return {**result, "ok": False, "error": "dig failed on the router"}
    present_fwd = address in {canonical_address(a) for a in result["forward"]}
    present_rev = name in result["reverse"]
    if expect_present:
        result["ok"] = present_fwd and present_rev
    else:
        result["ok"] = not present_fwd and not present_rev
    return result


async def _check_all(
    router: _Router, entry: tuple[str, str], expect_present: bool
) -> list[dict[str, Any]]:
    """Ask every running dnsmasq pod for the name and the address."""
    pods = [p for p in await router.pods("check") if not p["deleting"]]
    if not pods:
        raise HolodeckDnsError("check", "no running dnsmasq pod found")
    return [await _check_pod(router, p, entry, expect_present) for p in pods]


async def _write_hosts(
    router: _Router, before: dict[str, Any], new_hosts: str, result: dict[str, Any]
) -> None:
    """Save, patch, re-read, restart and wait. Fills *result* as it goes."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = f"/root/dnsmasq-cm-pre-{result['op']}-{stamp}.yaml"
    quoted = shlex.quote(backup)
    await router.step(
        f"kubectl get configmap dnsmasq -n default -o yaml > {quoted} && test -s {quoted}",
        "save",
    )
    result["backup_path"] = backup
    patch = json.dumps(
        [
            {"op": "test", "path": "/data/hosts", "value": before["hosts"]},
            {"op": "replace", "path": "/data/hosts", "value": new_hosts},
        ]
    )
    await router.step(
        f"kubectl patch configmap dnsmasq -n default --type json -p {shlex.quote(patch)}",
        "write",
    )
    result["changed"] = True
    after = await router.read_configmap()
    result["lines_after"] = len(parse_hosts(after["hosts"]))
    others_before = {k: v for k, v in before.items() if k != "hosts"}
    others_after = {k: v for k, v in after.items() if k != "hosts"}
    if after["hosts"] != new_hosts or others_before != others_after:
        raise HolodeckDnsError("verify", "the ConfigMap after the write is not what MEHO wrote")
    await router.step(_ROLLOUT_RESTART, "restart")
    await router.step(_ROLLOUT_STATUS, "restart")
    result["restarted"] = True
    await _wait_for_new_pods(router)


async def _change_entry(router: _Router, params: dict[str, Any], *, adding: bool) -> dict[str, Any]:
    """Shared body of ``holodeck.dns.host.add`` and ``holodeck.dns.host.remove``."""
    result: dict[str, Any] = {
        "op": "dns-host-add" if adding else "dns-host-remove",
        "state": None,
        "changed": False,
        "address": None,
        "name": None,
        "backup_path": None,
        "lines_before": None,
        "lines_after": None,
        "restarted": False,
        "checks": [],
        "verified": False,
        "error": None,
    }
    try:
        entry = bound_dns_entry(params)
        result["address"], result["name"] = entry
        before = await router.read_configmap()
        result["lines_before"] = len(parse_hosts(before["hosts"]))
        plan = plan_add if adding else plan_remove
        new_hosts = plan(before["hosts"], *entry)
        if new_hosts is None:
            result["state"] = "unchanged" if adding else "absent"
            result["lines_after"] = result["lines_before"]
        else:
            await _write_hosts(router, before, new_hosts, result)
            result["state"] = "added" if adding else "removed"
        result["checks"] = await _check_all(router, entry, adding)
        result["verified"] = all(c["ok"] for c in result["checks"])
    except HolodeckDnsError as exc:
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def holodeck_dns_host_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """List the lines of the ``hosts`` key of ConfigMap ``default/dnsmasq``.

    Op-id: ``holodeck.dns.host.list``. Safe read. Returns
    ``{rows: [{address, names}], total}``; on failure
    ``{rows: [], total: 0, error}``.
    """
    try:
        data = await _Router(self, target, operator).read_configmap()
    except HolodeckDnsError as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    rows = public_rows(parse_hosts(data["hosts"]))
    return {"rows": rows, "total": len(rows)}


async def holodeck_dns_host_add(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Add one ``<address> <name>`` line. Op-id: ``holodeck.dns.host.add``."""
    return await _change_entry(_Router(self, target, operator), params, adding=True)


async def holodeck_dns_host_remove(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Remove one ``<address> <name>`` line. Op-id: ``holodeck.dns.host.remove``."""
    return await _change_entry(_Router(self, target, operator), params, adding=False)


# ---------------------------------------------------------------------------
# Group texts and op metadata
# ---------------------------------------------------------------------------

HOLODECK_WHEN_TO_USE_DNS_BY_GROUP: dict[str, str] = {
    "dns": (
        "DNS names of a Holodeck 9.0 lab (dnsmasq on the HoloRouter): "
        "holodeck.dns.host.list lists the '<address> <name>' lines. Changes are "
        "in the dns-write group."
    ),
    "dns-write": (
        "Add or remove one DNS name of a Holodeck 9.0 lab (dnsmasq on the "
        "HoloRouter): holodeck.dns.host.add and holodeck.dns.host.remove. Each "
        "saves the old list, restarts the DNS pods and checks the name both "
        "ways. A person must approve each call."
    ),
}

_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {
            "type": "string",
            "minLength": 2,
            "maxLength": 45,
            "pattern": r"^[0-9A-Fa-f:.]+$",
            "description": "IPv4 or IPv6 address of the server, for example 192.0.2.10.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": NAME_MAX,
            "pattern": r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$",
            "description": (
                "DNS name of the server, for example appliance01.example.test. "
                "No trailing dot. MEHO writes it in lower case."
            ),
        },
    },
    "required": ["address", "name"],
    "additionalProperties": False,
    "examples": [{"address": "192.0.2.10", "name": "appliance01.example.test"}],
}

_WRITE_OUTPUT = (
    "{op, state, changed, address, name, backup_path, lines_before, lines_after, "
    "restarted, checks: [{pod, pod_ip, forward, reverse, ok}], verified, error}. "
)

DNS_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id="holodeck.dns.host.list",
        handler_attr="dns_host_list",
        summary="List the DNS name lines of a Holodeck 9.0 lab (dnsmasq hosts on the HoloRouter).",
        description=(
            "Reads the 'hosts' key of the ConfigMap default/dnsmasq on the "
            "HoloRouter's Kubernetes and returns one row per '<address> <name>' "
            "line. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema=HOLODECK_RESPONSE_SCHEMAS["holodeck.dns.host.list"],
        group_key="dns",
        tags=("read-only", "dns", "dnsmasq", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to see which DNS names a Holodeck 9.0 lab router serves, "
                "for example before adding a name for a new appliance. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{rows: [{address, names}], total}. On failure: {rows: [], total: 0, error}."
            ),
            "next_step": "To add or remove a name, call holodeck.dns.host.add or .remove.",
        },
    ),
    HolodeckOp(
        op_id="holodeck.dns.host.add",
        handler_attr="dns_host_add",
        summary="Add one DNS name (address and name) to a Holodeck 9.0 lab router's dnsmasq.",
        description=(
            "Adds one '<address> <name>' line to the 'hosts' key of ConfigMap "
            "default/dnsmasq: saves the ConfigMap, changes only that key, restarts "
            "the dnsmasq pods and checks the name both ways. The same line again "
            "changes nothing. A person must approve each call."
        ),
        parameter_schema=_PARAMS,
        response_schema=HOLODECK_RESPONSE_SCHEMAS["holodeck.dns.host.add"],
        group_key="dns-write",
        tags=("write", "dns", "dnsmasq", "holodeck"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call before the first start of a new appliance in a Holodeck 9.0 "
                "lab, so its name resolves both ways. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. The name and the address must "
                "not be used with another partner yet; check with "
                "holodeck.dns.host.list. The call restarts the lab's DNS pods."
            ),
            "parameter_hints": {
                "address": "The appliance's IP address.",
                "name": "The appliance's full DNS name, no trailing dot.",
            },
            "output_shape": _WRITE_OUTPUT
            + (
                "state is 'added' or 'unchanged' (the line was already there). "
                "When refused or failed: error says which step."
            ),
            "next_step": (
                "Check verified is true. To undo, call holodeck.dns.host.remove "
                "with the same address and name."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.dns.host.remove",
        handler_attr="dns_host_remove",
        summary="Remove one DNS name (address and name) from a Holodeck 9.0 lab router's dnsmasq.",
        description=(
            "Removes the '<address> <name>' line from the 'hosts' key of ConfigMap "
            "default/dnsmasq: saves the ConfigMap, changes only that key, restarts "
            "the dnsmasq pods and checks the name is gone. A missing line answers "
            "'absent'. A person must approve each call."
        ),
        parameter_schema=_PARAMS,
        response_schema=HOLODECK_RESPONSE_SCHEMAS["holodeck.dns.host.remove"],
        group_key="dns-write",
        tags=("write", "dns", "dnsmasq", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call when an appliance is removed from a Holodeck 9.0 lab, or to "
                "undo holodeck.dns.host.add. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. Only a line that is exactly "
                "'<address> <name>' is removed; other lines with the name or the "
                "address are refused. The call restarts the lab's DNS pods."
            ),
            "parameter_hints": {
                "address": "The address of the line to remove.",
                "name": "The name of the line to remove.",
            },
            "output_shape": _WRITE_OUTPUT
            + (
                "state is 'removed' or 'absent' (the line was not there). When "
                "refused or failed: error says which step."
            ),
            "next_step": "Check verified is true, then list the lines with holodeck.dns.host.list.",
        },
    ),
)
