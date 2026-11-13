# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense VPN onboarding ops (#3993).

* ``pfsense.openvpn.address.next_free`` -- both sources of used addresses,
  more than one server, no free address, a failed read.
* ``pfsense.openvpn.csc.sync`` -- the plan (create / update / unchanged and
  every refusal), compare-only, the write with read-back, hostile input,
  and the PHP fragment run for real (needs ``php``).
* ``pfsense.openvpn.user.create`` -- the password goes to Vault and nowhere
  else, the Vault failure undoes the user, an existing user is left alone.
* ``pfsense.openvpn.user.delete`` -- connected, unknown, force, refusals.
* Metadata: risk level, approval, response schemas.

All fixtures are synthetic: made-up names and documentation addresses
(198.51.100.0/24, 203.0.113.0/24, 192.0.2.0/24).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import hvac.exceptions
import pytest
from jsonschema import Draft202012Validator

import meho_backplane.connectors.pfsense as pfsense  # import for registry side-effects
import meho_backplane.connectors.pfsense.ops_openvpn_user as user_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.pfsense.openvpn_config import (
    parse_csc_entries,
    parse_openvpn_servers,
)
from meho_backplane.connectors.pfsense.ops_openvpn_csc import (
    OPENVPN_CSC_OPS,
    build_csc_sync_playback,
    plan_csc_sync,
)
from meho_backplane.connectors.pfsense.refusal import PfSenseRefusedError
from meho_backplane.op_classes import is_root_shell, needs_per_call_approval
from meho_backplane.settings import get_settings

_TENANT_ID = UUID("00000000-0000-0000-0000-000000003993")
_VAULT_PATH = f"tenants/{_TENANT_ID}/vpn/guest-1"
_PASSWORD = "FakePasswordFakePassword"[:24]  # 24 letters, the shape the firewall returns


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-vpn",
        name="VPN onboarding test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


# ---------------------------------------------------------------------------
# Synthetic config.xml
# ---------------------------------------------------------------------------


def _server(vpnid: str, network: str, *, mode: str = "server_user", disabled: bool = False) -> str:
    return (
        f"<openvpn-server><vpnid>{vpnid}</vpnid><mode>{mode}</mode>"
        f"<description>server {vpnid}</description><tunnel_network>{network}</tunnel_network>"
        "<tls>SERVER-TLS-KEY-MUST-NOT-LEAK</tls>"
        f"{'<disable></disable>' if disabled else ''}</openvpn-server>"
    )


def _csc(name: str, vpnid: str, tunnel: str = "", routes: str = "", server_list: str = "") -> str:
    return (
        f"<openvpn-csc><common_name>{name}</common_name>"
        f"<server_list>{server_list or vpnid}</server_list><description>d {name}</description>"
        f"<tunnel_network>{tunnel}</tunnel_network><local_network>{routes}</local_network>"
        "</openvpn-csc>"
    )


def _user(name: str, uid: str, *, scope: str = "user") -> str:
    return (
        f"<user><scope>{scope}</scope><name>{name}</name><uid>{uid}</uid>"
        "<bcrypt-hash>hash-must-not-leak</bcrypt-hash></user>"
    )


_NET_UDP = "198.51.100.0/24"
_NET_TCP = "203.0.113.0/24"
_SERVERS = _server("1", _NET_UDP) + _server("2", _NET_TCP)


def _config(*, servers: str = _SERVERS, csc: str = "", users: str = "", rules: str = "") -> str:
    return (
        "<pfsense><system>"
        f"<group><name>all</name><member>0</member></group>{_user('admin', '0', scope='system')}"
        f"{users}</system><filter>{rules}</filter>"
        f"<openvpn>{servers}{csc}</openvpn></pfsense>"
    )


def _tagged_rule(tracker: str, name: str, source: str) -> str:
    """A pass rule on the OpenVPN tab with the tag of ``pfsense.openvpn.rule.sync``."""
    return (
        f"<rule><tracker>{tracker}</tracker><type>pass</type><interface>openvpn</interface>"
        f"<ipprotocol>inet</ipprotocol><source><address>{source}</address></source>"
        "<destination><address>192.0.2.0/24</address></destination>"
        f"<descr>meho-vpn-user:{name}</descr></rule>"
    )


def _rendered(*rows: tuple[str, str, str]) -> str:
    """Output of the rendered-file command: ``(vpnid, name, address)`` per file."""
    return "".join(
        f"/var/etc/openvpn/server{v}/csc/{n}:ifconfig-push {a} 255.255.255.0\n" for v, n, a in rows
    )


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


class _Fake(pfsense.PfSenseConnector):
    """A scripted pfSense: config reads replay a list, other commands match by prefix."""

    def __init__(
        self,
        configs: list[str],
        *,
        rendered: str = "",
        rendered_exit: int = 0,
        playback_out: str = "",
        sockets: dict[str, tuple[str, int]] | None = None,
        playback_fails: bool = False,
        undo_out: str = "MEHO_VPN_REMOVED\n",
    ) -> None:
        super().__init__()
        self._configs = list(configs)
        self._rendered = rendered
        self._rendered_exit = rendered_exit
        self._playback_out = playback_out
        self._sockets = sockets or {}
        self._playback_fails = playback_fails
        self._undo_out = undo_out
        self.commands: list[str] = []
        self.played: list[str] = []
        self.staged: list[str] = []

    async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {"username": "admin", "client_keys": [], "known_hosts": None}

    async def _run_command(
        self,
        target: Any,
        cmd: str,
        *,
        operator: Any = None,
        timeout: float = 30.0,
        stdin: str | None = None,
    ) -> Any:
        self.commands.append(cmd)
        if cmd == "cat /cf/conf/config.xml":
            if len(self._configs) > 1:
                return _proc(self._configs.pop(0))
            return _proc(self._configs[0])
        if cmd.startswith("[ -d /var/etc/openvpn ]"):
            return _proc(self._rendered, self._rendered_exit)
        if cmd.startswith("cat > /etc/phpshellsessions/"):
            self.staged.append(cmd)
            return _proc()
        if cmd.startswith("pfSsh.php playback"):
            self.played.append(cmd)
            if cmd.startswith("pfSsh.php playback meho_openvpn_user_undo"):
                return _proc(self._undo_out)
            return _proc(self._playback_out, 1 if self._playback_fails else 0)
        for key, (out, code) in self._sockets.items():
            if f"/server{key}/sock" in cmd:
                return _proc(out, code)
        return _proc()

    @property
    def wrote(self) -> bool:
        return bool(self.played)


# ===========================================================================
# next_free
# ===========================================================================


async def _next_free(fake: _Fake) -> dict[str, Any]:
    return await fake.openvpn_address_next_free(None, {}, None)  # type: ignore[arg-type]


async def test_next_free_is_2_on_an_empty_estate() -> None:
    fake = _Fake([_config()])
    out = await _next_free(fake)
    assert out["status"] == "found"
    assert out["address_offset"] == 2
    assert [(s["vpnid"], s["address"], s["tunnel_network"]) for s in out["servers"]] == [
        ("1", "198.51.100.2", "198.51.100.2/24"),
        ("2", "203.0.113.2", "203.0.113.2/24"),
    ]


async def test_next_free_counts_config_entries_on_either_server() -> None:
    csc = _csc("a", "1", "198.51.100.2/24") + _csc("b", "2", "203.0.113.3/24")
    out = await _next_free(_Fake([_config(csc=csc)]))
    assert out["address_offset"] == 4  # 2 is used on server 1, 3 on server 2


async def test_next_free_counts_rendered_files_that_the_config_does_not_hold() -> None:
    """The two sources disagree: a file with no entry in the config still holds its address."""
    fake = _Fake([_config()], rendered=_rendered(("2", "stale-user", "203.0.113.2")))
    out = await _next_free(fake)
    assert out["address_offset"] == 3
    assert out["sources"] == {"config_entries": 0, "rendered_files": 1}


async def test_next_free_counts_a_config_entry_that_has_no_rendered_file() -> None:
    csc = _csc("a", "1", "198.51.100.2/24")
    out = await _next_free(_Fake([_config(csc=csc)], rendered=""))
    assert out["address_offset"] == 3


async def test_next_free_ignores_servers_that_do_not_log_users_in() -> None:
    servers = _server("1", _NET_UDP) + _server("9", "192.0.2.0/24", mode="p2p_tls")
    csc = _csc("a", "9", "192.0.2.2/24")
    out = await _next_free(_Fake([_config(servers=servers, csc=csc)]))
    assert out["address_offset"] == 2
    assert [s["vpnid"] for s in out["servers"]] == ["1"]


async def test_next_free_without_a_user_login_server() -> None:
    fake = _Fake([_config(servers=_server("9", "192.0.2.0/24", mode="p2p_tls"))])
    out = await _next_free(fake)
    assert out["status"] == "no_user_auth_server"
    assert out["address_offset"] is None


async def test_next_free_exhausted_when_no_offset_is_free_everywhere() -> None:
    servers = _server("1", "198.51.100.0/30") + _server("2", "203.0.113.0/30")
    csc = _csc("a", "1", "198.51.100.2/30")  # a /30 has one client address: offset 2
    out = await _next_free(_Fake([_config(servers=servers, csc=csc)]))
    assert out["status"] == "exhausted"
    assert out["address_offset"] is None


async def test_next_free_warns_about_an_entry_with_an_alias_as_tunnel_network() -> None:
    out = await _next_free(_Fake([_config(csc=_csc("a", "1", "vpn_alias"))]))
    assert out["status"] == "found"
    assert any("'a'" in w for w in out["warnings"])


async def test_next_free_fails_loudly_when_the_rendered_files_cannot_be_read() -> None:
    fake = _Fake([_config()], rendered_exit=2)
    with pytest.raises(RuntimeError, match="rendered OpenVPN override files"):
        await _next_free(fake)


async def test_next_free_fails_loudly_when_the_config_cannot_be_read() -> None:
    class _Broken(_Fake):
        async def _run_command(
            self,
            target: Any,
            cmd: str,
            *,
            operator: Any = None,
            timeout: float = 30.0,
            stdin: str | None = None,
        ) -> Any:
            return _proc("", 1)

    with pytest.raises(RuntimeError, match=r"config\.xml"):
        await _next_free(_Broken([""]))


async def test_next_free_fails_when_a_server_has_no_ipv4_network() -> None:
    fake = _Fake([_config(servers=_server("1", ""))])
    with pytest.raises(RuntimeError, match="no IPv4 tunnel network"):
        await _next_free(fake)


# ===========================================================================
# csc.sync -- the plan
# ===========================================================================


def _entry(name: str = "guest-1", vpnid: str = "1", **kw: Any) -> dict[str, Any]:
    return {"name": name, "server_vpnid": vpnid, **kw}


async def _sync(fake: _Fake, entries: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return await fake.openvpn_csc_sync(None, {"entries": entries, **kw}, None)  # type: ignore[arg-type]


def _plan_for(
    config: str, entries: list[dict[str, Any]], rendered: str = ""
) -> list[dict[str, Any]]:
    import ipaddress

    from meho_backplane.connectors.pfsense.openvpn_config import _RENDERED_LINE_RE
    from meho_backplane.connectors.pfsense.ops_openvpn_csc import _validate_entries

    rows = []
    for line in rendered.splitlines():
        m = _RENDERED_LINE_RE.match(line)
        assert m
        rows.append((m.group(1), m.group(2), ipaddress.IPv4Address(m.group(3))))
    return plan_csc_sync(
        _validate_entries(entries),
        parse_openvpn_servers(config),
        parse_csc_entries(config),
        rows,
        frozenset(e["name"] for e in entries),
    )


def test_plan_create_update_unchanged_in_one_call() -> None:
    csc = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25") + _csc(
        "same", "2", "203.0.113.6/24", "192.0.2.0/24"
    )
    plan = _plan_for(
        _config(csc=csc),
        [
            _entry("new", "1", tunnel_address="198.51.100.9", routes=["192.0.2.0/24"]),
            _entry("old", "1", routes=["192.0.2.0/24", "192.0.2.0/25"]),
            _entry("same", "2", tunnel_address="203.0.113.6", routes=["192.0.2.0/24"]),
        ],
    )
    assert [p["action"] for p in plan] == ["create", "update", "unchanged"]
    assert plan[0]["tunnel_network"] == "198.51.100.9/24"
    assert plan[1]["routes_added"] == ["192.0.2.0/24"]
    # An update only adds: the old route stays, the new one follows.
    assert plan[1]["routes"] == ["192.0.2.0/25", "192.0.2.0/24"]


@pytest.mark.parametrize(
    ("entry", "csc", "rendered", "reason"),
    [
        (_entry(vpnid="7", tunnel_address="198.51.100.9"), "", "", "server_not_found"),
        (_entry(tunnel_address="198.51.100.9"), "", "", None),  # control: this one is fine
        (_entry(), "", "", "tunnel_address_required"),
        (_entry(tunnel_address="192.0.2.9"), "", "", "address_outside_network"),
        (_entry(tunnel_address="198.51.100.0"), "", "", "address_reserved"),
        (_entry(tunnel_address="198.51.100.1"), "", "", "address_reserved"),
        (_entry(tunnel_address="198.51.100.255"), "", "", "address_reserved"),
        (
            _entry(tunnel_address="198.51.100.9"),
            _csc("other", "1", "198.51.100.9/24"),
            "",
            "address_in_use",
        ),
        (
            _entry(tunnel_address="198.51.100.9"),
            "",
            _rendered(("1", "ghost", "198.51.100.9")),
            "address_in_use",
        ),
        (
            _entry(tunnel_address="198.51.100.9"),
            _csc("guest-1", "1", "198.51.100.5/24"),
            "",
            "address_conflict",
        ),
        (_entry(), _csc("guest-1", "1") + _csc("guest-1", "1"), "", "ambiguous_entry"),
        (
            _entry(routes=["192.0.2.0/24"]),
            _csc("guest-1", "1", "198.51.100.5/24", server_list="1,2"),
            "",
            "shared_entry",
        ),
    ],
)
def test_plan_refusals(entry: dict[str, Any], csc: str, rendered: str, reason: str | None) -> None:
    (row,) = _plan_for(_config(csc=csc), [entry], rendered)
    assert row["reason"] == reason
    assert (row["action"] == "refused") is (reason is not None)


def test_plan_refuses_a_server_that_does_not_log_users_in() -> None:
    config = _config(servers=_server("1", _NET_UDP, mode="p2p_tls"))
    (row,) = _plan_for(config, [_entry(tunnel_address="198.51.100.9")])
    assert row["reason"] == "server_not_user_auth"


def test_plan_refuses_the_same_address_for_two_names_in_one_call() -> None:
    plan = _plan_for(
        _config(),
        [
            _entry("a", tunnel_address="198.51.100.9"),
            _entry("b", tunnel_address="198.51.100.9"),
        ],
    )
    assert [p["action"] for p in plan] == ["create", "refused"]
    assert plan[1]["reason"] == "address_repeated"


def test_plan_shared_entry_that_needs_no_change_is_unchanged() -> None:
    csc = _csc("guest-1", "1", "198.51.100.5/24", "192.0.2.0/24", server_list="1,2")
    (row,) = _plan_for(_config(csc=csc), [_entry(routes=["192.0.2.0/24"])])
    assert row["action"] == "unchanged"


def test_plan_same_name_on_another_server_is_a_new_entry() -> None:
    csc = _csc("guest-1", "1", "198.51.100.5/24")
    (row,) = _plan_for(_config(csc=csc), [_entry(vpnid="2", tunnel_address="203.0.113.5")])
    assert row["action"] == "create"


# ===========================================================================
# csc.sync -- input
# ===========================================================================


@pytest.mark.parametrize(
    "entries",
    [
        None,
        [],
        "x",
        [{"server_vpnid": "1"}],
        [{"name": "x'; system('id'); //", "server_vpnid": "1"}],
        [{"name": "a", "server_vpnid": "1; rm"}],
        [{"name": "a", "server_vpnid": "1", "tunnel_address": "not-an-ip"}],
        [{"name": "a", "server_vpnid": "1", "tunnel_address": "2001:db8::1"}],
        [{"name": "a", "server_vpnid": "1", "routes": ["10.0.0.0/33"]}],
        [{"name": "a", "server_vpnid": "1", "routes": "10.0.0.0/24"}],
        [{"name": "a", "server_vpnid": "1", "routes": ["2001:db8::/32"]}],
        [{"name": "a", "server_vpnid": "1", "description": "line\nbreak"}],
        [{"name": "a", "server_vpnid": "1", "extra": 1}],
        [{"name": "a", "server_vpnid": "1"}, {"name": "a", "server_vpnid": "1"}],
        ["not-an-object"],
    ],
)
async def test_sync_rejects_bad_input_before_any_ssh_call(entries: Any) -> None:
    fake = _Fake([_config()])
    with pytest.raises(ValueError):
        await fake.openvpn_csc_sync(None, {"entries": entries}, None)  # type: ignore[arg-type]
    assert fake.commands == []


# ===========================================================================
# csc.sync -- handler
# ===========================================================================


def _users(*names: str) -> str:
    """Local users with these names: the sync plans overrides only for them."""
    return "".join(_user(n, str(2000 + i)) for i, n in enumerate(names))


def _after_config(extra_csc: str, base_csc: str = "", users: str = "") -> str:
    return _config(csc=base_csc + extra_csc, users=users)


async def test_compare_only_returns_the_plan_and_writes_nothing() -> None:
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")
    fake = _Fake([_config(csc=base, users=_users("new", "old"))])
    out = await _sync(
        fake,
        [
            _entry("new", tunnel_address="198.51.100.9", routes=["192.0.2.0/24"]),
            _entry("old", routes=["192.0.2.0/24"]),
            _entry("old", "2"),  # a new entry on server 2 needs an address: refused
        ],
        compare_only=True,
    )
    assert out["status"] == "refused"
    assert out["counts"] == {"create": 1, "update": 1, "unchanged": 0, "refused": 1}
    assert not fake.wrote
    assert not any("cp /cf/conf/config.xml" in c for c in fake.commands)


async def test_compare_only_counts_when_every_entry_is_fine() -> None:
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25") + _csc(
        "ok", "1", "198.51.100.6/24", "192.0.2.0/24"
    )
    fake = _Fake([_config(csc=base, users=_users("new", "old", "ok"))])
    out = await _sync(
        fake,
        [
            _entry("new", tunnel_address="198.51.100.9"),
            _entry("old", routes=["192.0.2.0/24"]),
            _entry("ok", routes=["192.0.2.0/24"]),
        ],
        compare_only=True,
    )
    assert out["status"] == "compared"
    assert out["counts"] == {"create": 1, "update": 1, "unchanged": 1, "refused": 0}
    assert out["applied"] is False
    assert not fake.wrote


async def test_one_refused_entry_stops_the_whole_call() -> None:
    fake = _Fake([_config(users=_users("a", "b"))])
    with pytest.raises(PfSenseRefusedError) as info:
        await _sync(
            fake,
            [_entry("a", tunnel_address="198.51.100.9"), _entry("b", tunnel_address="192.0.2.9")],
        )
    assert info.value.code == "refused"
    assert "b on server 1: address_outside_network" in info.value.guidance
    assert "a on server" not in info.value.guidance
    assert not fake.wrote
    assert not any("cp /cf/conf/config.xml" in c for c in fake.commands)


async def test_sync_writes_and_verifies_without_a_rollback_copy() -> None:
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")
    users = _users("new", "old")
    after = _after_config(
        _csc("new", "1", "198.51.100.9/24", "192.0.2.0/24"),
        _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25,192.0.2.0/24"),
        users,
    )
    fake = _Fake([_config(csc=base, users=users), after])
    out = await _sync(
        fake,
        [
            _entry("new", tunnel_address="198.51.100.9", routes=["192.0.2.0/24"]),
            _entry("old", routes=["192.0.2.0/24"]),
        ],
    )
    assert out["status"] == "synced"
    assert out["applied"] is True and out["verified"] is True
    # pfSense keeps its own config history: the op makes no copy of config.xml.
    assert "rollback_copy" not in out
    assert not any("cp /cf/conf/config.xml" in c for c in fake.commands)


async def test_sync_plans_no_override_for_a_name_without_a_local_user() -> None:
    fake = _Fake([_config(users=_users("guest-1"))])
    entries = [
        _entry(tunnel_address="198.51.100.9"),
        _entry("nobody", tunnel_address="198.51.100.10"),
    ]
    out = await _sync(fake, entries, compare_only=True)
    assert out["status"] == "refused"
    row = out["entries"][1]
    assert (row["action"], row["reason"]) == ("refused", "user_not_found")
    assert row["detail"].startswith("no such user")
    with pytest.raises(PfSenseRefusedError) as info:
        await _sync(fake, entries)
    assert "nobody on server 1: user_not_found" in info.value.guidance
    assert not fake.wrote


async def test_sync_is_safe_to_run_twice() -> None:
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/24")
    fake = _Fake([_config(csc=base, users=_users("old"))])
    out = await _sync(fake, [_entry("old", tunnel_address="198.51.100.5", routes=["192.0.2.0/24"])])
    assert out["status"] == "nothing_to_do"
    assert not fake.wrote


async def test_sync_fails_when_the_read_back_does_not_hold_the_entry() -> None:
    # The config never changes: the playback "did not persist".
    fake = _Fake([_config(users=_users("guest-1"))])
    with pytest.raises(RuntimeError, match="verification failed"):
        await _sync(fake, [_entry(tunnel_address="198.51.100.9")])


async def test_sync_fails_when_something_else_changed_during_the_write() -> None:
    users = _users("new")
    after = _config(
        csc=_csc("new", "1", "198.51.100.9/24"), users=users + _user("intruder", "2005")
    )
    fake = _Fake([_config(users=users), after])
    with pytest.raises(RuntimeError, match=r"other parts of config\.xml"):
        await _sync(fake, [_entry("new", tunnel_address="198.51.100.9")])


async def test_sync_writes_nothing_when_an_override_changed_after_the_plan() -> None:
    """The fragment found the entry changed (a route added in the web page meanwhile)."""
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")
    fake = _Fake([_config(csc=base, users=_users("old"))], playback_out="MEHO_CSC_CHANGED=old@1\n")
    with pytest.raises(RuntimeError, match=r"changed after the plan was made \(old@1\)"):
        await _sync(fake, [_entry("old", routes=["192.0.2.0/24"])])
    assert len(fake.played) == 1
    # No read-back: the fragment wrote nothing, so nothing is reported as synced.
    assert fake.commands.count("cat /cf/conf/config.xml") == 1


async def test_sync_fails_when_a_failed_read_of_the_rendered_files() -> None:
    fake = _Fake([_config()], rendered_exit=2)
    with pytest.raises(RuntimeError, match="rendered OpenVPN override files"):
        await _sync(fake, [_entry(tunnel_address="198.51.100.9")])
    assert not fake.wrote


async def test_sync_fails_when_the_playback_fails() -> None:
    fake = _Fake([_config(users=_users("guest-1"))], playback_fails=True)
    with pytest.raises(RuntimeError, match="playback"):
        await _sync(fake, [_entry(tunnel_address="198.51.100.9")])


async def test_sync_result_and_next_free_result_match_their_schemas() -> None:
    validators = {op.op_id: Draft202012Validator(op.response_schema) for op in OPENVPN_CSC_OPS}
    base = _csc("old", "1", "198.51.100.5/24")
    compared = await _sync(
        _Fake([_config(csc=base)]),
        [_entry("n", tunnel_address="198.51.100.9"), _entry("zz", vpnid="7")],
        compare_only=True,
    )
    validators["pfsense.openvpn.csc.sync"].validate(compared)
    for config in (_config(), _config(servers=""), _config(csc=_csc("a", "1", "alias"))):
        validators["pfsense.openvpn.address.next_free"].validate(await _next_free(_Fake([config])))


async def test_no_secret_field_of_the_server_reaches_a_result() -> None:
    out = await _next_free(_Fake([_config()]))
    assert "SERVER-TLS-KEY-MUST-NOT-LEAK" not in json.dumps(out)


# ===========================================================================
# csc.sync -- the PHP fragment, run for real
# ===========================================================================

_PHP = shutil.which("php")
_needs_php = pytest.mark.skipif(
    _PHP is None and os.environ.get("MEHO_REQUIRE_PHP") != "1",
    reason="php not installed; set MEHO_REQUIRE_PHP=1 to fail instead of skip",
)

_PHP_STUBS = {
    "openvpn.inc": """<?php
function openvpn_resync_csc($settings) {
    global $meho_calls;
    $meho_calls[] = array('resync', $settings['common_name'], $settings['server_list']);
}
function openvpn_delete_csc($settings) {
    global $meho_calls;
    $meho_calls[] = array('delete_csc', $settings['common_name']);
}
""",
    "auth.inc": """<?php
function index_users() {
    global $config;
    $index = array();
    foreach ($config['system']['user'] as $i => $user) { $index[$user['name']] = $i; }
    return $index;
}
function local_user_set(&$user) {
    global $meho_calls;
    $meho_calls[] = array('local_user_set', $user['name']);
}
function local_user_set_password(&$user, $password) {
    global $meho_calls;
    $user['bcrypt-hash'] = 'hashed:' . strlen($password);
    $meho_calls[] = array('set_password', $user['name']);
}
function local_user_del($user) {
    global $meho_calls;
    $meho_calls[] = array('local_user_del', $user['name']);
}
function write_config($desc = 'Unknown') {
    global $meho_calls;
    $meho_calls[] = array('write_config', $desc);
    return true;
}
""",
    "run.php": """<?php
require_once('auth.inc');
require_once('openvpn.inc');
$config = json_decode(stream_get_contents(STDIN), true);
$meho_calls = array();
$userindex = null;
function meho_playback($file) { eval(file_get_contents($file)); }
meho_playback($argv[1]);
echo "\\n@@CONFIG@@" . json_encode(array('config' => $config, 'calls' => $meho_calls));
""",
}


def _run_php(tmp_path: Path, config: dict[str, Any], fragment: str) -> tuple[str, dict[str, Any]]:
    for name, body in _PHP_STUBS.items():
        (tmp_path / name).write_text(body)
    (tmp_path / "fragment").write_text(fragment)
    proc = subprocess.run(
        [
            _PHP or "php",
            "-n",
            "-d",
            f"include_path={tmp_path}",
            "-d",
            "display_errors=stderr",
            "-d",
            "error_reporting=E_ALL",
            str(tmp_path / "run.php"),
            str(tmp_path / "fragment"),
        ],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr
    printed, _, tail = proc.stdout.partition("\n@@CONFIG@@")
    return printed, json.loads(tail)


_CSC_USERS = ("old", "new", "bystander", "dup", "x", "third")


def _php_csc_config(csc: list[dict[str, Any]]) -> dict[str, Any]:
    """A config whose local users include every name the override tests use."""
    users = [{"name": "admin", "uid": "0", "scope": "system"}]
    users += [{"name": n, "uid": "2000", "scope": "user"} for n in _CSC_USERS]
    return _php_config(csc, users)


def _php_config(
    csc: list[dict[str, Any]], users: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "system": {
            "nextuid": "2000",
            "group": [{"name": "all", "member": ["0"]}],
            "user": users
            if users is not None
            else [{"name": "admin", "uid": "0", "scope": "system", "bcrypt-hash": "x"}],
        },
        "openvpn": {
            "openvpn-server": [{"vpnid": "1", "mode": "server_user"}],
            "openvpn-csc": csc,
        },
        "cert": [{"refid": "c1", "prv": "private"}],
    }


def _csc_row(name: str, vpnid: str, tunnel: str = "", routes: str = "") -> dict[str, Any]:
    return {
        "common_name": name,
        "server_list": vpnid,
        "description": f"d {name}",
        "tunnel_network": tunnel,
        "local_network": routes,
        "custom_options": "keep-me",
    }


def test_csc_fragment_shape_is_the_same_for_every_allowed_value() -> None:
    plan = _plan_for(_config(), [_entry("REFNAME", tunnel_address="198.51.100.9")])
    reference = build_csc_sync_playback(plan, 0)
    other = _plan_for(_config(), [_entry("a.b_c-9", tunnel_address="198.51.100.9")])
    assert build_csc_sync_playback(other, 0) == reference.replace("REFNAME", "a.b_c-9")
    assert all(not line.lstrip().startswith(("!", "=")) for line in reference.splitlines())
    assert reference.count("write_config(") == 1
    assert "unset(" not in reference  # the sync never deletes


@_needs_php
def test_csc_fragment_run_in_php_touches_only_the_named_entries(tmp_path: Path) -> None:
    before = _php_csc_config(
        [
            _csc_row("old", "1", "198.51.100.5/24", "192.0.2.0/25"),
            _csc_row("bystander", "1", "198.51.100.6/24", "192.0.2.0/24"),
        ]
    )
    plan = _plan_for(
        _config(csc=_csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")),
        [
            _entry("old", routes=["192.0.2.0/24"]),
            _entry("new", tunnel_address="198.51.100.9", routes=["192.0.2.0/24"]),
        ],
    )
    printed, out = _run_php(tmp_path, before, build_csc_sync_playback(plan, 2))
    assert printed == ""
    expected = copy.deepcopy(before)
    expected["openvpn"]["openvpn-csc"][0]["local_network"] = "192.0.2.0/25,192.0.2.0/24"
    expected["openvpn"]["openvpn-csc"].append(
        {
            "server_list": "1",
            "common_name": "new",
            "description": "meho: vpn user new",
            "tunnel_network": "198.51.100.9/24",
            "local_network": "192.0.2.0/24",
        }
    )
    assert out["config"] == expected
    assert out["calls"] == [
        ["resync", "new", "1"],
        ["resync", "old", "1"],
        ["write_config", "meho: sync OpenVPN client overrides"],
    ] or out["calls"] == [
        ["resync", "old", "1"],
        ["resync", "new", "1"],
        ["write_config", "meho: sync OpenVPN client overrides"],
    ]


@_needs_php
def test_csc_fragment_run_in_php_does_not_add_a_second_entry_or_edit_a_duplicate(
    tmp_path: Path,
) -> None:
    before = _php_csc_config(
        [_csc_row("dup", "1", "198.51.100.5/24"), _csc_row("dup", "1", "198.51.100.6/24")]
    )
    plan = _plan_for(
        _config(),
        [_entry("dup", tunnel_address="198.51.100.7"), _entry("x", tunnel_address="198.51.100.8")],
    )
    plan[0] = {**plan[0], "action": "update", "routes": ["192.0.2.0/24"]}
    # "dup" matches twice: the fragment's own check fails, so even "x" is not added.
    printed, out = _run_php(tmp_path, before, build_csc_sync_playback(plan, 2))
    assert printed == "MEHO_CSC_CHANGED=dup@1\n"
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_csc_fragment_run_in_php_keeps_a_route_added_after_the_plan(tmp_path: Path) -> None:
    """A route added in the web page between the plan and the write is not lost."""
    plan = _plan_for(
        _config(csc=_csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")),
        [_entry("old", routes=["192.0.2.0/24"]), _entry("new", tunnel_address="198.51.100.9")],
    )
    now = _php_csc_config([_csc_row("old", "1", "198.51.100.5/24", "192.0.2.0/25, 10.77.0.0/16")])
    printed, out = _run_php(tmp_path, now, build_csc_sync_playback(plan, 1))
    assert printed == "MEHO_CSC_CHANGED=old@1\n"
    assert out["config"] == now and out["calls"] == []


@_needs_php
@pytest.mark.parametrize(
    "now",
    [
        # Someone made an override for the new name meanwhile (for all servers).
        [_csc_row("old", "1", "198.51.100.5/24", "192.0.2.0/25"), _csc_row("new", "")],
        # Someone changed the address of the entry the plan extends.
        [_csc_row("old", "1", "198.51.100.8/24", "192.0.2.0/25")],
        # Someone added an override for another user.
        [
            _csc_row("old", "1", "198.51.100.5/24", "192.0.2.0/25"),
            _csc_row("third", "1", "198.51.100.9/24"),
        ],
    ],
)
def test_csc_fragment_run_in_php_writes_nothing_after_other_changes(
    tmp_path: Path, now: list[dict[str, Any]]
) -> None:
    plan = _plan_for(
        _config(csc=_csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")),
        [_entry("old", routes=["192.0.2.0/24"]), _entry("new", tunnel_address="198.51.100.9")],
    )
    config = _php_csc_config(now)
    printed, out = _run_php(tmp_path, config, build_csc_sync_playback(plan, 1))
    assert printed.startswith("MEHO_CSC_CHANGED=")
    assert out["config"] == config and out["calls"] == []


@_needs_php
def test_csc_fragment_run_in_php_accepts_spaces_in_an_unchanged_route_list(
    tmp_path: Path,
) -> None:
    """The parser trims the route list; the fragment's check compares it the same way."""
    xml = _config(csc=_csc("old", "1", " 198.51.100.5/24 ", "192.0.2.0/25 , 10.7.0.0/16"))
    plan = _plan_for(xml, [_entry("old", routes=["192.0.2.0/24"])])
    now = _php_csc_config([_csc_row("old", "1", " 198.51.100.5/24 ", "192.0.2.0/25 , 10.7.0.0/16")])
    printed, out = _run_php(tmp_path, now, build_csc_sync_playback(plan, 1))
    assert printed == ""
    assert out["config"]["openvpn"]["openvpn-csc"][0]["local_network"] == (
        "192.0.2.0/25,10.7.0.0/16,192.0.2.0/24"
    )


@_needs_php
def test_csc_fragment_run_in_php_creates_the_section_when_missing(tmp_path: Path) -> None:
    before = _php_csc_config([])
    del before["openvpn"]["openvpn-csc"]
    plan = _plan_for(_config(), [_entry("new", tunnel_address="198.51.100.9")])
    _, out = _run_php(tmp_path, before, build_csc_sync_playback(plan, 0))
    (entry,) = out["config"]["openvpn"]["openvpn-csc"]
    assert entry["common_name"] == "new" and "local_network" not in entry


# ===========================================================================
# user.create
# ===========================================================================


class _FakeVault:
    """Stands in for ``vault_client_for_operator``: one KV v2 secret with versions."""

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        fail: bool = False,
        read_fails: bool = False,
        concurrent: bool = False,
        mount_version: Any = "2",
    ) -> None:
        self.mount_version = mount_version  # what the mount info says (None: no options)
        self.data = data  # None: nothing is stored at the path yet
        self.version = 0 if data is None else 3
        self.fail = fail
        self.read_fails = read_fails
        self.concurrent = concurrent
        self.writes: list[tuple[dict[str, Any], int, str]] = []
        self.opened = 0

    def __call__(self, operator: Any, **kw: Any) -> Any:
        fake = self

        @asynccontextmanager
        async def _ctx() -> AsyncIterator[Any]:
            fake.opened += 1
            client = MagicMock()

            def read(path: str, mount_point: str, raise_on_deleted_version: bool) -> Any:
                assert raise_on_deleted_version is False
                if fake.read_fails:
                    raise hvac.exceptions.Forbidden(f"denied at {path}")
                if fake.data is None:
                    raise hvac.exceptions.InvalidPath()
                return {"data": {"data": dict(fake.data), "metadata": {"version": fake.version}}}

            def create(
                path: str, secret: dict[str, Any], cas: int, mount_point: str
            ) -> dict[str, Any]:
                if fake.fail:
                    raise RuntimeError(f"boom {secret}")  # a message that would carry the secret
                if fake.concurrent:
                    # Someone else wrote a new version since this call read the path.
                    fake.version += 1
                if cas != fake.version:
                    raise hvac.exceptions.InvalidRequest("check-and-set parameter did not match")
                fake.version += 1
                fake.data = dict(secret)
                fake.writes.append((dict(secret), cas, mount_point + "/" + path))
                return {}

            def mount_info(url: str) -> dict[str, Any]:
                assert url.startswith("/v1/sys/internal/ui/mounts/")
                options = None if fake.mount_version is None else {"version": fake.mount_version}
                return {"data": {"type": "kv", "options": options}}

            client.adapter.get = mount_info
            client.secrets.kv.v2.read_secret_version = read
            client.secrets.kv.v2.create_or_update_secret = create
            yield client

        return _ctx()


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> _FakeVault:
    fake = _FakeVault()
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake)
    return fake


_REAL_TARGET_SECRET_PATHS = user_module._target_secret_paths


@pytest.fixture(autouse=True)
def target_paths(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The credential paths of the targets; empty unless a test adds one."""
    paths: set[str] = set()

    async def _paths() -> set[str]:
        return paths

    monkeypatch.setattr(user_module, "_target_secret_paths", _paths)
    return paths


_CREATE = {"name": "guest-1", "vault_path": _VAULT_PATH}
_CREATED_OUT = f"MEHO_VPN_CREATED_UID=2000\nMEHO_VPN_PASSWORD={_PASSWORD}\n"


async def _create(fake: _Fake, params: dict[str, Any] | None = None, op: Any = "default") -> Any:
    operator = _operator() if op == "default" else op
    return await fake.openvpn_user_create(None, params or _CREATE, operator)  # type: ignore[arg-type]


def _created_fake(**kw: Any) -> _Fake:
    before = _config()
    after = _config(users=_user("guest-1", "2000"))
    return _Fake([before, after], playback_out=_CREATED_OUT, **kw)


async def test_create_stores_the_password_in_vault_and_never_returns_it(vault: _FakeVault) -> None:
    fake = _created_fake()
    out = await _create(fake)
    assert out["status"] == "created" and out["applied"] and out["verified"]
    assert out["vault"] == {
        "mount": "secret",
        "path": _VAULT_PATH,
        "field": "password",
        "stored": True,
    }
    assert vault.writes == [({"password": _PASSWORD}, 0, f"secret/{_VAULT_PATH}")]
    # The password is in no result and in no command sent to the firewall.
    assert _PASSWORD not in json.dumps(out)
    assert all(_PASSWORD not in c for c in fake.commands)
    assert all(_PASSWORD not in c for c in fake.staged)


async def test_create_keeps_the_other_fields_and_writes_with_the_version_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_vault = _FakeVault(data={"username": "guest-1"})
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    out = await _create(_created_fake(), {**_CREATE, "vault_field": "pw", "vault_mount": "secret"})
    assert out["status"] == "created"
    assert fake_vault.writes == [
        ({"username": "guest-1", "pw": _PASSWORD}, 3, f"secret/{_VAULT_PATH}")
    ]


async def test_create_refuses_a_vault_field_that_already_holds_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_vault = _FakeVault(data={"password": "older"})
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    fake = _created_fake()
    with pytest.raises(PfSenseRefusedError, match=r"refused \(vault_field_exists\)") as err:
        await _create(fake)
    assert err.value.code == "vault_field_exists"
    assert "older" not in str(err.value)
    assert not fake.wrote and fake_vault.writes == []
    assert fake_vault.data == {"password": "older"}


async def test_create_refuses_a_vault_path_that_is_a_targets_credential_path(
    vault: _FakeVault, target_paths: set[str]
) -> None:
    target_paths.add(_VAULT_PATH)
    fake = _created_fake()
    with pytest.raises(PfSenseRefusedError, match=r"refused \(vault_path_is_target_secret\)"):
        await _create(fake)
    assert not fake.wrote and vault.opened == 0


async def test_target_secret_paths_cut_the_scheme() -> None:
    """The real lookup reads every target row and compares Vault paths without the scheme."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target

    async with get_sessionmaker()() as session:
        session.add_all(
            [
                Target(
                    tenant_id=_TENANT_ID,
                    name=f"vpn-t{i}",
                    host="pf.test",
                    product="pfsense",
                    secret_ref=ref,
                )
                for i, ref in enumerate(
                    [f"vault:{_VAULT_PATH}", "gsm:proj/secret#pw", "tenants/x//y/"]
                )
            ]
        )
        await session.commit()
    paths = await _REAL_TARGET_SECRET_PATHS()
    assert {_VAULT_PATH, "tenants/x/y"} <= paths
    assert "proj/secret#pw" not in paths


async def test_create_stops_when_the_vault_path_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_vault = _FakeVault(read_fails=True)
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    fake = _created_fake()
    with pytest.raises(RuntimeError, match=r"reading the Vault path failed \(Forbidden\)") as err:
        await _create(fake)
    assert "denied" not in str(err.value)
    assert not fake.wrote and fake_vault.writes == []


async def test_a_concurrent_vault_writer_is_not_overwritten_and_the_user_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_vault = _FakeVault(data={"username": "guest-1"}, concurrent=True)
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    fake = _created_fake()
    with pytest.raises(RuntimeError, match="removed again") as err:
        await _create(fake)
    assert "InvalidRequest" in str(err.value) and _PASSWORD not in str(err.value)
    assert fake_vault.writes == [] and fake_vault.data == {"username": "guest-1"}
    assert len(fake.played) == 2  # the create, then the undo


async def test_create_leaves_an_existing_user_alone(vault: _FakeVault) -> None:
    fake = _Fake([_config(users=_user("guest-1", "2000"))])
    out = await _create(fake)
    assert out["status"] == "exists" and out["nothing_to_do"] is True
    assert not fake.wrote and vault.opened == 0


async def test_create_refuses_a_duplicate_name(vault: _FakeVault) -> None:
    fake = _Fake([_config(users=_user("guest-1", "2000") + _user("guest-1", "2001"))])
    with pytest.raises(PfSenseRefusedError, match=r"refused \(ambiguous\)"):
        await _create(fake)
    assert not fake.wrote and vault.opened == 0


async def test_vault_failure_removes_the_user_again_and_hides_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(fail=True))
    fake = _created_fake()
    with pytest.raises(RuntimeError) as err:
        await _create(fake)
    assert "removed again" in str(err.value)
    assert _PASSWORD not in str(err.value)
    # The Vault error that started it carries no message of the client and no password.
    inner = err.value.__context__
    assert inner is not None and inner.__cause__ is None and inner.__suppress_context__
    assert _PASSWORD not in str(inner) and "boom" not in str(inner)
    assert len(fake.played) == 2  # the create, then the undo
    assert any("local_user_del" in s for s in fake.staged)


async def test_vault_failure_and_failed_undo_names_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(fail=True))

    class _NoUndo(_Fake):
        async def _run_command(
            self,
            target: Any,
            cmd: str,
            *,
            operator: Any = None,
            timeout: float = 30.0,
            stdin: str | None = None,
        ) -> Any:
            if cmd.startswith("pfSsh.php playback meho_openvpn_user_undo"):
                return _proc("", 1)
            return await super()._run_command(target, cmd, operator=operator)

    fake = _NoUndo([_config(), _config(users=_user("guest-1", "2000"))], playback_out=_CREATED_OUT)
    with pytest.raises(RuntimeError, match=r"'guest-1' could not be removed again"):
        await _create(fake)


async def test_create_without_a_password_from_the_firewall_undoes_the_user(
    vault: _FakeVault,
) -> None:
    fake = _Fake(
        [_config(), _config(users=_user("guest-1", "2000"))],
        playback_out="MEHO_VPN_CREATED_UID=2000\n",
    )
    with pytest.raises(RuntimeError, match="did not return a password"):
        await _create(fake)
    assert vault.writes == []
    assert len(fake.played) == 2
    # The undo removes only the user with the uid this call made, and no override.
    undo = next(c for c in fake.staged if "meho_openvpn_user_undo" in c)
    assert "=== '2000'" in undo and "openvpn_delete_csc" not in undo


async def test_create_does_not_touch_a_user_someone_else_made_meanwhile(
    vault: _FakeVault,
) -> None:
    """The name was free at the first read, but taken when the fragment ran."""
    theirs = _config(users=_user("guest-1", "2007"), csc=_csc("guest-1", "1", "198.51.100.5/24"))
    fake = _Fake([_config(), theirs], playback_out="MEHO_VPN_EXISTS\n")
    out = await _create(fake)
    assert out["status"] == "exists" and out["applied"] is False
    assert out["vault"]["stored"] is False
    assert len(fake.played) == 1  # no undo
    assert vault.writes == []


async def test_create_without_a_clear_answer_from_the_firewall_undoes_nothing(
    vault: _FakeVault,
) -> None:
    fake = _Fake([_config(), _config(users=_user("guest-1", "2000"))], playback_out="")
    with pytest.raises(RuntimeError, match="did not say whether it made"):
        await _create(fake)
    assert len(fake.played) == 1 and vault.writes == []


async def test_create_fails_when_the_read_back_has_no_user(vault: _FakeVault) -> None:
    fake = _Fake([_config()], playback_out=_CREATED_OUT)
    with pytest.raises(RuntimeError, match="verification failed") as err:
        await _create(fake)
    # The password was stored before the check, so nobody loses it.
    assert vault.writes == [({"password": _PASSWORD}, 0, f"secret/{_VAULT_PATH}")]
    assert "stored in Vault" in str(err.value) and _PASSWORD not in str(err.value)
    assert len(fake.played) == 1  # nothing undone


async def test_create_fails_when_the_read_back_has_another_uid(vault: _FakeVault) -> None:
    fake = _Fake([_config(), _config(users=_user("guest-1", "2009"))], playback_out=_CREATED_OUT)
    with pytest.raises(RuntimeError, match="uid 2000"):
        await _create(fake)
    assert len(vault.writes) == 1


async def test_create_fails_when_another_user_changed(vault: _FakeVault) -> None:
    after = _config(users=_user("guest-1", "2000") + _user("surprise", "2002"))
    fake = _Fake([_config(), after], playback_out=_CREATED_OUT)
    with pytest.raises(RuntimeError, match="other users changed") as err:
        await _create(fake)
    assert "stored in Vault" in str(err.value)
    assert len(vault.writes) == 1 and len(fake.played) == 1


async def test_create_keeps_the_password_when_the_read_back_fails(vault: _FakeVault) -> None:
    class _ReadBackFails(_Fake):
        reads = 0

        async def _run_command(
            self,
            target: Any,
            cmd: str,
            *,
            operator: Any = None,
            timeout: float = 30.0,
            stdin: str | None = None,
        ) -> Any:
            if cmd == "cat /cf/conf/config.xml":
                self.reads += 1
                if self.reads == 2:
                    return _proc("", 1)
            return await super()._run_command(target, cmd, operator=operator)

    fake = _ReadBackFails([_config()], playback_out=_CREATED_OUT)
    with pytest.raises(RuntimeError, match="could not be read again") as err:
        await _create(fake)
    assert f"secret/{_VAULT_PATH}" in str(err.value) and _PASSWORD not in str(err.value)
    assert len(vault.writes) == 1 and len(fake.played) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"name": "x'; //", "vault_path": _VAULT_PATH},
        {"name": "guest-1"},
        {"name": "guest-1", "vault_path": "a/../b"},
        {"name": "guest-1", "vault_path": "a//b"},
        {"name": "guest-1", "vault_path": "a/b/"},
        {"name": "guest-1", "vault_path": "/abs"},
        {"name": "guest-1", "vault_path": "secret/" + _VAULT_PATH},
        {"name": "guest-1", "vault_path": "a%2e%2e/b"},
        {"name": "guest-1", "vault_path": _VAULT_PATH, "vault_mount": "a/b"},
        {"name": "guest-1", "vault_path": _VAULT_PATH, "vault_field": "x y"},
        {"name": "guest-1", "vault_path": _VAULT_PATH, "description": "a\nb"},
    ],
)
async def test_create_rejects_bad_input_before_any_ssh_call(
    vault: _FakeVault, params: dict[str, Any]
) -> None:
    fake = _created_fake()
    with pytest.raises(ValueError):
        await _create(fake, params)
    assert fake.commands == [] and vault.opened == 0


async def test_create_refuses_a_vault_path_outside_the_tenant_before_any_ssh_call(
    vault: _FakeVault,
) -> None:
    from meho_backplane.connectors.vault.tenant_scope import VaultTenantScopeError

    fake = _created_fake()
    with pytest.raises(VaultTenantScopeError):
        await _create(fake, {"name": "guest-1", "vault_path": "tenants/someone-else/vpn"})
    assert fake.commands == [] and vault.opened == 0


async def test_create_needs_an_operator(vault: _FakeVault) -> None:
    fake = _created_fake()
    with pytest.raises(ValueError, match="needs an operator"):
        await _create(fake, op=None)
    assert fake.commands == []


def test_create_fragment_shape_and_password_alphabet() -> None:
    body = user_module.build_user_create_playback("guest-1", "A note")
    assert all(not line.lstrip().startswith(("!", "=")) for line in body.splitlines())
    assert body.count("write_config(") == 1
    assert "random_int" in body
    with pytest.raises(ValueError, match="name must match"):
        user_module.build_user_create_playback("x'; system('id'); //", "d")
    # An allowed name only fills the string literals.
    assert user_module.build_user_create_playback("a.b", "d") == body.replace(
        "guest-1", "a.b"
    ).replace("A note", "d")


@_needs_php
def test_create_fragment_run_in_php(tmp_path: Path) -> None:
    before = _php_config([])
    printed, out = _run_php(
        tmp_path, before, user_module.build_user_create_playback("guest-1", "A note")
    )
    found = re.fullmatch(
        r"MEHO_VPN_CREATED_UID=2000\nMEHO_VPN_PASSWORD=([A-Za-z0-9]{24})\n", printed
    )
    assert found, printed
    users = out["config"]["system"]["user"]
    assert [u["name"] for u in users] == ["admin", "guest-1"]
    new = users[1]
    assert (new["scope"], new["uid"], new["descr"]) == ("user", "2000", "A note")
    assert "bcrypt-hash" in new and found.group(1) not in json.dumps(out["config"])
    assert out["config"]["system"]["nextuid"] == 2001
    assert out["config"]["system"]["group"][0]["member"] == ["0", "2000"]
    assert out["config"]["openvpn"] == before["openvpn"] and out["config"]["cert"] == before["cert"]
    assert out["calls"] == [
        ["set_password", "guest-1"],
        ["local_user_set", "guest-1"],
        ["write_config", "meho: add VPN user guest-1"],
    ]


@_needs_php
def test_create_fragment_run_in_php_does_nothing_when_the_name_exists(tmp_path: Path) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user", "bcrypt-hash": "old"},
    ]
    before = _php_config([], users)
    printed, out = _run_php(
        tmp_path, before, user_module.build_user_create_playback("guest-1", "d")
    )
    assert printed == "MEHO_VPN_EXISTS\n"
    assert out["config"] == before and out["calls"] == []


# ===========================================================================
# user.delete
# ===========================================================================

# The ``status 2`` answer of OpenVPN 2.6 (``multi.c``), shortened.
_CLIENT_HEADER = (
    "HEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Virtual IPv6 Address,"
    "Bytes Received,Bytes Sent,Connected Since,Connected Since (time_t),Username,Client ID,"
    "Peer ID,Data Channel Cipher\n"
)
_SOCK_EMPTY = (f"TITLE,x\n{_CLIENT_HEADER}END\n", 0)


def _session(name: str, common_name: str | None = None) -> str:
    cn = common_name or name
    return (
        f"TITLE,x\n{_CLIENT_HEADER}"
        f"CLIENT_LIST,{cn},203.0.113.77:1194,198.51.100.5,,1,2,now,1,{name},1,1,AES-256-GCM\n"
        "END\n"
    )


def _vpn_config(*, with_user: bool = True) -> str:
    users = (
        _user("guest-1", "2000") + _user("other", "2001") if with_user else _user("other", "2001")
    )
    csc = _csc("guest-1", "1", "198.51.100.5/24") + _csc("guest-1", "2", "203.0.113.5/24")
    csc += _csc("other", "1", "198.51.100.6/24")
    return _config(users=users, csc=csc if with_user else _csc("other", "1", "198.51.100.6/24"))


async def _delete(fake: _Fake, **params: Any) -> dict[str, Any]:
    return await fake.openvpn_user_delete(None, {"name": "guest-1", **params}, _operator())  # type: ignore[arg-type]


async def test_delete_removes_the_user_and_its_overrides() -> None:
    fake = _Fake(
        [_vpn_config(), _vpn_config(with_user=False)],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
    )
    out = await _delete(fake)
    assert out["status"] == "deleted" and out["applied"] and out["verified"]
    assert out["overrides_removed"] == 2 and out["connected_sessions"] == 0
    assert len(fake.played) == 1


async def test_delete_is_refused_while_the_user_is_connected() -> None:
    fake = _Fake([_vpn_config()], sockets={"1": (_session("guest-1"), 0), "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(connected\).*1 session"):
        await _delete(fake)
    assert not fake.wrote


async def test_delete_is_refused_when_the_login_name_differs_from_the_certificate_name() -> None:
    """On a server that checks a certificate too, the certificate name can differ."""
    servers = _server("1", _NET_UDP, mode="server_tls_user")
    config = _config(servers=servers, users=_user("guest-1", "2000"))
    fake = _Fake([config], sockets={"1": (_session("guest-1", common_name="laptop-77"), 0)})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(connected\).*1 session"):
        await _delete(fake)
    assert not fake.wrote


async def test_a_certificate_named_like_the_user_counts_too() -> None:
    fake = _Fake(
        [_vpn_config()],
        sockets={"1": (_session("someone", common_name="guest-1"), 0), "2": _SOCK_EMPTY},
    )
    with pytest.raises(PfSenseRefusedError, match=r"refused \(connected\)"):
        await _delete(fake)


async def test_a_client_row_without_a_username_column_is_unknown() -> None:
    answer = ("TITLE,x\nCLIENT_LIST,other,203.0.113.77:1194\nEND\n", 0)
    fake = _Fake([_vpn_config()], sockets={"1": answer, "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(connection_unknown\)"):
        await _delete(fake)
    assert not fake.wrote


async def test_a_session_of_another_user_does_not_block_the_delete() -> None:
    fake = _Fake(
        [_vpn_config(), _vpn_config(with_user=False)],
        sockets={"1": (_session("other"), 0), "2": _SOCK_EMPTY},
    )
    assert (await _delete(fake))["status"] == "deleted"


@pytest.mark.parametrize("answer", [("", 1), ("garbage without the end marker\n", 0), ("", 0)])
async def test_delete_is_refused_when_the_connection_state_is_unknown(
    answer: tuple[str, int],
) -> None:
    fake = _Fake([_vpn_config()], sockets={"1": _SOCK_EMPTY, "2": answer})
    with pytest.raises(PfSenseRefusedError) as info:
        await _delete(fake)
    assert info.value.code == "connection_unknown"
    assert not fake.wrote


async def test_force_deletes_a_connected_user_without_asking_the_servers() -> None:
    fake = _Fake([_vpn_config(), _vpn_config(with_user=False)])
    out = await _delete(fake, force=True)
    assert out["status"] == "deleted" and out["force"] is True
    assert not any("sock" in c for c in fake.commands)


async def test_delete_skips_a_disabled_server() -> None:
    servers = _server("1", _NET_UDP) + _server("2", _NET_TCP, disabled=True)
    config = _config(servers=servers, users=_user("guest-1", "2000"))
    fake = _Fake([config, _config(servers=servers)], sockets={"1": _SOCK_EMPTY})
    assert (await _delete(fake))["status"] == "deleted"
    assert not any("/server2/sock" in c for c in fake.commands)


async def test_delete_of_a_user_that_is_already_gone_is_a_normal_answer() -> None:
    fake = _Fake([_vpn_config()])
    out = await fake.openvpn_user_delete(None, {"name": "nobody"}, _operator())  # type: ignore[arg-type]
    assert out["status"] == "not_found" and not out["applied"] and not fake.wrote
    Draft202012Validator(user_module._DELETE_RESPONSE_SCHEMA).validate(out)


async def test_delete_refuses_the_admin_account() -> None:
    fake = _Fake([_vpn_config()])
    with pytest.raises(PfSenseRefusedError, match=r"refused \(protected\)"):
        await fake.openvpn_user_delete(None, {"name": "admin"}, _operator())  # type: ignore[arg-type]
    assert not fake.wrote


async def test_delete_refuses_a_duplicate_name() -> None:
    config = _config(users=_user("guest-1", "2000") + _user("guest-1", "2001"))
    fake = _Fake([config])
    with pytest.raises(PfSenseRefusedError, match=r"refused \(ambiguous\)"):
        await _delete(fake)
    assert not fake.wrote


@pytest.mark.parametrize("force", [False, True])
async def test_delete_is_refused_while_tagged_rules_remain(force: bool) -> None:
    """A tagged rule grants access by address; the delete would free that address."""
    rule = _tagged_rule("123456789", "guest-1", "198.51.100.5")
    users = _user("guest-1", "2000") + _user("other", "2001")
    fake = _Fake(
        [_config(users=users, csc=_csc("guest-1", "1", "198.51.100.5/24"), rules=rule)],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
    )
    with pytest.raises(PfSenseRefusedError) as info:
        await _delete(fake, force=force)
    assert info.value.code == "rules_remain"
    assert "ridentifier 123456789" in info.value.guidance
    assert "pfsense.filter.rule.delete" in info.value.guidance
    assert not fake.wrote


async def test_a_refused_delete_keeps_the_address_away_from_the_next_user() -> None:
    """After the refused delete, next_free does not hand out the old user's address."""
    rule = _tagged_rule("123456789", "guest-1", "198.51.100.2")
    config = _config(
        users=_user("guest-1", "2000"),
        csc=_csc("guest-1", "1", "198.51.100.2/24"),
        rules=rule,
    )
    fake = _Fake([config], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(rules_remain\)"):
        await _delete(fake)
    free = await _next_free(fake)
    assert free["status"] == "found" and free["address_offset"] == 3


async def test_a_tagged_rule_of_another_user_does_not_block_the_delete() -> None:
    rule = _tagged_rule("123456789", "other", "198.51.100.6")
    after = _config(users=_user("other", "2001"), rules=rule)
    fake = _Fake(
        [_config(users=_user("guest-1", "2000") + _user("other", "2001"), rules=rule), after],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
    )
    out = await _delete(fake)
    assert out["status"] == "deleted" and out["applied"]


async def test_delete_is_refused_when_the_fragment_finds_a_new_tagged_rule() -> None:
    fake = _Fake(
        [_vpn_config()],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
        playback_out="MEHO_VPN_RULES_REMAIN=1\n",
    )
    with pytest.raises(PfSenseRefusedError, match=r"refused \(rules_remain\)"):
        await _delete(fake)
    assert len(fake.played) == 1


async def test_delete_is_refused_when_the_fragment_finds_a_new_group_or_permission() -> None:
    fake = _Fake(
        [_vpn_config()],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
        playback_out="MEHO_VPN_PROTECTED\n",
    )
    with pytest.raises(PfSenseRefusedError, match=r"refused \(protected\)"):
        await _delete(fake)
    assert len(fake.played) == 1


async def test_delete_fails_when_the_rule_list_cannot_be_read() -> None:
    config = _vpn_config().replace("<filter></filter>", "")
    fake = _Fake([config], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(RuntimeError, match="firewall rules could not be read"):
        await _delete(fake)
    assert not fake.wrote


async def test_delete_fails_when_the_user_is_still_there_after_the_write() -> None:
    fake = _Fake([_vpn_config()], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(RuntimeError, match="verification failed"):
        await _delete(fake)


async def test_delete_fails_when_an_override_of_another_user_vanished() -> None:
    after = _config(users=_user("other", "2001"))  # "other"'s override is gone too
    fake = _Fake([_vpn_config(), after], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(RuntimeError, match="verification failed"):
        await _delete(fake)


async def test_delete_rejects_a_hostile_name_before_any_ssh_call() -> None:
    fake = _Fake([_vpn_config()])
    with pytest.raises(ValueError, match="name must match"):
        await fake.openvpn_user_delete(None, {"name": "x'; //"}, _operator())  # type: ignore[arg-type]
    assert fake.commands == []


def test_delete_fragment_shape() -> None:
    body = user_module.build_user_delete_playback("guest-1")
    assert all(not line.lstrip().startswith(("!", "=")) for line in body.splitlines())
    assert body.count("write_config(") == 1
    assert "(string)$config['system']['user'][$meho_idx]['uid'] !== '0'" in body
    with pytest.raises(ValueError, match="name must match"):
        user_module.build_user_delete_playback("a b")


@_needs_php
def test_delete_fragment_run_in_php(tmp_path: Path) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
        {"name": "other", "uid": "2001", "scope": "user"},
    ]
    before = _php_config(
        [
            _csc_row("guest-1", "1", "198.51.100.5/24"),
            _csc_row("other", "1", "198.51.100.6/24"),
            _csc_row("guest-1", "2", "203.0.113.5/24"),
        ],
        users,
    )
    _, out = _run_php(tmp_path, before, user_module.build_user_delete_playback("guest-1"))
    assert [u["name"] for u in out["config"]["system"]["user"]] == ["admin", "other"]
    assert [e["common_name"] for e in out["config"]["openvpn"]["openvpn-csc"]] == ["other"]
    assert out["config"]["openvpn"]["openvpn-csc"][0] == before["openvpn"]["openvpn-csc"][1]
    assert out["config"]["cert"] == before["cert"]
    assert out["calls"] == [
        ["delete_csc", "guest-1"],
        ["delete_csc", "guest-1"],
        ["local_user_del", "guest-1"],
        ["write_config", "meho: delete VPN user guest-1"],
    ]


@_needs_php
def test_delete_fragment_run_in_php_writes_nothing_while_a_tagged_rule_remains(
    tmp_path: Path,
) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    before["filter"] = {
        "rule": [
            {"tracker": "1", "descr": "meho-vpn-user:other"},
            {"tracker": "2", "descr": " meho-vpn-user:guest-1 "},
        ]
    }
    printed, out = _run_php(tmp_path, before, user_module.build_user_delete_playback("guest-1"))
    assert printed.strip() == "MEHO_VPN_RULES_REMAIN=1"
    assert out["config"] == before and out["calls"] == []


@pytest.mark.parametrize(
    ("change", "printed"),
    [
        ("priv", "MEHO_VPN_PROTECTED"),
        ("group", "MEHO_VPN_PROTECTED"),
        ("new_address", "MEHO_VPN_RULES_REMAIN=1"),
    ],
)
@_needs_php
def test_delete_fragment_run_in_php_checks_the_config_as_it_is_now(
    tmp_path: Path, change: str, printed: str
) -> None:
    # The handler read the user with no group, no permission and the override
    # address 198.51.100.5. Someone changed the config before the playback.
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    if change == "priv":
        before["system"]["user"][1]["priv"] = ["page-all"]
    elif change == "group":
        before["system"]["group"].append({"name": "admins", "member": ["0", "2000"]})
    else:
        before["openvpn"]["openvpn-csc"][0]["tunnel_network"] = "198.51.100.9/24"
        rule = {"tracker": "7", "interface": "openvpn", "source": {"address": "198.51.100.9"}}
        before["filter"] = {"rule": [rule]}
    fragment = user_module.build_user_delete_playback("guest-1", ["198.51.100.5"])
    out_text, out = _run_php(tmp_path, before, fragment)
    assert out_text.strip() == printed
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_delete_fragment_run_in_php_ignores_the_all_group_and_empty_permissions(
    tmp_path: Path,
) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user", "priv": [""]},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    before["system"]["group"] = [
        {"name": "all", "member": ["0", "2000"]},
        {"name": "admins", "member": "0"},
    ]
    printed, out = _run_php(tmp_path, before, user_module.build_user_delete_playback("guest-1"))
    assert printed.strip() == ""
    assert [u["name"] for u in out["config"]["system"]["user"]] == ["admin"]


@_needs_php
def test_undo_fragment_run_in_php_keeps_the_overrides_of_the_name(tmp_path: Path) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    _, out = _run_php(tmp_path, before, user_module.build_user_undo_playback("guest-1", "2000"))
    assert [u["name"] for u in out["config"]["system"]["user"]] == ["admin"]
    assert out["config"]["openvpn"] == before["openvpn"]
    assert out["calls"] == [
        ["local_user_del", "guest-1"],
        ["write_config", "meho: undo new VPN user guest-1"],
    ]


@_needs_php
def test_undo_fragment_run_in_php_leaves_a_user_with_another_uid(tmp_path: Path) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2007", "scope": "user"},
    ]
    before = _php_config([], users)
    _, out = _run_php(tmp_path, before, user_module.build_user_undo_playback("guest-1", "2000"))
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_undo_fragment_run_in_php_prints_a_marker_when_it_removed_the_user(
    tmp_path: Path,
) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    fragment = user_module.build_user_undo_playback("guest-1", "2000")
    printed, _ = _run_php(tmp_path, _php_config([], users), fragment)
    assert printed == "MEHO_VPN_REMOVED\n"
    printed, _ = _run_php(tmp_path, _php_config([], users[:1]), fragment)
    assert printed == ""


async def test_an_undo_without_the_removed_marker_says_the_user_was_not_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(fail=True))
    fake = _created_fake(undo_out="")
    with pytest.raises(RuntimeError, match="'guest-1' was not removed again") as err:
        await _create(fake)
    assert "was removed again" not in str(err.value)
    assert "password is not stored" in str(err.value)
    assert len(fake.played) == 2  # the create, then the undo


@pytest.mark.parametrize("version", [None, "3", True, 0])
async def test_create_refuses_a_vault_answer_without_a_version(
    monkeypatch: pytest.MonkeyPatch, version: Any
) -> None:
    fake_vault = _FakeVault(data={"username": "guest-1"})
    fake_vault.version = version
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    fake = _created_fake()
    with pytest.raises(PfSenseRefusedError, match=r"refused \(vault_version_unknown\)"):
        await _create(fake)
    assert not fake.wrote and fake_vault.writes == []


# ===========================================================================
# User names that start with a dot (pfSense uses /home/<name>)
# ===========================================================================

_DOT_NAMES = [".", "..", "...", ".x"]


@pytest.mark.parametrize("name", _DOT_NAMES)
async def test_a_name_that_starts_with_a_dot_is_refused_before_the_firewall(
    vault: _FakeVault, name: str
) -> None:
    for op in user_module.OPENVPN_USER_OPS:
        assert re.fullmatch(op.parameter_schema["properties"]["name"]["pattern"], name) is None
    fake = _Fake([_config()])
    with pytest.raises(ValueError, match="no leading dot"):
        await _create(fake, {**_CREATE, "name": name})
    with pytest.raises(ValueError, match="no leading dot"):
        await fake.openvpn_user_delete(None, {"name": name}, None)  # type: ignore[arg-type]
    for build in (
        lambda: user_module.build_user_create_playback(name, "d"),
        lambda: user_module.build_user_delete_playback(name),
        lambda: user_module.build_user_undo_playback(name, "2000"),
    ):
        with pytest.raises(ValueError, match="no leading dot"):
            build()
    assert fake.commands == [] and vault.opened == 0


def _with_name(fragment: str, name: str) -> str:
    """Put *name* into a fragment built for ``guest-1``, past the handler's own check."""
    line = "$meho_name = 'guest-1';"
    assert fragment.count(line) == 1
    return fragment.replace(line, f"$meho_name = '{name}';")


@_needs_php
@pytest.mark.parametrize("name", _DOT_NAMES)
@pytest.mark.parametrize("kind", ["create", "delete", "undo"])
def test_the_fragments_run_in_php_stop_for_a_name_that_starts_with_a_dot(
    tmp_path: Path, name: str, kind: str
) -> None:
    users = [{"name": "admin", "uid": "0", "scope": "system"}]
    if kind != "create":
        users.append({"name": name, "uid": "2000", "scope": "user"})
    before = _php_config([_csc_row(name, "1", "198.51.100.5/24")], users)
    built = {
        "create": lambda: user_module.build_user_create_playback("guest-1", "d"),
        "delete": lambda: user_module.build_user_delete_playback("guest-1"),
        "undo": lambda: user_module.build_user_undo_playback("guest-1", "2000"),
    }[kind]()
    printed, out = _run_php(tmp_path, before, _with_name(built, name))
    assert printed == "MEHO_VPN_DOT_NAME\n"
    assert out["config"] == before and out["calls"] == []


def test_undo_fragment_needs_a_numeric_uid() -> None:
    with pytest.raises(ValueError, match="uid"):
        user_module.build_user_undo_playback("guest-1", "1' || '1")


@_needs_php
@pytest.mark.parametrize("name", ["admin", "nobody"])
def test_delete_fragment_run_in_php_writes_nothing_when_its_own_guard_fails(
    tmp_path: Path, name: str
) -> None:
    before = _php_config([_csc_row("admin", "1")])
    _, out = _run_php(tmp_path, before, user_module.build_user_delete_playback(name))
    assert out["config"] == before and out["calls"] == []


# ===========================================================================
# System names, failed or cancelled playback, and the VPN-user-only delete
# ===========================================================================


@pytest.mark.parametrize(
    ("printed", "code"),
    [
        ("MEHO_VPN_SYSTEM_NAME\n", "system_name"),
        ("MEHO_VPN_SYSTEM_NAMES_UNREADABLE\n", "system_names_unreadable"),
        ("MEHO_VPN_DOT_NAME\n", "dot_name"),
    ],
)
async def test_create_refuses_a_system_name_of_the_firewall(
    vault: _FakeVault, printed: str, code: str
) -> None:
    fake = _Fake([_config()], playback_out=printed)
    with pytest.raises(PfSenseRefusedError, match=rf"refused \({code}\)"):
        await _create(fake)
    assert len(fake.played) == 1 and vault.writes == []


async def test_a_failed_playback_says_the_user_may_exist_without_a_stored_password(
    vault: _FakeVault,
) -> None:
    fake = _Fake([_config()], playback_fails=True)
    with pytest.raises(RuntimeError, match="may exist on the firewall now") as err:
        await _create(fake)
    assert "password is not stored" in str(err.value)
    assert vault.writes == []


class _CancelOn(_Fake):
    """Cancels the call when a command starts with *prefix*."""

    def __init__(self, prefix: str, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._prefix = prefix

    async def _run_command(
        self,
        target: Any,
        cmd: str,
        *,
        operator: Any = None,
        timeout: float = 30.0,
        stdin: str | None = None,
    ) -> Any:
        if cmd.startswith(self._prefix):
            self.commands.append(cmd)
            raise asyncio.CancelledError
        return await super()._run_command(target, cmd, operator=operator)


async def test_a_cancel_during_the_playback_says_the_user_may_exist(vault: _FakeVault) -> None:
    fake = _CancelOn("pfSsh.php playback meho_openvpn_user_create", [_config()])
    with pytest.raises(asyncio.CancelledError, match="may exist on the firewall now"):
        await _create(fake)
    assert vault.writes == []


async def test_a_cancel_during_the_vault_write_undoes_the_user_and_cancels_again(
    monkeypatch: pytest.MonkeyPatch, vault: _FakeVault
) -> None:
    async def _cancelled(*args: Any, **kw: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(user_module, "_store_password", _cancelled)
    fake = _created_fake()
    with pytest.raises(asyncio.CancelledError, match="was removed again") as err:
        await _create(fake)
    # The write runs in a thread and may still finish after the undo.
    assert f"may be stored at secret/{_VAULT_PATH}" in str(err.value)
    assert "vault_field_exists" in str(err.value)
    assert len(fake.played) == 2  # the create, then the undo
    assert any("meho_openvpn_user_undo" in c for c in fake.staged)


async def test_an_undo_refused_for_a_system_name_says_so(vault: _FakeVault) -> None:
    class _UndoRefused(_Fake):
        async def _run_command(
            self,
            target: Any,
            cmd: str,
            *,
            operator: Any = None,
            timeout: float = 30.0,
            stdin: str | None = None,
        ) -> Any:
            if cmd.startswith("pfSsh.php playback meho_openvpn_user_undo"):
                return _proc("MEHO_VPN_SYSTEM_NAME\n")
            return await super()._run_command(target, cmd, operator=operator)

    fake = _UndoRefused([_config()], playback_out="MEHO_VPN_CREATED_UID=2000\n")
    with pytest.raises(RuntimeError, match=r"was not removed again \(system_name\)"):
        await _create(fake)


@_needs_php
@pytest.mark.parametrize("name", ["root", "nobody"])
def test_create_fragment_run_in_php_refuses_a_system_account(tmp_path: Path, name: str) -> None:
    before = _php_config([])
    printed, out = _run_php(tmp_path, before, user_module.build_user_create_playback(name, "d"))
    assert printed == "MEHO_VPN_SYSTEM_NAME\n"
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_create_fragment_run_in_php_refuses_when_the_files_cannot_be_read(
    tmp_path: Path,
) -> None:
    before = _php_config([])
    fragment = user_module.build_user_create_playback("guest-1", "d").replace(
        "'/etc/passwd'", f"'{tmp_path / 'missing'}'"
    )
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed == "MEHO_VPN_SYSTEM_NAMES_UNREADABLE\n"
    assert out["config"] == before and out["calls"] == []


@_needs_php
@pytest.mark.parametrize("name", ["root", "nobody"])
@pytest.mark.parametrize("undo", [False, True])
def test_delete_and_undo_never_call_local_user_del_for_a_system_account(
    tmp_path: Path, name: str, undo: bool
) -> None:
    """A local user named like a system account of another uid is never removed."""
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": name, "uid": "2003", "scope": "user"},
    ]
    before = _php_config([_csc_row(name, "1", "198.51.100.5/24")], users)
    fragment = (
        user_module.build_user_undo_playback(name, "2003")
        if undo
        else user_module.build_user_delete_playback(name)
    )
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed == "MEHO_VPN_SYSTEM_NAME\n"
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_delete_fragment_run_in_php_writes_nothing_while_a_rule_has_the_address_as_source(
    tmp_path: Path,
) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    before["filter"] = {
        "rule": [
            {"tracker": "1", "interface": "lan", "source": {"address": "198.51.100.5"}},
            {"tracker": "2", "interface": "openvpn", "source": {"address": "198.51.100.5/32"}},
        ]
    }
    fragment = user_module.build_user_delete_playback("guest-1", ["198.51.100.5"])
    printed, out = _run_php(tmp_path, before, fragment)
    # The rule on the LAN tab counts too: the source is the address.
    assert printed.strip() == "MEHO_VPN_RULES_REMAIN=2"
    assert out["config"] == before and out["calls"] == []


async def test_delete_is_refused_when_the_fragment_finds_a_system_name() -> None:
    fake = _Fake(
        [_vpn_config()],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
        playback_out="MEHO_VPN_SYSTEM_NAME\n",
    )
    with pytest.raises(PfSenseRefusedError, match=r"refused \(system_name\)"):
        await _delete(fake)


def _member_of(group: str, uid: str = "2000") -> str:
    return f"<group><name>{group}</name><member>{uid}</member></group>"


@pytest.mark.parametrize(
    ("users", "extra", "why"),
    [
        (_user("guest-1", "2000"), _member_of("admins"), "group"),
        (
            "<user><scope>user</scope><name>guest-1</name><uid>2000</uid>"
            "<priv>page-all</priv></user>",
            "",
            "permission",
        ),
    ],
)
async def test_delete_refuses_a_user_that_is_more_than_a_vpn_user(
    users: str, extra: str, why: str
) -> None:
    config = _config(users=users + extra)
    fake = _Fake([config], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=rf"refused \(protected\).*{why}"):
        await _delete(fake)
    assert not fake.wrote


async def test_delete_refuses_the_ssh_login_of_the_target() -> None:
    class _LoginGuest(_Fake):
        async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
            return {"username": "guest-1", "client_keys": [], "known_hosts": None}

    fake = _LoginGuest([_vpn_config()], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(protected\).*SSH login"):
        await _delete(fake)
    assert not fake.wrote


@pytest.mark.parametrize("force", [False, True])
async def test_delete_is_refused_while_an_untagged_rule_has_the_users_address(
    force: bool,
) -> None:
    rule = (
        "<rule><tracker>1700000077</tracker><type>pass</type><interface>openvpn</interface>"
        "<ipprotocol>inet</ipprotocol><source><address>203.0.113.5/32</address></source>"
        "<destination><any></any></destination><descr>by hand</descr></rule>"
    )
    users = _user("guest-1", "2000")
    csc = _csc("guest-1", "1", "198.51.100.5/24") + _csc("guest-1", "2", "203.0.113.5/24")
    fake = _Fake([_config(users=users, csc=csc, rules=rule)], sockets={"1": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(rules_remain\).*1700000077"):
        await _delete(fake, force=force)
    assert not fake.wrote


async def test_a_deleted_user_keeps_its_password_in_vault_and_says_so() -> None:
    fake = _Fake(
        [_vpn_config(), _vpn_config(with_user=False)],
        sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY},
    )
    out = await _delete(fake)
    assert "password in Vault stays" in out["guidance"]
    staged = fake.staged[0]
    assert "'198.51.100.5'" in staged and "'203.0.113.5/32'" in staged


async def test_the_delete_preview_shows_groups_and_permissions() -> None:
    from meho_backplane.operations._preview import PreviewContext

    users = (
        "<user><scope>user</scope><name>guest-1</name><uid>2000</uid><priv>page-all</priv>"
        "</user>" + _member_of("admins") + _member_of("all")
    )
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=_Fake([_config(users=users)]),
        operator=MagicMock(),
        target=None,
        params={"name": "guest-1"},
    )
    preview = await user_module._delete_preview(ctx)
    assert preview is not None
    assert preview["groups"] == ["admins"] and preview["permissions"] == ["page-all"]
    assert preview["refusal"] == "protected"
    assert "password in Vault stays" in preview["note"]


@pytest.mark.parametrize(
    ("data", "is_target", "state"),
    [
        (None, False, "new"),
        ({"username": "guest-1"}, False, "holds_other_fields"),
        ({"password": "older"}, False, "field_exists"),
        (None, True, "target_secret"),
    ],
)
async def test_the_create_preview_says_whether_the_vault_path_is_new(
    monkeypatch: pytest.MonkeyPatch,
    target_paths: set[str],
    data: dict[str, Any] | None,
    is_target: bool,
    state: str,
) -> None:
    from meho_backplane.operations._preview import PreviewContext

    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(data=data))
    if is_target:
        target_paths.add(_VAULT_PATH)
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=_Fake([_config()]),
        operator=_operator(),
        target=None,
        params=_CREATE,
    )
    preview = await user_module._create_preview(ctx)
    assert preview is not None
    assert preview["vault"]["path_state"] == state
    assert "older" not in json.dumps(preview)


async def test_the_create_preview_reads_nothing_for_a_path_outside_the_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.operations._preview import PreviewContext

    async def _no_db() -> set[str]:
        raise AssertionError("the target paths must not be read")

    fake_vault = _FakeVault(data={"password": "older"})
    monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
    monkeypatch.setattr(user_module, "_target_secret_paths", _no_db)
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"name": "guest-1", "vault_path": "tenants/someone-else/vpn"},
    )
    preview = await user_module._create_preview(ctx)
    assert preview is not None
    assert preview["vault"]["path_state"] == "outside_tenant"
    assert fake_vault.opened == 0


# ===========================================================================
# Metadata
# ===========================================================================


def test_ops_are_registered_with_the_right_risk_level() -> None:
    by_id = {op.op_id: op for op in pfsense.PFSENSE_OPS}
    expected = {
        "pfsense.openvpn.address.next_free": ("safe", False),
        "pfsense.openvpn.csc.sync": ("caution", True),
        "pfsense.openvpn.user.create": ("dangerous", True),
        "pfsense.openvpn.user.delete": ("dangerous", True),
    }
    for op_id, (level, approval) in expected.items():
        assert (by_id[op_id].safety_level, by_id[op_id].requires_approval) == (level, approval)
        assert by_id[op_id].group_key == "vpn"
        Draft202012Validator.check_schema(by_id[op_id].parameter_schema)
        Draft202012Validator.check_schema(by_id[op_id].response_schema or {})
    assert {op.op_id for op in (*OPENVPN_CSC_OPS, *user_module.OPENVPN_USER_OPS)} == set(expected)


def test_every_op_has_a_connector_handler() -> None:
    for op in (*OPENVPN_CSC_OPS, *user_module.OPENVPN_USER_OPS):
        assert callable(getattr(pfsense.PfSenseConnector, op.handler_attr))


def test_broadcast_classes_and_root_shell_marks() -> None:
    assert classify_op("pfsense.openvpn.user.create") == "credential_mint"
    assert classify_op("pfsense.openvpn.csc.sync") == "write"
    assert classify_op("pfsense.openvpn.user.delete") == "write"
    for op_id in (
        "pfsense.openvpn.csc.sync",
        "pfsense.openvpn.user.create",
        "pfsense.openvpn.user.delete",
    ):
        assert is_root_shell(op_id)
    assert not is_root_shell("pfsense.openvpn.address.next_free")


def test_each_vpn_user_create_and_delete_needs_its_own_approval() -> None:
    """A standing grant never covers these two ops: a person approves each call."""
    assert needs_per_call_approval("pfsense.openvpn.user.create")
    assert needs_per_call_approval("pfsense.openvpn.user.delete")
    assert not needs_per_call_approval("pfsense.openvpn.csc.sync")


def test_the_create_response_schema_has_no_password_field() -> None:
    schema = next(
        op for op in user_module.OPENVPN_USER_OPS if op.op_id.endswith("user.create")
    ).response_schema
    assert schema is not None
    assert "password" not in json.dumps(schema["properties"]).replace('"field"', "")


async def test_the_override_sync_preview_shows_the_whole_plan() -> None:
    from meho_backplane.connectors.pfsense.ops_openvpn_csc import _csc_preview
    from meho_backplane.operations._preview import PreviewContext

    entries = [_entry(f"u{i}", tunnel_address=f"198.51.100.{i + 2}") for i in range(60)]
    fake = _Fake([_config(users=_users(*(e["name"] for e in entries)))])
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=fake,
        operator=_operator(),
        target=None,
        params={"entries": entries},
    )
    preview = await _csc_preview(ctx)
    assert preview is not None
    assert preview["counts"]["create"] == 60
    assert len(preview["entries"]) == 60  # no cut at 50
    assert not fake.wrote


async def test_the_override_sync_preview_declines_bad_input() -> None:
    from meho_backplane.connectors.pfsense.ops_openvpn_csc import _csc_preview
    from meho_backplane.operations._preview import PreviewContext

    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"entries": "x"},
    )
    assert await _csc_preview(ctx) is None


# ===========================================================================
# Gaps left after the first release (one name rule, KV version, rules,
# override sync, a second cancel)
# ===========================================================================


@pytest.mark.parametrize("name", [".", "..", ".x", ".guest-1"])
async def test_user_disable_and_enable_refuse_a_name_with_a_leading_dot(name: str) -> None:
    from meho_backplane.connectors.pfsense.ops_user_write import USER_WRITE_OPS

    for op in USER_WRITE_OPS:
        assert re.fullmatch(op.parameter_schema["properties"]["name"]["pattern"], name) is None
    fake = _Fake([_config(users=_user(name, "2000"))])
    with pytest.raises(ValueError, match="no leading dot"):
        await fake.user_disable(None, {"name": name}, _operator())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no leading dot"):
        await fake.user_enable(None, {"name": name}, _operator())  # type: ignore[arg-type]
    assert fake.commands == []


def test_the_user_name_rule_is_the_same_for_every_pfsense_user_operation() -> None:
    from meho_backplane.connectors.pfsense import ops_openvpn_rule
    from meho_backplane.connectors.pfsense.ops_user_write import USER_NAME_RE

    assert user_module._VPN_USER_NAME_RE is USER_NAME_RE
    assert ops_openvpn_rule.USER_NAME_RE is USER_NAME_RE
    assert USER_NAME_RE.fullmatch(".x") is None
    assert USER_NAME_RE.fullmatch("a.b_c-9") is not None


@_needs_php
@pytest.mark.parametrize("name", _DOT_NAMES)
@pytest.mark.parametrize("disable", [True, False])
def test_the_user_flag_fragment_run_in_php_never_reaches_local_user_set_for_a_dot_name(
    tmp_path: Path, name: str, disable: bool
) -> None:
    from meho_backplane.connectors.pfsense.ops_user_write import build_user_flag_playback

    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": name, "uid": "2000", "scope": "user"},
    ]
    before = _php_config([], users)
    fragment = _with_name(build_user_flag_playback("guest-1", disable=disable), name)
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed == ""
    assert out["config"] == before and out["calls"] == []


async def test_create_refuses_a_new_path_on_a_mount_that_is_not_kv_version_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for version in ("1", None, "", "3"):
        fake_vault = _FakeVault(mount_version=version)  # a new path: the read says "not found"
        monkeypatch.setattr(user_module, "vault_client_for_operator", fake_vault)
        fake = _created_fake()
        with pytest.raises(PfSenseRefusedError, match=r"refused \(vault_version_unknown\)"):
            await _create(fake)
        assert not fake.wrote and fake_vault.writes == []


async def test_create_refuses_a_mount_whose_version_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_vault = _FakeVault()
    original = fake_vault.__call__

    def _broken(operator: Any, **kw: Any) -> Any:
        ctx = original(operator, **kw)

        @asynccontextmanager
        async def _wrapped() -> AsyncIterator[Any]:
            async with ctx as client:
                client.adapter.get = MagicMock(side_effect=hvac.exceptions.Forbidden("denied"))
                yield client

        return _wrapped()

    monkeypatch.setattr(user_module, "vault_client_for_operator", _broken)
    fake = _created_fake()
    with pytest.raises(RuntimeError, match="reading the Vault path failed"):
        await _create(fake)
    assert not fake.wrote


async def test_the_create_preview_says_unknown_for_a_mount_that_is_not_kv_version_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.operations._preview import PreviewContext

    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(mount_version="1"))
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=_Fake([_config()]),
        operator=_operator(),
        target=None,
        params=_CREATE,
    )
    preview = await user_module._create_preview(ctx)
    assert preview is not None and preview["vault"]["path_state"] == "unknown"


def _alias(name: str, kind: str, address: str) -> str:
    return f"<alias><name>{name}</name><type>{kind}</type><address>{address}</address></alias>"


def _aliases(name: str, kind: str, address: str) -> str:
    return f"<aliases>{_alias(name, kind, address)}</aliases>"


def _guest_rule(tracker: str, source: str, interface: str = "lan") -> str:
    """A pass rule written by hand (no tag) with the given ``<source>`` content."""
    return (
        f"<rule><tracker>{tracker}</tracker><type>pass</type><interface>{interface}</interface>"
        f"<ipprotocol>inet</ipprotocol><source>{source}</source>"
        "<destination><any></any></destination><descr>by hand</descr></rule>"
    )


def _guest_config(rules: str, extra: str = "", *, with_user: bool = True) -> str:
    users = _user("guest-1", "2000") if with_user else ""
    csc = _csc("guest-1", "1", "198.51.100.5/24") if with_user else ""
    xml = _config(users=users, csc=csc, rules=rules)
    return xml.replace("</pfsense>", f"{extra}</pfsense>")


_HOST_ALIAS = _aliases("vpn_guests", "host", "192.0.2.9 198.51.100.5")
_NESTED_ALIAS = (
    "<aliases>"
    + _alias("outer", "network", "192.0.2.0/24 inner")
    + _alias("inner", "network", "198.51.100.0/25")
    + "</aliases>"
)
_OPT_NET = (
    "<interfaces><opt2><if>ovpns1</if><ipaddr>198.51.100.1</ipaddr><subnet>24</subnet></opt2>"
    "</interfaces>"
)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    ("source", "interface", "extra"),
    [
        ("<address>198.51.100.5</address>", "lan", ""),
        ("<address>198.51.100.0/24</address>", "wan", ""),
        ("<address>vpn_guests</address>", "lan", _HOST_ALIAS),
        ("<address>outer</address>", "opt1", _NESTED_ALIAS),
        ("<address>vpn_guests</address>", "VPNGroup", _HOST_ALIAS),
        ("<address>198.51.100.5</address>", "opt2", _OPT_NET),
        ("<address>198.51.100.5</address>", "lan,wan", ""),
        ("<network>opt2</network>", "lan", _OPT_NET),
        ("<network>opt2ip</network>", "lan", _OPT_NET.replace("198.51.100.1", "198.51.100.5")),
        ("<address>urls</address>", "lan", _aliases("urls", "urltable", "")),
    ],
)
async def test_delete_is_refused_while_any_rule_source_holds_the_users_address(
    source: str, interface: str, extra: str, force: bool
) -> None:
    config = _guest_config(_guest_rule("1700000088", source, interface), extra)
    fake = _Fake([config], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    with pytest.raises(PfSenseRefusedError, match=r"refused \(rules_remain\).*1700000088"):
        await _delete(fake, force=force)
    assert not fake.wrote


@pytest.mark.parametrize(
    ("source", "extra"),
    [
        ("<address>198.51.100.6</address>", ""),
        ("<address>203.0.114.0/24</address>", ""),
        (
            "<address>vpn_guests</address>",
            _aliases("vpn_guests", "host", "192.0.2.9"),
        ),
        (
            "<address>names</address>",
            _aliases("names", "host", "host.example.test"),
        ),
        ("<address>loop</address>", _aliases("loop", "network", "loop")),
        ("<any></any>", ""),
        ("<not></not><address>198.51.100.5</address>", ""),
        ("<network>opt2</network>", _OPT_NET.replace("198.51.100.1", "203.0.114.1")),
        ("<network>opt9</network>", _OPT_NET),
        ("<network>(self)</network>", ""),
    ],
)
async def test_delete_goes_on_when_no_rule_source_holds_the_users_address(
    source: str, extra: str
) -> None:
    before = _guest_config(_guest_rule("1700000089", source), extra)
    after = _guest_config(_guest_rule("1700000089", source), extra, with_user=False)
    fake = _Fake([before, after], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    out = await _delete(fake)
    assert out["status"] == "deleted" and out["applied"]


async def test_the_delete_preview_names_a_rule_that_holds_the_address_through_an_alias() -> None:
    from meho_backplane.operations._preview import PreviewContext

    config = _guest_config(_guest_rule("1700000090", "<address>vpn_guests</address>"), _HOST_ALIAS)
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=_Fake([config]),
        operator=_operator(),
        target=None,
        params={"name": "guest-1"},
    )
    preview = await user_module._delete_preview(ctx)
    assert preview is not None and preview["refusal"] == "rules_remain"


async def test_a_url_alias_rule_does_not_block_the_delete_of_a_user_with_no_address() -> None:
    rule = _guest_rule("1700000091", "<address>urls</address>")
    extra = _aliases("urls", "urltable", "")
    before = _config(users=_user("guest-1", "2000"), rules=rule).replace(
        "</pfsense>", f"{extra}</pfsense>"
    )
    after = _config(rules=rule).replace("</pfsense>", f"{extra}</pfsense>")
    fake = _Fake([before, after], sockets={"1": _SOCK_EMPTY, "2": _SOCK_EMPTY})
    out = await _delete(fake)
    assert out["status"] == "deleted" and out["applied"]


def _php_rule_config(
    source: dict[str, Any], interface: str, extra: dict[str, Any]
) -> dict[str, Any]:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([_csc_row("guest-1", "1", "198.51.100.5/24")], users)
    before["filter"] = {"rule": [{"tracker": "7", "interface": interface, "source": source}]}
    before.update(extra)
    return before


_PHP_ALIASES = {
    "aliases": {
        "alias": [
            {"name": "vpn_guests", "type": "host", "address": "192.0.2.9 198.51.100.5"},
            {"name": "outer", "type": "network", "address": "192.0.2.0/24 inner"},
            {"name": "inner", "type": "network", "address": "198.51.100.0/25"},
            {"name": "loop", "type": "network", "address": "loop"},
            {"name": "urls", "type": "urltable", "address": ""},
        ]
    }
}
_PHP_OPT = {"interfaces": {"opt2": {"ipaddr": "198.51.100.1", "subnet": "24"}}}


@_needs_php
@pytest.mark.parametrize(
    ("source", "interface", "extra"),
    [
        ({"address": "198.51.100.5"}, "lan", {}),
        ({"address": "198.51.100.0/24"}, "wan", {}),
        ({"address": "vpn_guests"}, "lan", _PHP_ALIASES),
        ({"address": "outer"}, "opt1", _PHP_ALIASES),
        ({"address": "urls"}, "lan", _PHP_ALIASES),
        ({"address": "198.51.100.5"}, "VPNGroup", {}),
        ({"network": "opt2"}, "lan", _PHP_OPT),
    ],
)
def test_delete_fragment_run_in_php_writes_nothing_while_a_source_holds_the_address(
    tmp_path: Path, source: dict[str, Any], interface: str, extra: dict[str, Any]
) -> None:
    before = _php_rule_config(source, interface, extra)
    fragment = user_module.build_user_delete_playback("guest-1", ["198.51.100.5"])
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed.strip() == "MEHO_VPN_RULES_REMAIN=1"
    assert out["config"] == before and out["calls"] == []


@_needs_php
@pytest.mark.parametrize(
    ("source", "extra"),
    [
        ({"address": "198.51.100.6"}, {}),
        ({"address": "203.0.114.0/24"}, {}),
        ({"address": "loop"}, _PHP_ALIASES),
        ({"address": "198.51.100.5", "not": ""}, {}),
        ({"any": ""}, {}),
        ({"network": "opt2"}, {"interfaces": {"opt2": {"ipaddr": "203.0.114.1", "subnet": "24"}}}),
        ({"network": "opt9"}, _PHP_OPT),
    ],
)
def test_delete_fragment_run_in_php_goes_on_when_no_source_holds_the_address(
    tmp_path: Path, source: dict[str, Any], extra: dict[str, Any]
) -> None:
    before = _php_rule_config(source, "lan", extra)
    fragment = user_module.build_user_delete_playback("guest-1", ["198.51.100.5"])
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed == ""
    assert ["write_config", "meho: delete VPN user guest-1"] in out["calls"]


@_needs_php
def test_delete_fragment_run_in_php_ignores_a_url_alias_for_a_user_with_no_address(
    tmp_path: Path,
) -> None:
    users = [
        {"name": "admin", "uid": "0", "scope": "system"},
        {"name": "guest-1", "uid": "2000", "scope": "user"},
    ]
    before = _php_config([], users)
    before["filter"] = {"rule": [{"tracker": "7", "source": {"address": "urls"}}]}
    before.update(_PHP_ALIASES)
    printed, out = _run_php(tmp_path, before, user_module.build_user_delete_playback("guest-1"))
    assert printed == ""
    assert ["write_config", "meho: delete VPN user guest-1"] in out["calls"]
    # The rule tag still counts for a user with no address.
    before["filter"]["rule"].append({"tracker": "8", "descr": "meho-vpn-user:guest-1"})
    printed, out = _run_php(tmp_path, before, user_module.build_user_delete_playback("guest-1"))
    assert printed.strip() == "MEHO_VPN_RULES_REMAIN=1"
    assert out["config"] == before and out["calls"] == []


@_needs_php
def test_csc_fragment_run_in_php_writes_nothing_when_the_user_is_gone(tmp_path: Path) -> None:
    plan = _plan_for(
        _config(csc=_csc("old", "1", "198.51.100.5/24", "192.0.2.0/25"), users=_users("old")),
        [_entry("old", routes=["192.0.2.0/24"])],
    )
    # The user was deleted after the plan was made.
    now = _php_config([_csc_row("old", "1", "198.51.100.5/24", "192.0.2.0/25")])
    printed, out = _run_php(tmp_path, now, build_csc_sync_playback(plan, 1))
    assert printed == "MEHO_CSC_NO_USER=old\n"
    assert out["config"] == now and out["calls"] == []


async def test_sync_writes_nothing_when_the_user_is_gone_after_the_plan() -> None:
    base = _csc("old", "1", "198.51.100.5/24", "192.0.2.0/25")
    fake = _Fake([_config(csc=base, users=_users("old"))], playback_out="MEHO_CSC_NO_USER=old\n")
    with pytest.raises(PfSenseRefusedError, match=r"refused \(user_not_found\).*'old'"):
        await _sync(fake, [_entry("old", routes=["192.0.2.0/24"])])
    # No read-back: the fragment wrote nothing, so nothing is reported as synced.
    assert fake.commands.count("cat /cf/conf/config.xml") == 1


async def test_a_second_cancel_during_the_undo_still_says_the_user_may_exist(
    monkeypatch: pytest.MonkeyPatch, vault: _FakeVault
) -> None:
    async def _cancelled(*args: Any, **kw: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(user_module, "_store_password", _cancelled)
    fake = _CancelOn(
        "pfSsh.php playback meho_openvpn_user_undo",
        [_config(), _config(users=_user("guest-1", "2000"))],
        playback_out=_CREATED_OUT,
    )
    with pytest.raises(asyncio.CancelledError) as err:
        await _create(fake)
    text = str(err.value)
    assert "may exist on the firewall now" in text and "password is not stored" in text


async def test_a_second_cancel_during_the_undo_after_a_vault_failure_says_it_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module, "vault_client_for_operator", _FakeVault(fail=True))
    fake = _CancelOn(
        "pfSsh.php playback meho_openvpn_user_undo",
        [_config(), _config(users=_user("guest-1", "2000"))],
        playback_out=_CREATED_OUT,
    )
    with pytest.raises(asyncio.CancelledError) as err:
        await _create(fake)
    text = str(err.value)
    assert "may exist on the firewall now" in text and "password is not stored" in text
