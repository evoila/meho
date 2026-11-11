"""Every pfSense refusal is a failed call, through the dispatcher (#5138).

Each case runs one refused change through :func:`dispatch` against a
recording connector that replays a made-up ``config.xml``. The checks:

* the call gets ``status: error`` and the message names the refusal;
* no ``pfSsh.php playback`` ran, so nothing changed;
* the audit record says the call failed and keeps the refusal.

Every case runs on the path an approved request takes (``_approved=True``):
a person must approve each of these writes before it runs.

All config.xml fixtures are synthetic (RFC 5737 / RFC 1918 addresses).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000005138")


def _user(name: str, uid: str, *, scope: str = "user") -> str:
    return f"<user><scope>{scope}</scope><name>{name}</name><uid>{uid}</uid></user>"


def _users(*users: str) -> str:
    return f"<pfsense><system>{''.join(users)}</system></pfsense>"


_ALIAS = "<alias><name>{name}</name><type>host</type><address>{address}</address></alias>"
_GATEWAY = (
    "<gateway_item><name>{name}</name><interface>{interface}</interface>"
    "<gateway>{address}</gateway></gateway_item>"
)

_CONFIG_DUP_TRACKER = (
    "<pfsense><nat>"
    "<rule><tracker>1585165239</tracker><target>192.0.2.10</target></rule>"
    "<rule><tracker>1585165239</tracker><target>192.0.2.11</target></rule>"
    "</nat></pfsense>"
)
_CONFIG_DUP_ALIAS = (
    "<pfsense><aliases>"
    + _ALIAS.format(name="WEB_SERVERS", address="192.0.2.10")
    + _ALIAS.format(name="WEB_SERVERS", address="192.0.2.11")
    + "</aliases></pfsense>"
)
_CONFIG_REFERENCED_ALIAS = (
    "<pfsense><aliases>"
    + _ALIAS.format(name="WEB_SERVERS", address="192.0.2.10")
    + "</aliases><filter>"
    "<rule><tracker>1600000001</tracker><descr>allow web</descr>"
    "<destination><address>WEB_SERVERS</address></destination></rule>"
    "</filter></pfsense>"
)
_CONFIG_DUP_ROUTE = (
    "<pfsense><staticroutes>"
    "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "<route><network>10.10.0.5/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes></pfsense>"
)
_CONFIG_DUP_GW = (
    "<pfsense><gateways>"
    + _GATEWAY.format(name="DUP_GW", interface="opt1", address="192.0.2.1")
    + _GATEWAY.format(name="DUP_GW", interface="opt2", address="192.0.2.2")
    + "</gateways></pfsense>"
)
_CONFIG_REFERENCED_GW = (
    "<pfsense><staticroutes>"
    "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes><gateways>"
    + _GATEWAY.format(name="LAB_GW", interface="opt1", address="192.0.2.1")
    + "</gateways></pfsense>"
)
_CONFIG_MEMBERS = (
    "<pfsense><aliases>"
    + _ALIAS.format(name="SOLO", address="203.0.113.9")
    + _ALIAS.format(name="TWICE", address="198.51.100.5 198.51.100.5")
    + "</aliases></pfsense>"
)
_CONFIG_USERS = _users(
    _user("admin", "0", scope="system"),
    _user("vpn.guest-1", "2000"),
    _user("vpn.guest-1", "2000"),
)
_CONFIG_ADDS = (
    "<pfsense><gateways>"
    + _GATEWAY.format(name="LAB_GW", interface="lan", address="10.0.0.1")
    + _GATEWAY.format(name="OTHER_GW", interface="lan", address="10.0.0.2")
    + "</gateways><staticroutes>"
    "<route><network>10.9.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes></pfsense>"
)

_VPN_SERVER = (
    "<openvpn-server><vpnid>{vpnid}</vpnid><mode>server_user</mode>"
    "<tunnel_network>198.51.100.0/24</tunnel_network></openvpn-server>"
)
_VPN_USER = _user("vpn.guest-2", "2001")
_VPN_TAGGED_RULE = (
    "<rule><tracker>123456789</tracker><type>pass</type><interface>openvpn</interface>"
    "<ipprotocol>inet</ipprotocol><source><address>198.51.100.5</address></source>"
    "<destination><address>192.0.2.0/24</address></destination>"
    "<descr>meho-vpn-user:vpn.guest-2</descr></rule>"
)


def _vpn_config(*, vpnid: str = "1", rules: str = "") -> str:
    """A config with one user-login OpenVPN server and the user ``vpn.guest-2``."""
    return (
        f"<pfsense><system>{_VPN_USER}</system><filter>{rules}</filter>"
        f"<openvpn>{_VPN_SERVER.format(vpnid=vpnid)}</openvpn></pfsense>"
    )


#: (op_id, config.xml, params, refusal code).
_REFUSALS: list[tuple[str, str, dict[str, Any], str]] = [
    ("pfsense.nat.delete", _CONFIG_DUP_TRACKER, {"tracker": "1585165239"}, "ambiguous"),
    ("pfsense.alias.delete", _CONFIG_DUP_ALIAS, {"name": "WEB_SERVERS"}, "ambiguous"),
    ("pfsense.alias.delete", _CONFIG_REFERENCED_ALIAS, {"name": "WEB_SERVERS"}, "referenced"),
    ("pfsense.route.static.delete", _CONFIG_DUP_ROUTE, {"network": "10.10.0.0/24"}, "ambiguous"),
    ("pfsense.gateway.delete", _CONFIG_DUP_GW, {"name": "DUP_GW"}, "ambiguous"),
    ("pfsense.gateway.delete", _CONFIG_REFERENCED_GW, {"name": "LAB_GW"}, "referenced"),
    (
        "pfsense.alias.member.remove",
        _CONFIG_DUP_ALIAS,
        {"name": "WEB_SERVERS", "value": "192.0.2.10"},
        "ambiguous",
    ),
    (
        "pfsense.alias.member.remove",
        _CONFIG_MEMBERS,
        {"name": "TWICE", "value": "198.51.100.5"},
        "ambiguous",
    ),
    (
        "pfsense.alias.member.remove",
        _CONFIG_MEMBERS,
        {"name": "SOLO", "value": "203.0.113.9"},
        "last_member",
    ),
    ("pfsense.user.disable", _CONFIG_USERS, {"name": "nobody"}, "not_found"),
    ("pfsense.user.disable", _CONFIG_USERS, {"name": "vpn.guest-1"}, "ambiguous"),
    ("pfsense.user.disable", _CONFIG_USERS, {"name": "admin"}, "protected"),
    ("pfsense.user.enable", _CONFIG_USERS, {"name": "nobody"}, "not_found"),
    ("pfsense.user.enable", _CONFIG_USERS, {"name": "vpn.guest-1"}, "ambiguous"),
    ("pfsense.user.enable", _CONFIG_USERS, {"name": "admin"}, "protected"),
    (
        "pfsense.gateway.add",
        _CONFIG_ADDS,
        {"name": "LAB_GW", "interface": "opt1", "gateway": "10.0.0.1"},
        "conflict",
    ),
    (
        "pfsense.route.static.add",
        _CONFIG_ADDS,
        {"network": "10.9.0.0/24", "gateway": "OTHER_GW"},
        "conflict",
    ),
    (
        "pfsense.openvpn.csc.sync",
        _vpn_config(),
        {"entries": [{"name": "vpn.guest-2", "server_vpnid": "7"}]},
        "refused",
    ),
    (
        "pfsense.openvpn.rule.sync",
        _vpn_config(),
        {"rules": [{"name": "nobody", "source": "198.51.100.5", "destination": "192.0.2.0/24"}]},
        "refused",
    ),
    (
        "pfsense.openvpn.user.create",
        _CONFIG_USERS,
        {"name": "vpn.guest-1", "vault_path": f"tenants/{_TENANT_ID}/vpn/guest-1"},
        "ambiguous",
    ),
    ("pfsense.openvpn.user.delete", _CONFIG_USERS, {"name": "vpn.guest-1"}, "ambiguous"),
    ("pfsense.openvpn.user.delete", _CONFIG_USERS, {"name": "admin"}, "protected"),
    ("pfsense.openvpn.user.delete", _vpn_config(), {"name": "vpn.guest-2"}, "connected"),
    (
        "pfsense.openvpn.user.delete",
        _vpn_config(vpnid="x"),
        {"name": "vpn.guest-2"},
        "connection_unknown",
    ),
    (
        "pfsense.openvpn.user.delete",
        _vpn_config(rules=_VPN_TAGGED_RULE),
        {"name": "vpn.guest-2", "force": True},
        "rules_remain",
    ),
]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


_VPN_SESSION = (
    "TITLE,x\nHEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Username\n"
    "CLIENT_LIST,vpn.guest-2,203.0.113.77:1194,198.51.100.5,vpn.guest-2\nEND\n"
)


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


class _RecordingPfSense(PfSenseConnector):
    """A PfSenseConnector that replays one config.xml and records each command.

    An OpenVPN server's management socket answers that ``vpn.guest-2`` is
    connected.
    """

    def __init__(self, config: str) -> None:
        super().__init__()
        self._config = config
        self.commands: list[str] = []

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
            return _proc(self._config)
        if cmd.endswith("/sock"):
            return _proc(_VPN_SESSION)
        return _proc("", 0)

    @property
    def playback_ran(self) -> bool:
        return any(c.startswith("pfSsh.php playback") for c in self.commands)


class _FakeFingerprint:
    version = "2.7"


class _FakePfsenseTarget:
    def __init__(self) -> None:
        self.product = "pfsense"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = "pfsense-ssh"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "pf-perimeter"
        self.host = "pf.test"
        self.port = 22
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-pf",
        name="pfSense Refusal Conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _bootstrap(recorder: _RecordingPfSense) -> None:
    if ("pfsense", "2.7", "pfsense-ssh") not in all_connectors_v2():
        register_connector_v2(
            product="pfsense", version="2.7", impl_id="pfsense-ssh", cls=PfSenseConnector
        )
    await PfSenseConnector.register_operations()
    _CONNECTOR_INSTANCE_CACHE[PfSenseConnector] = recorder  # type: ignore[assignment]


async def _run(op_id: str, config: str, params: dict[str, Any]) -> tuple[Any, _RecordingPfSense]:
    recorder = _RecordingPfSense(config)
    await _bootstrap(recorder)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_FakePfsenseTarget(),
        params=params,
        _approved=True,
    )
    return result, recorder


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "config", "params", "code"), _REFUSALS)
async def test_refusal_is_a_failed_call_with_a_failed_audit_record(
    op_id: str, config: str, params: dict[str, Any], code: str
) -> None:
    result, recorder = await _run(op_id, config, params)

    assert result.status == "error", result
    assert result.extras["exception_class"] == "PfSenseRefusedError"
    assert f"{op_id} refused ({code})" in result.extras["exception_message"]
    assert result.extras["remediation"]
    assert not recorder.playback_ran

    rows = await _audit_rows(op_id)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["result_status"] == "error"
    assert f"refused ({code})" in payload["error"]["exception_message"]
    assert payload["error"]["remediation"] == result.extras["remediation"]


def _many_rules_config(op_id: str, count: int) -> tuple[str, str]:
    """A config where *count* objects use one alias or gateway, and its name."""
    if op_id == "pfsense.alias.delete":
        rules = "".join(
            f"<rule><tracker>{1600000000 + i}</tracker><descr>allow web {i}</descr>"
            "<destination><address>WEB_SERVERS</address></destination></rule>"
            for i in range(count)
        )
        config = (
            "<pfsense><aliases>"
            + _ALIAS.format(name="WEB_SERVERS", address="192.0.2.10")
            + f"</aliases><filter>{rules}</filter></pfsense>"
        )
        return config, "WEB_SERVERS"
    routes = "".join(
        f"<route><network>10.{i}.0.0/24</network><gateway>LAB_GW</gateway>"
        f"<descr>lab net {i}</descr></route>"
        for i in range(count)
    )
    config = (
        f"<pfsense><staticroutes>{routes}</staticroutes><gateways>"
        + _GATEWAY.format(name="LAB_GW", interface="opt1", address="192.0.2.1")
        + "</gateways></pfsense>"
    )
    return config, "LAB_GW"


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["pfsense.alias.delete", "pfsense.gateway.delete"])
async def test_referenced_refusal_names_every_referrer_past_the_message_cap(op_id: str) -> None:
    """The error message is cut to 256 characters; the full list is in ``remediation``."""
    config, name = _many_rules_config(op_id, 8)
    result, recorder = await _run(op_id, config, {"name": name})

    assert result.status == "error", result
    message = result.extras["exception_message"]
    assert "refused (referenced)" in message
    assert "referenced by 8 object(s)" in message
    assert message.endswith("...<truncated>")  # the list does not fit in the message
    remediation = result.extras["remediation"]
    for i in range(8):
        assert f" {i})" in remediation, i  # every descr ends in its number
    assert "remove or repoint each one first" in remediation
    assert "more (preview_operation" not in remediation
    assert not recorder.playback_ran

    rows = await _audit_rows(op_id)
    assert rows[0].payload["error"]["remediation"] == remediation


@pytest.mark.asyncio
async def test_referenced_refusal_too_long_to_list_says_how_many_more() -> None:
    """A list longer than the remediation budget names what fits and counts the rest."""
    config, name = _many_rules_config("pfsense.alias.delete", 200)
    result, recorder = await _run("pfsense.alias.delete", config, {"name": name})

    assert result.status == "error", result
    remediation = result.extras["remediation"]
    assert "referenced by 200 object(s)" in remediation
    assert "more: run pfsense.config.show and search its config_xml for 'WEB_SERVERS'" in (
        remediation
    )
    assert not remediation.endswith("...<truncated>")
    assert not recorder.playback_ran

    # Follow the message: the read op it names returns every referrer,
    # also the ones the message leaves out.
    trackers = [str(1600000000 + i) for i in range(200)]
    omitted = [t for t in trackers if f"filter_rule({t}:" not in remediation]
    assert omitted, "the message names every referrer, so nothing is left to look up"
    read, _ = await _run("pfsense.config.show", config, {})
    assert read.status == "ok", read
    assert read.handle is None
    assert isinstance(read.result, dict)
    config_xml = read.result["config_xml"]
    for tracker in omitted:
        assert f"<tracker>{tracker}</tracker>" in config_xml, tracker
