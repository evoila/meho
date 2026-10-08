# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the safe pfSense reads ``pfsense.user.list`` and
``pfsense.route.static.list`` (#3954).

All data is synthetic: fake hashes and keys with the right shape and junk
inside (reused from ``test_connectors_pfsense_config_redaction.py``), and
made-up user, group and gateway names.

Coverage:

* no secrets: a config that holds every kind of secret field, in the user,
  group, route and system elements, returns none of the secret values, and
  each row has exactly the allow-listed keys;
* the field mapping for users and routes;
* the ``disabled`` and ``expires`` handling;
* the group membership (from the ``<group><member>`` uid lists);
* an empty config (no users, no routes);
* a parse failure (malformed XML, a DTD / entity, a wrong root, an empty
  file) raises an error that holds no file content; a failed read raises;
* a listed route's ``network`` is what ``pfsense.route.static.delete``
  needs to find that route;
* the response schemas, the registry entries and the op metadata.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.pfsense.ops_config_reads import (
    ROUTE_ROW_FIELDS,
    USER_ROW_FIELDS,
    PfSenseConfigParseError,
    list_local_users,
    list_static_routes,
)
from meho_backplane.connectors.pfsense.ops_delete import _match_static_routes_canonical
from meho_backplane.connectors.pfsense.ops_write import _validate_network_cidr
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.settings import get_settings
from tests.test_connectors_pfsense_config_redaction import (
    _FAKE_CERT_B64,
    _FAKE_SSH_PUBKEY_B64,
    _SECRET_CASES,
    ALL_FAKE_SECRETS,
    FAKE_BCRYPT,
    FAKE_PEM_KEY,
    FAKE_PSK,
    FAKE_SSH_HOST_KEY_B64,
)

# ---------------------------------------------------------------------------
# Environment + stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="fw-01",
    host="fw-01.test.invalid",
    port=22,
    secret_ref="meho/testing/pfsense/fw-01",
)


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's SSHCompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _op(op_id: str) -> Any:
    return next(op for op in PFSENSE_OPS if op.op_id == op_id)


# ---------------------------------------------------------------------------
# Fixtures: a normal config, and one full of secrets
# ---------------------------------------------------------------------------

#: A normal config written the way pfSense writes it: two groups with uid
#: member lists, three users (built-in admin, an enabled user with an expiry
#: date, a disabled user with an empty expiry), and three static routes (one
#: disabled, one stored with host bits set).
_CONFIG = """<?xml version="1.0"?>
<pfsense>
\t<version>23.3</version>
\t<system>
\t\t<hostname>fw-01</hostname>
\t\t<group>
\t\t\t<name>all</name>
\t\t\t<description><![CDATA[All Users]]></description>
\t\t\t<scope>system</scope>
\t\t\t<gid>1998</gid>
\t\t\t<member>0</member>
\t\t\t<member>2000</member>
\t\t\t<member>2001</member>
\t\t</group>
\t\t<group>
\t\t\t<name>admins</name>
\t\t\t<scope>system</scope>
\t\t\t<gid>1999</gid>
\t\t\t<member>0</member>
\t\t\t<priv>page-all</priv>
\t\t</group>
\t\t<group>
\t\t\t<name>vpn-users</name>
\t\t\t<scope>local</scope>
\t\t\t<gid>2000</gid>
\t\t\t<member>2000</member>
\t\t\t<member>2001</member>
\t\t</group>
\t\t<user>
\t\t\t<name>admin</name>
\t\t\t<descr><![CDATA[System Administrator]]></descr>
\t\t\t<scope>system</scope>
\t\t\t<groupname>admins</groupname>
\t\t\t<bcrypt-hash>HASH_PLACEHOLDER_0</bcrypt-hash>
\t\t\t<uid>0</uid>
\t\t\t<priv>user-shell-access</priv>
\t\t</user>
\t\t<user>
\t\t\t<scope>user</scope>
\t\t\t<bcrypt-hash>HASH_PLACEHOLDER_1</bcrypt-hash>
\t\t\t<descr><![CDATA[Example User A]]></descr>
\t\t\t<name>user-a</name>
\t\t\t<expires>12/31/2026</expires>
\t\t\t<uid>2000</uid>
\t\t</user>
\t\t<user>
\t\t\t<scope>user</scope>
\t\t\t<bcrypt-hash>HASH_PLACEHOLDER_2</bcrypt-hash>
\t\t\t<descr></descr>
\t\t\t<name>user-b</name>
\t\t\t<expires></expires>
\t\t\t<disabled></disabled>
\t\t\t<uid>2001</uid>
\t\t</user>
\t\t<nextuid>2002</nextuid>
\t</system>
\t<staticroutes>
\t\t<route>
\t\t\t<network>192.0.2.0/24</network>
\t\t\t<gateway>GW_EXAMPLE</gateway>
\t\t\t<descr><![CDATA[example route]]></descr>
\t\t</route>
\t\t<route>
\t\t\t<network>198.51.100.0/24</network>
\t\t\t<gateway>GW_OTHER</gateway>
\t\t\t<descr></descr>
\t\t\t<disabled/>
\t\t</route>
\t\t<route>
\t\t\t<network>203.0.113.5/24</network>
\t\t\t<gateway>GW_EXAMPLE</gateway>
\t\t</route>
\t</staticroutes>
</pfsense>
"""

#: User-only secret fields. ``authorizedkeys`` holds a public key and
#: ``cert`` a certificate reference: not secret, but not on the allow-list,
#: so they must not come back either. ``future_secret_field`` stands for a
#: field no list knows yet.
_FAKE_CERT_REFID = "6a0f0c0ffee0cafe"
_FAKE_OTP_SEED = "fake-otp-seed-value-0123"
_FAKE_UNKNOWN_SECRET = "fake-unknown-secret-value-9876"
_USER_ONLY_FIELDS = (
    f"<authorizedkeys>{_FAKE_SSH_PUBKEY_B64}</authorizedkeys>"
    f"<cert>{_FAKE_CERT_REFID}</cert>"
    f"<otp_seed>{_FAKE_OTP_SEED}</otp_seed>"
    f"<future_secret_field>{_FAKE_UNKNOWN_SECRET}</future_secret_field>"
    f"<raw_key_blob>{FAKE_PEM_KEY}</raw_key_blob>"
)

#: Every secret value placed in the secret-heavy config below.
_ALL_SECRETS: tuple[str, ...] = (
    *ALL_FAKE_SECRETS,
    *(secret for _, _, secret in _SECRET_CASES),
    _FAKE_SSH_PUBKEY_B64,
    _FAKE_CERT_B64,
    _FAKE_CERT_REFID,
    _FAKE_OTP_SEED,
    _FAKE_UNKNOWN_SECRET,
)


def _secret_heavy_config() -> str:
    """Return a config where every user, group and route carries every secret kind."""
    every_secret = "".join(snippet for _, snippet, _ in _SECRET_CASES)
    return (
        '<?xml version="1.0"?>\n<pfsense>\n'
        "\t<system>\n"
        f"\t\t{every_secret}\n"
        "\t\t<sshdata><sshkeyfile><filename>ssh_host_ed25519_key</filename>"
        f"<xmldata>{FAKE_SSH_HOST_KEY_B64}</xmldata></sshkeyfile></sshdata>\n"
        "\t\t<group><name>vpn-users</name><member>2000</member>"
        f"{every_secret}</group>\n"
        "\t\t<user><name>user-a</name><descr>Example User A</descr>"
        "<scope>user</scope><uid>2000</uid><expires>12/31/2026</expires>"
        f"<ipsecpsk>{FAKE_PSK}</ipsecpsk><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash>"
        f"{_USER_ONLY_FIELDS}{every_secret}</user>\n"
        "\t\t<user><name>user-b</name><uid>2001</uid><disabled/>"
        f"{every_secret}{_USER_ONLY_FIELDS}</user>\n"
        "\t</system>\n"
        f"\t<cert><refid>{_FAKE_CERT_REFID}</refid><crt>{_FAKE_CERT_B64}</crt>"
        f"{every_secret}</cert>\n"
        "\t<staticroutes>\n"
        "\t\t<route><network>192.0.2.0/24</network><gateway>GW_EXAMPLE</gateway>"
        f"<descr>example route</descr>{every_secret}</route>\n"
        f"\t\t{every_secret}\n"
        "\t</staticroutes>\n"
        "</pfsense>\n"
    )


# ---------------------------------------------------------------------------
# No secrets
# ---------------------------------------------------------------------------


def test_secret_heavy_config_is_valid_xml_holding_every_secret() -> None:
    """Guard the fixture itself: it parses, and every secret is really in it."""
    xml = _secret_heavy_config()
    assert list_local_users(xml)  # parses
    for secret in _ALL_SECRETS:
        assert secret in xml


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_attr", ["user_list", "route_static_list"])
async def test_no_secret_value_appears_in_either_result(handler_attr: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_secret_heavy_config())
        result = await getattr(connector, handler_attr)(_TARGET, {})
    assert result["total"] >= 1
    dumped = json.dumps(result) + repr(result)
    for secret in _ALL_SECRETS:
        assert secret not in dumped, f"a secret value leaked into {handler_attr}"


def test_rows_hold_exactly_the_allow_listed_keys() -> None:
    xml = _secret_heavy_config()
    users = list_local_users(xml)
    routes = list_static_routes(xml)
    assert [u["name"] for u in users] == ["user-a", "user-b"]
    assert len(routes) == 1
    for row in users:
        assert tuple(row) == USER_ROW_FIELDS
    for row in routes:
        assert tuple(row) == ROUTE_ROW_FIELDS


# ---------------------------------------------------------------------------
# Field mapping, disabled, expires, groups
# ---------------------------------------------------------------------------


def test_user_field_mapping() -> None:
    rows = list_local_users(_CONFIG)
    assert rows == [
        {
            "name": "admin",
            "descr": "System Administrator",
            "scope": "system",
            "disabled": False,
            "expires": None,
            "uid": "0",
            "groups": ["admins", "all"],
        },
        {
            "name": "user-a",
            "descr": "Example User A",
            "scope": "user",
            "disabled": False,
            "expires": "12/31/2026",
            "uid": "2000",
            "groups": ["all", "vpn-users"],
        },
        {
            "name": "user-b",
            "descr": None,
            "scope": "user",
            "disabled": True,
            "expires": None,
            "uid": "2001",
            "groups": ["all", "vpn-users"],
        },
    ]


def test_route_field_mapping() -> None:
    rows = list_static_routes(_CONFIG)
    assert rows == [
        {
            "network": "192.0.2.0/24",
            "gateway": "GW_EXAMPLE",
            "descr": "example route",
            "disabled": False,
        },
        {
            "network": "198.51.100.0/24",
            "gateway": "GW_OTHER",
            "descr": None,
            "disabled": True,
        },
        {
            "network": "203.0.113.5/24",
            "gateway": "GW_EXAMPLE",
            "descr": None,
            "disabled": False,
        },
    ]


@pytest.mark.parametrize(
    ("disabled_xml", "expected"),
    [
        ("", False),
        ("<disabled/>", True),
        ("<disabled></disabled>", True),
        ("<disabled>yes</disabled>", True),
    ],
    ids=["absent", "self-closing", "empty", "with-text"],
)
def test_disabled_flag_is_presence_based(disabled_xml: str, expected: bool) -> None:
    """pfSense treats the element as set when it is there (``isset``), whatever it holds."""
    users = list_local_users(
        f"<pfsense><system><user><name>u</name>{disabled_xml}</user></system></pfsense>"
    )
    routes = list_static_routes(
        f"<pfsense><staticroutes><route><network>192.0.2.0/24</network>"
        f"{disabled_xml}</route></staticroutes></pfsense>"
    )
    assert users[0]["disabled"] is expected
    assert routes[0]["disabled"] is expected


@pytest.mark.parametrize(
    ("expires_xml", "expected"),
    [
        ("", None),
        ("<expires/>", None),
        ("<expires></expires>", None),
        ("<expires>  </expires>", None),
        ("<expires>01/15/2027</expires>", "01/15/2027"),
        ("<expires> 01/15/2027 </expires>", "01/15/2027"),
    ],
    ids=["absent", "self-closing", "empty", "blank", "date", "date-with-spaces"],
)
def test_expires_is_the_stored_date_or_none(expires_xml: str, expected: str | None) -> None:
    rows = list_local_users(
        f"<pfsense><system><user><name>u</name>{expires_xml}</user></system></pfsense>"
    )
    assert rows[0]["expires"] == expected


def test_group_membership_comes_from_member_uid_lists() -> None:
    xml = (
        "<pfsense><system>"
        "<group><name>zeta</name><member>10</member><member> 11 </member></group>"
        "<group><name>alpha</name><member>10</member></group>"
        "<group><member>10</member></group>"  # no name: skipped
        "<group><name>empty</name></group>"
        "<group><name>alpha</name><member>10</member></group>"  # duplicate name
        "<user><name>in-two</name><uid>10</uid></user>"
        "<user><name>in-one</name><uid>11</uid></user>"
        "<user><name>in-none</name><uid>12</uid></user>"
        "<user><name>no-uid</name></user>"
        "</system></pfsense>"
    )
    groups = {row["name"]: row["groups"] for row in list_local_users(xml)}
    assert groups == {
        "in-two": ["alpha", "zeta"],
        "in-one": ["zeta"],
        "in-none": [],
        "no-uid": [],
    }


def test_only_direct_children_are_read() -> None:
    """A ``<user>`` or ``<staticroutes>`` outside its real place is not listed."""
    xml = (
        "<pfsense>"
        "<installedpackages><pkg><user><name>not-a-local-user</name></user></pkg>"
        "<staticroutes><route><network>203.0.113.0/24</network></route></staticroutes>"
        "</installedpackages>"
        "<system><user><name>real</name><cert><name>not-the-user-name</name></cert></user>"
        "</system>"
        "</pfsense>"
    )
    assert [row["name"] for row in list_local_users(xml)] == ["real"]
    assert list_static_routes(xml) == []


# ---------------------------------------------------------------------------
# Empty config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xml",
    [
        "<pfsense></pfsense>",
        '<?xml version="1.0"?>\n<pfsense>\n\t<system></system>\n</pfsense>\n',
        "<pfsense><system/><staticroutes/></pfsense>",
    ],
    ids=["bare-root", "empty-system", "empty-sections"],
)
def test_empty_config_returns_no_rows(xml: str) -> None:
    assert list_local_users(xml) == []
    assert list_static_routes(xml) == []


@pytest.mark.asyncio
async def test_handlers_return_empty_envelope_for_empty_config() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("<pfsense></pfsense>")
        users = await connector.user_list(_TARGET, {})
        routes = await connector.route_static_list(_TARGET, {})
    assert users == {"rows": [], "total": 0}
    assert routes == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# Parse failure and read failure
# ---------------------------------------------------------------------------

_BROKEN_INPUTS: list[tuple[str, str]] = [
    (
        "unclosed",
        f"<pfsense><system><user><name>u</name><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash>",
    ),
    ("not-xml", f"password={FAKE_PSK}\n"),
    (
        "entity",
        f'<!DOCTYPE pfsense [<!ENTITY leak "{FAKE_PSK}">]><pfsense>&leak;</pfsense>',
    ),
    ("wrong-root", f"<config><system><user><name>{FAKE_PSK}</name></user></system></config>"),
    ("empty", ""),
    ("blank", "  \n\t\n"),
]


@pytest.mark.parametrize(
    "xml", [xml for _, xml in _BROKEN_INPUTS], ids=[case for case, _ in _BROKEN_INPUTS]
)
@pytest.mark.parametrize("parser", [list_local_users, list_static_routes])
def test_parse_failure_raises_without_file_content(xml: str, parser: Any) -> None:
    with pytest.raises(PfSenseConfigParseError) as excinfo:
        parser(xml)
    message = str(excinfo.value)
    assert "no rows were returned" in message
    assert FAKE_BCRYPT not in message
    assert FAKE_PSK not in message
    # The parser's own exception (which can quote the file) is not chained.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "handler_attr"),
    [("pfsense.user.list", "user_list"), ("pfsense.route.static.list", "route_static_list")],
)
async def test_handler_parse_failure_fails_the_call_without_file_content(
    op_id: str, handler_attr: str
) -> None:
    connector = PfSenseConnector()
    broken = f"<pfsense><system><user><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash>"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(broken)
        with pytest.raises(PfSenseConfigParseError) as excinfo:
            await getattr(connector, handler_attr)(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "cat /cf/conf/config.xml", operator=None)
    # The dispatcher turns the exception into a connector_error envelope.
    envelope = result_connector_error(op_id, excinfo.value, 1.0)
    assert envelope.status == "error"
    assert FAKE_BCRYPT not in envelope.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_attr", ["user_list", "route_static_list"])
async def test_handler_read_failure_raises(handler_attr: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("", exit_status=1)
        with pytest.raises(RuntimeError, match=r"cat /cf/conf/config\.xml exit 1"):
            await getattr(connector, handler_attr)(_TARGET, {})


# ---------------------------------------------------------------------------
# From list to delete
# ---------------------------------------------------------------------------


def test_listed_network_finds_exactly_that_route_for_delete() -> None:
    """Each row's ``network`` is accepted by ``pfsense.route.static.delete`` and
    resolves to that one route -- also for a route stored with host bits set."""
    delete_params = Draft202012Validator(_op("pfsense.route.static.delete").parameter_schema)
    for row in list_static_routes(_CONFIG):
        params = {"network": row["network"]}
        delete_params.validate(params)
        matches = _match_static_routes_canonical(_CONFIG, _validate_network_cidr(row["network"]))
        assert len(matches) == 1
        assert matches[0]["network"] == row["network"]
        assert matches[0]["gateway"] == row["gateway"]


# ---------------------------------------------------------------------------
# Response schemas, registry and metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "handler_attr"),
    [("pfsense.user.list", "user_list"), ("pfsense.route.static.list", "route_static_list")],
)
async def test_handler_output_matches_response_schema(op_id: str, handler_attr: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_CONFIG)
        result = await getattr(connector, handler_attr)(_TARGET, {})
    assert result["total"] == 3
    validator = Draft202012Validator(_op(op_id).response_schema)
    validator.validate(result)
    # The row schema is the allow-list: an extra key is rejected.
    extra = {"rows": [{**result["rows"][0], "bcrypt-hash": "x"}], "total": 1}
    assert not validator.is_valid(extra)


@pytest.mark.parametrize(
    ("op_id", "handler_attr", "group_key"),
    [
        ("pfsense.user.list", "user_list", "users"),
        ("pfsense.route.static.list", "route_static_list", "routing"),
    ],
)
def test_op_metadata(op_id: str, handler_attr: str, group_key: str) -> None:
    op = _op(op_id)
    assert op.handler_attr == handler_attr
    assert hasattr(PfSenseConnector, handler_attr)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == group_key
    assert "read-only" in op.tags
    assert "pfsense" in op.tags
    assert op.parameter_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert op.llm_instructions is not None
    assert op_id in op.llm_instructions["when_to_use"]
    assert op.llm_instructions["output_shape"]
    assert op.llm_instructions["parameter_hints"] == {}
    assert op_id in _WHEN_TO_USE_BY_GROUP[group_key]


def test_users_group_has_curated_when_to_use() -> None:
    text = _WHEN_TO_USE_BY_GROUP["users"]
    assert "pfsense.user.list" in text
    assert "Operations grouped under" not in text


def test_config_show_points_to_the_two_safe_reads() -> None:
    op = _op("pfsense.config.show")
    assert op.llm_instructions is not None
    for text in (
        op.description,
        op.llm_instructions["when_to_use"],
        _WHEN_TO_USE_BY_GROUP["config"],
    ):
        assert "pfsense.user.list" in text
        assert "pfsense.route.static.list" in text
