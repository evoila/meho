# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Holodeck 9.0 DNS name ops (#4066).

* Pure helpers: input checks, hosts parsing, add / remove plans.
* Op metadata: the read is safe; both writes wait for approval.
* Handlers against :class:`_FakeRouter`, a small stand-in for the router
  that plays ``kubectl`` (ConfigMap, rollout, pods) and ``dig``. Each fake
  pod keeps the hosts value it read at start, like the real pods (they
  mount the file with ``subPath``).
* Dispatch through the recorded fake-shell SSH server: the read runs
  without approval, a write waits for approval, and the approved write runs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import jsonschema
import pytest

import meho_backplane.connectors.holodeck  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.holodeck import HolodeckConnector, ops_dns
from meho_backplane.connectors.holodeck.dns_hosts import (
    HolodeckDnsError,
    bound_dns_entry,
    parse_hosts,
    plan_add,
    plan_remove,
)
from meho_backplane.connectors.holodeck.ops import HOLODECK_OPS
from meho_backplane.connectors.holodeck.ops_dns import DNS_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(ops_dns, "_POD_WAIT_INTERVAL_S", 0.0)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_HOSTS = "192.0.2.1 router.example.test\n192.0.2.2 dc01.example.test\n"
_NEW = {"address": "192.0.2.10", "name": "appliance01.example.test"}
_OTHER_KEYS = {"dnsmasq.conf": "domain=example.test\n", "dhcp": "dhcp-range=192.0.2.50\n"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (_NEW, ("192.0.2.10", "appliance01.example.test")),
        (
            {"address": "192.0.2.10", "name": "App-01.Example.TEST"},
            ("192.0.2.10", "app-01.example.test"),
        ),
        (
            {"address": "2001:DB8::0:1", "name": "v6.example.test"},
            ("2001:db8::1", "v6.example.test"),
        ),
        ({"address": "192.0.2.10", "name": "single"}, ("192.0.2.10", "single")),
    ],
)
def test_bound_dns_entry_accepts(params: dict[str, Any], expected: tuple[str, str]) -> None:
    assert bound_dns_entry(params) == expected


@pytest.mark.parametrize(
    "params",
    [
        {"address": "192.0.2.300", "name": "a.example.test"},
        {"address": "0.0.0.0", "name": "a.example.test"},
        {"address": "224.0.0.1", "name": "a.example.test"},
        {"address": "192.0.2.10; reboot", "name": "a.example.test"},
        {"address": "192.0.2.10", "name": "a.example.test; reboot"},
        {"address": "192.0.2.10", "name": "$(reboot).example.test"},
        {"address": "192.0.2.10", "name": "a example.test"},
        {"address": "192.0.2.10", "name": "a.example.test."},
        {"address": "192.0.2.10", "name": "-a.example.test"},
        {"address": "192.0.2.10", "name": "a_b.example.test"},
        {"address": "192.0.2.10", "name": "a.example.test\n192.0.2.11 b"},
        {"address": "192.0.2.10", "name": ("a" * 63 + ".") * 4 + "test"},
        {"address": 3221225994, "name": "a.example.test"},
        {"address": "192.0.2.10"},
    ],
)
def test_bound_dns_entry_refuses(params: dict[str, Any]) -> None:
    with pytest.raises(HolodeckDnsError) as exc:
        bound_dns_entry(params)
    assert exc.value.step == "input"


def test_parse_hosts_skips_comments_and_blank_lines() -> None:
    text = "# lab names\n\n192.0.2.1 Router.example.test r1 # main\n2001:db8::0:1 v6.example.test"
    assert parse_hosts(text) == [
        {"index": 2, "address": "192.0.2.1", "names": ["router.example.test", "r1"]},
        {"index": 3, "address": "2001:db8::1", "names": ["v6.example.test"]},
    ]


def test_plan_add_appends_one_line() -> None:
    new = plan_add(_HOSTS, "192.0.2.10", "appliance01.example.test")
    assert new == _HOSTS + "192.0.2.10 appliance01.example.test\n"


def test_plan_add_adds_missing_final_newline() -> None:
    assert plan_add("192.0.2.1 r.example.test", "192.0.2.10", "a.example.test") == (
        "192.0.2.1 r.example.test\n192.0.2.10 a.example.test\n"
    )


def test_plan_add_same_line_changes_nothing() -> None:
    assert plan_add(_HOSTS, "192.0.2.2", "dc01.example.test") is None


@pytest.mark.parametrize(
    ("address", "name"),
    [("192.0.2.99", "dc01.example.test"), ("192.0.2.2", "other.example.test")],
)
def test_plan_add_refuses_other_partner(address: str, name: str) -> None:
    with pytest.raises(HolodeckDnsError) as exc:
        plan_add(_HOSTS, address, name)
    assert exc.value.step == "conflict"


def test_plan_remove_drops_the_line_only() -> None:
    assert (
        plan_remove(_HOSTS, "192.0.2.2", "dc01.example.test") == "192.0.2.1 router.example.test\n"
    )


def test_plan_remove_missing_line_is_absent() -> None:
    assert plan_remove(_HOSTS, "192.0.2.10", "appliance01.example.test") is None


def test_plan_remove_refuses_line_it_did_not_write() -> None:
    text = "192.0.2.2 dc01.example.test dc01\n"
    with pytest.raises(HolodeckDnsError) as exc:
        plan_remove(text, "192.0.2.2", "dc01.example.test")
    assert exc.value.step == "conflict"


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------


def _op(op_id: str) -> Any:
    return next(op for op in DNS_OPS if op.op_id == op_id)


def test_dns_ops_are_registered() -> None:
    ids = {op.op_id for op in HOLODECK_OPS}
    assert {"holodeck.dns.host.list", "holodeck.dns.host.add", "holodeck.dns.host.remove"} <= ids


def test_dns_read_is_safe_and_writes_wait_for_approval() -> None:
    assert _op("holodeck.dns.host.list").safety_level == "safe"
    assert _op("holodeck.dns.host.list").requires_approval is False
    assert _op("holodeck.dns.host.add").safety_level == "caution"
    assert _op("holodeck.dns.host.remove").safety_level == "dangerous"
    for op_id in ("holodeck.dns.host.add", "holodeck.dns.host.remove"):
        assert _op(op_id).requires_approval is True
        assert "write" in _op(op_id).tags


def test_k8s_exec_stays_read_only() -> None:
    from meho_backplane.connectors.holodeck.ops_read import (
        KubectlSafetyError,
        parse_kubectl_command,
    )

    for cmd in ("kubectl patch configmap dnsmasq -n default", "kubectl rollout restart deploy/x"):
        with pytest.raises(KubectlSafetyError):
            parse_kubectl_command(cmd)


@pytest.mark.parametrize(
    "params",
    [
        {"address": "192.0.2.10; reboot", "name": "a.example.test"},
        {"address": "192.0.2.10", "name": "a.example.test;reboot"},
        {"address": "192.0.2.10", "name": "a.example.test", "extra": "x"},
        {"address": "192.0.2.10"},
    ],
)
def test_write_schema_refuses_bad_params(params: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(params, _op("holodeck.dns.host.add").parameter_schema)


def test_write_schema_accepts_example() -> None:
    schema = _op("holodeck.dns.host.remove").parameter_schema
    jsonschema.validate(schema["examples"][0], schema)


# ---------------------------------------------------------------------------
# Fake router
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Plays the router's kubectl and dig for the DNS ops."""

    def __init__(self, hosts: str = _HOSTS, *, has_dig: bool = True) -> None:
        self.data: dict[str, str] = {"hosts": hosts, **_OTHER_KEYS}
        self.files: dict[str, str] = {}
        self.commands: list[str] = []
        self.has_dig = has_dig
        self.change_before_patch: str | None = None
        self._rev = 0
        self.pods: list[dict[str, Any]] = [self._pod()]

    def _pod(self) -> dict[str, Any]:
        self._rev += 1
        return {
            "hash": f"h{self._rev}",
            "name": f"dnsmasq-dns-deployment-h{self._rev}-x{self._rev}",
            "ip": f"198.51.100.{self._rev}",
            "deleting": False,
            "hosts": self.data["hosts"],
        }

    def _pods_json(self) -> str:
        items = [
            {
                "metadata": {
                    "name": p["name"],
                    "labels": {"pod-template-hash": p["hash"]},
                    **({"deletionTimestamp": "2026-01-01T00:00:00Z"} if p["deleting"] else {}),
                },
                "status": {"podIP": p["ip"]},
            }
            for p in self.pods
        ]
        # A pod of another workload must be ignored.
        items.append({"metadata": {"name": "coredns-abc", "labels": {}}, "status": {}})
        out = json.dumps({"items": items})
        # Old pods are gone the next time someone looks.
        self.pods = [p for p in self.pods if not p["deleting"]]
        return out

    def _patch(self, cmd: str) -> tuple[str, str, int]:
        if self.change_before_patch is not None:
            self.data["hosts"] = self.change_before_patch
        payload = json.loads(shlex.split(cmd)[-1])
        assert [step["op"] for step in payload] == ["test", "replace"]
        assert all(step["path"] == "/data/hosts" for step in payload)
        if payload[0]["value"] != self.data["hosts"]:
            return "", "The request is invalid: test operation failed", 1
        self.data["hosts"] = payload[1]["value"]
        return "configmap/dnsmasq patched\n", "", 0

    def _dig(self, cmd: str) -> tuple[str, str, int]:
        if not self.has_dig:
            return "", "dig: command not found", 127
        tokens = shlex.split(cmd)
        server = tokens[4].lstrip("@")
        pod = next(p for p in self.pods if p["ip"] == server)
        rows = parse_hosts(pod["hosts"])
        if tokens[5] == "-x":
            address = str(ipaddress.ip_address(tokens[6]))
            names = [n for r in rows if r["address"] == address for n in r["names"]]
            return "".join(f"{n}.\n" for n in names), "", 0
        name = tokens[5]
        addrs = [r["address"] for r in rows if name in r["names"]]
        return "".join(f"{a}\n" for a in addrs), "", 0

    def handle(self, cmd: str) -> tuple[str, str, int]:
        self.commands.append(cmd)
        if cmd == "kubectl get configmap dnsmasq -n default -o json":
            return json.dumps({"kind": "ConfigMap", "data": self.data}), "", 0
        if cmd.startswith("kubectl get configmap dnsmasq -n default -o yaml > /root/"):
            self.files[shlex.split(cmd)[9]] = json.dumps(self.data)
            return "", "", 0
        if cmd.startswith("kubectl patch configmap dnsmasq -n default --type json -p "):
            return self._patch(cmd)
        if cmd == "kubectl rollout restart deployment/dnsmasq-dns-deployment -n default":
            for p in self.pods:
                p["deleting"] = True
            self.pods.append(self._pod())
            return "deployment restarted\n", "", 0
        if cmd.startswith("kubectl rollout status deployment/dnsmasq-dns-deployment -n default"):
            return "successfully rolled out\n", "", 0
        if cmd == "kubectl get pods -n default -o json":
            return self._pods_json(), "", 0
        if cmd.startswith("dig +short +time=2 +tries=1 @"):
            return self._dig(cmd)
        return "", f"unknown command: {cmd}", 127

    def as_run_command(self) -> AsyncMock:
        async def _run(_target: Any, cmd: str, operator: Any = None) -> Any:
            stdout, stderr, status = self.handle(cmd)
            proc = MagicMock()
            proc.stdout, proc.stderr, proc.exit_status = stdout, stderr, status
            return proc

        return AsyncMock(side_effect=_run)


_TARGET = types.SimpleNamespace(name="holorouter-dns-test", host="holorouter.test.invalid")


async def _call(router: _FakeRouter, op: str, params: dict[str, Any]) -> dict[str, Any]:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", router.as_run_command()):
        result: dict[str, Any] = await getattr(connector, op)(_TARGET, params)
    return result


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_rows() -> None:
    result = await _call(_FakeRouter(), "dns_host_list", {})
    assert result == {
        "rows": [
            {"address": "192.0.2.1", "names": ["router.example.test"]},
            {"address": "192.0.2.2", "names": ["dc01.example.test"]},
        ],
        "total": 2,
    }


@pytest.mark.asyncio
async def test_add_saves_writes_restarts_waits_and_checks() -> None:
    router = _FakeRouter()
    result = await _call(router, "dns_host_add", _NEW)
    assert result["error"] is None, result
    assert result["state"] == "added"
    assert result["changed"] is True
    assert (result["lines_before"], result["lines_after"]) == (2, 3)
    assert result["restarted"] is True
    assert result["verified"] is True
    assert [c["pod"] for c in result["checks"]] == ["dnsmasq-dns-deployment-h2-x2"]
    assert result["checks"][0]["forward"] == ["192.0.2.10"]
    assert result["checks"][0]["reverse"] == ["appliance01.example.test"]
    assert router.data["hosts"] == _HOSTS + "192.0.2.10 appliance01.example.test\n"
    assert {k: v for k, v in router.data.items() if k != "hosts"} == _OTHER_KEYS
    # The save ran before the write and holds the old value.
    assert result["backup_path"] in router.files
    assert json.loads(router.files[result["backup_path"]])["hosts"] == _HOSTS
    kinds = [c.split(" -n ")[0] for c in router.commands if c.startswith("kubectl")]
    assert kinds[:6] == [
        "kubectl get configmap dnsmasq",
        "kubectl get configmap dnsmasq",
        "kubectl patch configmap dnsmasq",
        "kubectl get configmap dnsmasq",
        "kubectl rollout restart deployment/dnsmasq-dns-deployment",
        "kubectl rollout status deployment/dnsmasq-dns-deployment",
    ]


@pytest.mark.asyncio
async def test_add_twice_changes_nothing_the_second_time() -> None:
    router = _FakeRouter()
    await _call(router, "dns_host_add", _NEW)
    router.commands.clear()
    result = await _call(router, "dns_host_add", _NEW)
    assert result["state"] == "unchanged"
    assert result["changed"] is False
    assert result["backup_path"] is None
    assert result["restarted"] is False
    assert result["verified"] is True
    assert not any("patch" in c or "rollout" in c or "-o yaml" in c for c in router.commands)


@pytest.mark.asyncio
async def test_add_refuses_name_used_with_other_address() -> None:
    router = _FakeRouter()
    result = await _call(
        router, "dns_host_add", {"address": "192.0.2.10", "name": "dc01.example.test"}
    )
    assert result["error"].startswith("conflict:")
    assert result["changed"] is False
    assert router.data["hosts"] == _HOSTS
    assert not any("patch" in c or "rollout" in c for c in router.commands)


@pytest.mark.asyncio
async def test_add_refuses_bad_input_before_any_command() -> None:
    router = _FakeRouter()
    result = await _call(router, "dns_host_add", {"address": "192.0.2.10", "name": "x;reboot"})
    assert result["error"].startswith("input:")
    assert router.commands == []


@pytest.mark.asyncio
async def test_add_never_overwrites_a_change_made_in_between() -> None:
    router = _FakeRouter()
    router.change_before_patch = _HOSTS + "192.0.2.20 someone-else.example.test\n"
    result = await _call(router, "dns_host_add", _NEW)
    assert result["error"].startswith("write:")
    assert result["changed"] is False
    assert result["restarted"] is False
    assert "someone-else" in router.data["hosts"]
    assert "appliance01" not in router.data["hosts"]


@pytest.mark.asyncio
async def test_add_reports_not_verified_when_dig_is_missing() -> None:
    router = _FakeRouter(has_dig=False)
    result = await _call(router, "dns_host_add", _NEW)
    assert result["state"] == "added"
    assert result["verified"] is False
    assert result["checks"][0]["error"] == "dig failed on the router"


@pytest.mark.asyncio
async def test_remove_then_remove_again_is_absent() -> None:
    router = _FakeRouter(_HOSTS + "192.0.2.10 appliance01.example.test\n")
    result = await _call(router, "dns_host_remove", _NEW)
    assert result["error"] is None, result
    assert result["state"] == "removed"
    assert (result["lines_before"], result["lines_after"]) == (3, 2)
    assert result["verified"] is True
    assert result["checks"][0]["forward"] == []
    assert router.data["hosts"] == _HOSTS

    router.commands.clear()
    again = await _call(router, "dns_host_remove", _NEW)
    assert again["state"] == "absent"
    assert again["changed"] is False
    assert again["verified"] is True
    assert not any("patch" in c or "rollout" in c for c in router.commands)


@pytest.mark.asyncio
async def test_wait_fails_when_old_pods_stay() -> None:
    router = _FakeRouter()
    real = router._pods_json

    def _stuck() -> str:
        for p in router.pods:
            if p["hash"] == "h1":
                p["deleting"] = True
        pods = list(router.pods)
        out = real()
        router.pods = pods
        return out

    router._pods_json = _stuck  # type: ignore[method-assign]
    result = await _call(router, "dns_host_add", _NEW)
    assert result["error"].startswith("wait:")
    assert result["changed"] is True
    assert result["backup_path"]


# ---------------------------------------------------------------------------
# Dispatch through the fake-shell SSH server
# ---------------------------------------------------------------------------

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()
_TARGET_NAME = "holodeck-dns-e2e"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004066")
_OPERATOR = Operator(
    sub="holodeck-dns-e2e-test",
    name="Holodeck DNS E2E Operator",
    email=None,
    raw_jwt="<holodeck-dns-e2e-raw-jwt>",
    tenant_id=_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)


class _FakeShellSSHServer(asyncssh.SSHServer):
    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:  # type: ignore[override]
        return bool(key == _CLIENT_PUB)


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
async def fake_router_server() -> AsyncIterator[types.SimpleNamespace]:
    router = _FakeRouter()

    async def _factory(process: Any) -> None:
        stdout, stderr, status = router.handle(process.command or "")
        process.stdout.write(stdout)
        process.stderr.write(stderr)
        process.exit(status)

    srv = await asyncssh.create_server(
        _FakeShellSSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[_SERVER_KEY],
        process_factory=_factory,
    )
    port: int = srv.sockets[0].getsockname()[1]
    yield types.SimpleNamespace(host="127.0.0.1", port=port, router=router)
    srv.close()
    await srv.wait_closed()


@pytest.fixture
async def dns_e2e(fake_router_server: types.SimpleNamespace) -> AsyncIterator[_FakeRouter]:
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    await HolodeckConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT,
                name=_TARGET_NAME,
                aliases=[],
                product="holodeck",
                host=fake_router_server.host,
                port=fake_router_server.port,
                fqdn=None,
                secret_ref="kv/dev/holodeck/dns-e2e",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0.0"},
                notes="seeded by test_connectors_holodeck_dns",
            )
        )
        await session.commit()

    class _Seeded(HolodeckConnector):  # type: ignore[misc]
        async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
            return {"username": "root", "client_keys": [_CLIENT_KEY], "known_hosts": None}

    instance = _Seeded()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[HolodeckConnector] = instance  # type: ignore[assignment]
    yield fake_router_server.router
    await instance.aclose()
    reset_dispatcher_caches()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id="holodeck-ssh-9.0",
        op_id=op_id,
        target=target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


@pytest.mark.asyncio
async def test_dispatch_list_runs_without_approval(dns_e2e: _FakeRouter) -> None:
    result = await _dispatch("holodeck.dns.host.list", {}, approved=False)
    assert result["status"] == "ok", result
    assert result["result"]["total"] == 2


@pytest.mark.asyncio
async def test_dispatch_add_waits_for_approval_then_runs(dns_e2e: _FakeRouter) -> None:
    parked = await _dispatch("holodeck.dns.host.add", _NEW, approved=False)
    assert parked["status"] == "awaiting_approval", parked
    assert "appliance01" not in dns_e2e.data["hosts"]

    done = await _dispatch("holodeck.dns.host.add", _NEW, approved=True)
    assert done["status"] == "ok", done
    assert done["result"]["state"] == "added"
    assert done["result"]["verified"] is True
    assert "192.0.2.10 appliance01.example.test" in dns_e2e.data["hosts"]
