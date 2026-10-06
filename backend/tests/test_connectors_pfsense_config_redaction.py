# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the secret removal in ``pfsense.config.show``.

Every value here is synthetic: fake hashes and key blocks with the right
shape and junk inside. No real key, hash, host, address or user name is used.
The PEM header is built at runtime so the source never carries a literal
private-key block.

Coverage:

* each kind of secret is removed (known names, name patterns, value shapes);
* normal content is unchanged byte for byte;
* a base64-wrapped private key is caught, also line-wrapped and deflated;
* CDATA is handled; every attribute value and comment is replaced;
* an unknown element whose value is a private key is caught by its shape;
* the fail-closed path never returns the raw file, and its error never
  quotes the file;
* ``redacted_count`` is correct;
* the handler returns the cleaned text, its length and the count.

The rules added after review (ACME, package text fields, base64 text, URLs,
DER keys, the allow-list, depth and timing) are in
``test_connectors_pfsense_config_redaction_rules.py``.
"""

from __future__ import annotations

import base64
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense import ops_read as ops_read_module
from meho_backplane.connectors.pfsense.redaction import (
    REDACTED,
    ConfigRedactionError,
    is_secret_name,
    looks_like_secret,
    redact_config_xml,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Synthetic secrets (right shape, junk inside)
# ---------------------------------------------------------------------------

_DASHES = "-----"
_PEM_LABEL = "PRIVATE" + " KEY"
_JUNK_B64 = base64.b64encode(b"not a real key, only test junk " * 3).decode()
FAKE_PEM_KEY = (
    f"{_DASHES}BEGIN {_PEM_LABEL}{_DASHES}\n{_JUNK_B64}\n{_DASHES}END {_PEM_LABEL}{_DASHES}\n"
)
FAKE_RSA_PEM_KEY = FAKE_PEM_KEY.replace(_PEM_LABEL, "RSA " + _PEM_LABEL)
FAKE_OPENVPN_KEY = (
    f"{_DASHES}BEGIN OpenVPN Static key V1{_DASHES}\n"
    + "\n".join(["0123456789abcdef" * 2] * 4)
    + f"\n{_DASHES}END OpenVPN Static key V1{_DASHES}\n"
)
FAKE_BCRYPT = "$2y$10$" + "a" * 22 + "B" * 31
FAKE_SHA512_CRYPT = "$6$fakesalt$" + "c" * 86
FAKE_MD5_HEX = "d" * 32
FAKE_PASSWORD = "fake-password-value-1"
FAKE_PSK = "fake-pre-shared-key-value"
FAKE_TOKEN = "fake-api-token-value-123"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _b64_deflated(text: str) -> str:
    """base64(raw deflate(text)) -- what pfSense writes for ``sshdata``."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return base64.b64encode(compressor.compress(text.encode()) + compressor.flush()).decode()


FAKE_PEM_KEY_B64 = _b64(FAKE_PEM_KEY)
FAKE_OPENVPN_KEY_B64 = _b64(FAKE_OPENVPN_KEY)
FAKE_SSH_HOST_KEY_B64 = _b64_deflated(FAKE_PEM_KEY.replace(_PEM_LABEL, "OPENSSH " + _PEM_LABEL))

#: Public material that must stay: a fake certificate and an SSH public key.
_FAKE_CERT_B64 = _b64(
    f"{_DASHES}BEGIN CERTIFICATE{_DASHES}\n{_JUNK_B64}\n{_DASHES}END CERTIFICATE{_DASHES}\n"
)
_FAKE_SSH_PUBKEY_B64 = _b64("ssh-ed25519 " + "A" * 68 + " test@example.invalid\n")
#: A WireGuard-shaped public key: 32 fixed bytes, base64.
_FAKE_WG_PUBKEY_B64 = base64.b64encode(bytes(range(100, 132))).decode()

ALL_FAKE_SECRETS = (
    FAKE_PEM_KEY,
    FAKE_PEM_KEY_B64,
    FAKE_OPENVPN_KEY_B64,
    FAKE_SSH_HOST_KEY_B64,
    FAKE_BCRYPT,
    FAKE_SHA512_CRYPT,
    FAKE_PASSWORD,
    FAKE_PSK,
    FAKE_TOKEN,
)


def _wrap(body: str) -> str:
    return f'<?xml version="1.0"?>\n<pfsense>\n{body}\n</pfsense>\n'


# ---------------------------------------------------------------------------
# Each kind of secret is removed
# ---------------------------------------------------------------------------

_SECRET_CASES: list[tuple[str, str, str]] = [
    # (case id, element snippet holding the secret, the secret text)
    ("user-bcrypt-hash", f"<bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash>", FAKE_BCRYPT),
    ("user-sha512-hash", f"<sha512-hash>{FAKE_SHA512_CRYPT}</sha512-hash>", FAKE_SHA512_CRYPT),
    ("user-md5-hash", f"<md5-hash>{FAKE_MD5_HEX}</md5-hash>", FAKE_MD5_HEX),
    ("user-ipsecpsk", f"<ipsecpsk>{FAKE_PSK}</ipsecpsk>", FAKE_PSK),
    ("cert-prv", f"<prv>{FAKE_PEM_KEY_B64}</prv>", FAKE_PEM_KEY_B64),
    (
        "openvpn-shared-key",
        f"<shared_key>{FAKE_OPENVPN_KEY_B64}</shared_key>",
        FAKE_OPENVPN_KEY_B64,
    ),
    ("openvpn-tls", f"<tls>{FAKE_OPENVPN_KEY_B64}</tls>", FAKE_OPENVPN_KEY_B64),
    ("openvpn-auth-pass", f"<auth_pass><![CDATA[{FAKE_PASSWORD}]]></auth_pass>", FAKE_PASSWORD),
    ("openvpn-proxy-passwd", f"<proxy_passwd>{FAKE_PASSWORD}</proxy_passwd>", FAKE_PASSWORD),
    ("ipsec-psk", f"<pre-shared-key>{FAKE_PSK}</pre-shared-key>", FAKE_PSK),
    ("ipsec-pkcs11pin", f"<pkcs11pin>{FAKE_PSK}</pkcs11pin>", FAKE_PSK),
    ("ldap-bindpw", f"<ldap_bindpw>{FAKE_PASSWORD}</ldap_bindpw>", FAKE_PASSWORD),
    ("radius-secret", f"<radius_secret>{FAKE_PSK}</radius_secret>", FAKE_PSK),
    ("hasync-password", f"<password><![CDATA[{FAKE_PASSWORD}]]></password>", FAKE_PASSWORD),
    ("wpa-passphrase", f"<passphrase>{FAKE_PASSWORD}</passphrase>", FAKE_PASSWORD),
    ("rfc2136-keydata", f"<keydata>{FAKE_TOKEN}</keydata>", FAKE_TOKEN),
    ("dhcp-omapi-key", f"<omapi_key>{FAKE_TOKEN}</omapi_key>", FAKE_TOKEN),
    ("ntp-serverauthkey", f"<serverauthkey>{FAKE_TOKEN}</serverauthkey>", FAKE_TOKEN),
    ("snmp-rocommunity", f"<rocommunity>{FAKE_PSK}</rocommunity>", FAKE_PSK),
    ("snmp-trapstring", f"<trapstring>{FAKE_PSK}</trapstring>", FAKE_PSK),
    ("pushover-apikey", f"<apikey>{FAKE_TOKEN}</apikey>", FAKE_TOKEN),
    ("telegram-api", f"<api>{FAKE_TOKEN}</api>", FAKE_TOKEN),
    (
        "acb-encryption-password",
        f"<encryption_password>{FAKE_PASSWORD}</encryption_password>",
        FAKE_PASSWORD,
    ),
    ("system-proxypass", f"<proxypass>{FAKE_PASSWORD}</proxypass>", FAKE_PASSWORD),
    ("voucher-privatekey", f"<privatekey>{FAKE_PEM_KEY_B64}</privatekey>", FAKE_PEM_KEY_B64),
    ("wireguard-presharedkey", f"<presharedkey>{FAKE_PSK}</presharedkey>", FAKE_PSK),
    ("acme-accountkey", f"<accountkey>{FAKE_PEM_KEY_B64}</accountkey>", FAKE_PEM_KEY_B64),
    (
        "acme-dns-token",
        f"<dns_exampleprovidertoken>{FAKE_TOKEN}</dns_exampleprovidertoken>",
        FAKE_TOKEN,
    ),
    # Name patterns: package fields no list names.
    (
        "pattern-password",
        f"<smtp_password_field>{FAKE_PASSWORD}</smtp_password_field>",
        FAKE_PASSWORD,
    ),
    ("pattern-pass-suffix", f"<db_pass>{FAKE_PASSWORD}</db_pass>", FAKE_PASSWORD),
    ("pattern-secret", f"<client-secret-value>{FAKE_TOKEN}</client-secret-value>", FAKE_TOKEN),
    ("pattern-token-suffix", f"<auth_token>{FAKE_TOKEN}</auth_token>", FAKE_TOKEN),
    ("pattern-api-key", f"<service_api_key>{FAKE_TOKEN}</service_api_key>", FAKE_TOKEN),
    ("pattern-hash-suffix", f"<legacy_hash>{FAKE_MD5_HEX}</legacy_hash>", FAKE_MD5_HEX),
    # Context rule: PPPoE server users carry base64 passwords in <username>.
    (
        "pppoe-server-users",
        f"<pppoes><pppoe><username>user1:{_b64(FAKE_PASSWORD)}:</username></pppoe></pppoes>",
        _b64(FAKE_PASSWORD),
    ),
]


@pytest.mark.parametrize(
    ("snippet", "secret"),
    [(snippet, secret) for _, snippet, secret in _SECRET_CASES],
    ids=[case_id for case_id, _, _ in _SECRET_CASES],
)
def test_each_secret_kind_is_removed(snippet: str, secret: str) -> None:
    xml = _wrap(f"\t<section><descr>kept</descr>{snippet}</section>")
    redacted, count = redact_config_xml(xml)
    assert secret not in redacted
    assert REDACTED in redacted
    assert "<descr>kept</descr>" in redacted
    assert count == 1


def test_ssh_host_key_backup_subtree_is_removed() -> None:
    """``<sshdata>`` holds base64(deflate(private key)); the whole block goes."""
    xml = _wrap(
        "\t<sshdata>\n\t\t<sshkeyfile>\n"
        "\t\t\t<filename>ssh_host_ed25519_key</filename>\n"
        f"\t\t\t<xmldata>{FAKE_SSH_HOST_KEY_B64}</xmldata>\n"
        "\t\t</sshkeyfile>\n\t</sshdata>"
    )
    redacted, count = redact_config_xml(xml)
    assert FAKE_SSH_HOST_KEY_B64 not in redacted
    assert count == 2  # filename + xmldata, both below the secret element


# ---------------------------------------------------------------------------
# Normal content is unchanged byte for byte
# ---------------------------------------------------------------------------

#: A synthetic config with no secret in it, written the way pfSense writes
#: (no comments, no attributes): tabs, CRLF, an XML declaration, CDATA with
#: entities, self-closing and empty elements, a public certificate, an SSH
#: public key, a WireGuard public key, a plain URL, and settings whose names
#: look a bit like secrets but are not (IPsec hash choices, LAGG hash, the
#: hash algorithm name, a ``bypass`` flag, a plain ``username``, a Wi-Fi
#: rekey interval, a dashboard widget id, FRR route-map settings).
_CLEAN_CONFIG = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n'
    "<pfsense>\r\n"
    "\t<version>23.3</version>\r\n"
    "\t<system>\r\n"
    "\t\t<hostname>fw-example</hostname>\r\n"
    "\t\t<domain>example.invalid</domain>\r\n"
    "\t\t<webgui><protocol>https</protocol><pwhash>bcrypt</pwhash></webgui>\r\n"
    "\t\t<user>\r\n"
    "\t\t\t<name>example-admin</name>\r\n"
    "\t\t\t<descr><![CDATA[Example &amp; admin &lt;ops&gt;]]></descr>\r\n"
    f"\t\t\t<authorizedkeys>{_FAKE_SSH_PUBKEY_B64}</authorizedkeys>\r\n"
    "\t\t\t<priv>user-shell-access</priv>\r\n"
    "\t\t</user>\r\n"
    "\t</system>\r\n"
    "\t<interfaces><wan><if>vtnet0</if><ipaddr>dhcp</ipaddr><enable/></wan></interfaces>\r\n"
    "\t<laggs><lagg><laggif>lagg0</laggif><lagghash>l2,l3,l4</lagghash></lagg></laggs>\r\n"
    "\t<ipsec><phase2><hash-algorithm-option>hmac_sha256</hash-algorithm-option>"
    "<bypass></bypass></phase2></ipsec>\r\n"
    "\t<dyndnses><dyndns><username>example-ddns-user</username></dyndns></dyndnses>\r\n"
    "\t<gateways>\r\n"
    "\t\t<gateway_item>\r\n"
    "\t\t\t<name>WAN_DHCP</name>\r\n"
    "\t\t\t<gateway>dynamic</gateway>\r\n"
    "\t\t\t<descr><![CDATA[Interface WAN_DHCP Gateway]]></descr>\r\n"
    "\t\t\t<defaultgw/>\r\n"
    "\t\t</gateway_item>\r\n"
    "\t</gateways>\r\n"
    "\t<filter><rule><type>pass</type>"
    "<tracker>1000000101</tracker></rule></filter>\r\n"
    "\t<cert><refid>5f00aa</refid><descr><![CDATA[web cert]]></descr>"
    f"<crt>{_FAKE_CERT_B64}</crt></cert>\r\n"
    "\t<unbound><custom_options></custom_options></unbound>\r\n"
    "\t<wireless><wpa><wpa_group_rekey>60</wpa_group_rekey></wpa></wireless>\r\n"
    f"\t<wireguard><peer><publickey>{_FAKE_WG_PUBKEY_B64}</publickey></peer></wireguard>\r\n"
    "\t<widgets><widgetkey>gateways-0</widgetkey></widgets>\r\n"
    "\t<frr><routemap_in>RM-IN</routemap_in><community_set>65000:100</community_set></frr>\r\n"
    "\t<aliases><alias><name>feed</name>"
    "<url>https://lists.example.invalid/feed.txt?format=plain&amp;v=2</url></alias></aliases>\r\n"
    "</pfsense>\r\n"
)


def test_normal_content_is_unchanged_byte_for_byte() -> None:
    redacted, count = redact_config_xml(_CLEAN_CONFIG)
    assert redacted == _CLEAN_CONFIG
    assert count == 0


def test_only_secret_values_change_in_a_mixed_config() -> None:
    """Swap each secret back in: the output equals the input with markers only."""
    template = (
        '<?xml version="1.0"?>\n<pfsense>\n'
        "\t<system><user><name>u1</name><bcrypt-hash>{s1}</bcrypt-hash>"
        "<descr><![CDATA[keep &amp; me]]></descr></user></system>\n"
        "\t<cert><crt>{crt}</crt><prv>{s2}</prv></cert>\n"
        "\t<openvpn><openvpn-server><mode>server_tls</mode>"
        "<tls>{s3}</tls><shared_key>  {s4}\n\t</shared_key></openvpn-server></openvpn>\n"
        "\t<hasync><username>sync</username><password><![CDATA[{s5}]]></password></hasync>\n"
        "</pfsense>\n"
    )
    xml = template.format(
        s1=FAKE_BCRYPT,
        crt=_FAKE_CERT_B64,
        s2=FAKE_PEM_KEY_B64,
        s3=FAKE_OPENVPN_KEY_B64,
        s4=FAKE_OPENVPN_KEY_B64,
        s5=FAKE_PASSWORD,
    )
    expected = template.format(
        s1=REDACTED, crt=_FAKE_CERT_B64, s2=REDACTED, s3=REDACTED, s4=REDACTED, s5=REDACTED
    )
    redacted, count = redact_config_xml(xml)
    assert redacted == expected  # whitespace around a value is kept, too
    assert count == 5


# ---------------------------------------------------------------------------
# Base64, CDATA, attributes, comments, unknown elements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        FAKE_PEM_KEY_B64,
        # Line-wrapped at 64 / 76 columns, as many tools write base64.
        "\n".join(FAKE_PEM_KEY_B64[i : i + 64] for i in range(0, len(FAKE_PEM_KEY_B64), 64)),
        "\n".join(FAKE_PEM_KEY_B64[i : i + 76] for i in range(0, len(FAKE_PEM_KEY_B64), 76)),
        FAKE_OPENVPN_KEY_B64,
        _b64_deflated(FAKE_RSA_PEM_KEY),
        f"prefix text {FAKE_PEM_KEY_B64} suffix text",
    ],
    ids=["plain", "wrapped-64", "wrapped-76", "openvpn", "deflated", "embedded"],
)
def test_base64_wrapped_private_key_is_caught(value: str) -> None:
    xml = _wrap(f"\t<blob_of_bytes>{value}</blob_of_bytes>")
    redacted, count = redact_config_xml(xml)
    assert "<blob_of_bytes>" + REDACTED + "</blob_of_bytes>" in redacted
    assert count == 1


@pytest.mark.parametrize(
    "value",
    [FAKE_PEM_KEY, FAKE_RSA_PEM_KEY, FAKE_OPENVPN_KEY, FAKE_BCRYPT, FAKE_SHA512_CRYPT],
    ids=["pem", "rsa-pem", "openvpn", "bcrypt", "sha512-crypt"],
)
def test_unknown_element_is_caught_by_value_shape(value: str) -> None:
    """An element no name rule knows is still caught by what its value looks like."""
    xml = _wrap(f"\t<somepackage><field_x>{value}</field_x><other>stays</other></somepackage>")
    redacted, count = redact_config_xml(xml)
    assert value not in redacted
    # The value's outer whitespace (a PEM block's last newline) is kept.
    assert f"<field_x>{REDACTED}" in redacted
    assert "<other>stays</other>" in redacted
    assert count == 1


def test_cdata_is_handled() -> None:
    xml = _wrap(
        f"\t<password><![CDATA[{FAKE_PASSWORD}]]></password>\n"
        f"\t<note><![CDATA[{FAKE_PEM_KEY}]]></note>\n"
        "\t<descr><![CDATA[plain <b>text</b> & more]]></descr>"
    )
    redacted, count = redact_config_xml(xml)
    assert f"<password><![CDATA[{REDACTED}]]></password>" in redacted
    assert f"<note><![CDATA[{REDACTED}\n]]></note>" in redacted
    assert "<descr><![CDATA[plain <b>text</b> & more]]></descr>" in redacted
    assert count == 2


def test_cdata_entity_encoded_crypt_hash_is_caught() -> None:
    """pfSense writes CDATA values through ``htmlentities``; decode before checking."""
    encoded = FAKE_BCRYPT.replace("$", "&#36;")
    redacted, count = redact_config_xml(_wrap(f"\t<misc><![CDATA[{encoded}]]></misc>"))
    assert encoded not in redacted
    assert count == 1


def test_every_attribute_value_is_replaced() -> None:
    """pfSense never writes attributes, so any attribute value goes, whatever it holds."""
    xml = _wrap(
        f'\t<item password="{FAKE_PASSWORD}" blob=\'{FAKE_PEM_KEY_B64}\' name="plain" id="" />\n'
        f'\t<prv format="pem">{FAKE_PEM_KEY_B64}</prv>'
    )
    redacted, count = redact_config_xml(xml)
    expected_item = f'<item password="{REDACTED}" blob=\'{REDACTED}\' name="{REDACTED}" id="" />'
    assert expected_item in redacted
    assert f'<prv format="{REDACTED}">{REDACTED}</prv>' in redacted
    assert count == 5  # three filled attributes on <item>, one on <prv>, the <prv> text


def test_every_comment_is_replaced() -> None:
    """pfSense never writes comments, so any comment goes, whatever it holds."""
    xml = _wrap(
        f"\t<!-- old key: {FAKE_PEM_KEY} -->\n"
        f"\t<!-- password: {FAKE_PASSWORD} -->\n"
        "\t<!-- just a note -->\n"
        "\t<!---->"
    )
    redacted, count = redact_config_xml(xml)
    assert f"<!-- {REDACTED}\n -->" in redacted
    assert redacted.count(f"<!-- {REDACTED} -->") == 2
    assert "<!---->" in redacted  # an empty comment has nothing to replace
    assert count == 3


def test_empty_and_whitespace_values_are_not_counted() -> None:
    xml = _wrap("\t<password></password>\n\t<prv>   </prv>\n\t<tls/>")
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


def test_redacted_count_counts_every_replaced_value() -> None:
    users = "".join(
        f"<user><name>u{i}</name><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash></user>" for i in range(7)
    )
    certs = "".join(
        f"<cert><crt>{_FAKE_CERT_B64}</crt><prv>{FAKE_PEM_KEY_B64}</prv></cert>" for _ in range(3)
    )
    redacted, count = redact_config_xml(_wrap(f"\t<system>{users}</system>{certs}"))
    assert count == 10
    assert redacted.count(REDACTED) == 10


# ---------------------------------------------------------------------------
# Name rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "bcrypt-hash",
        "SHA512-HASH",
        "prv",
        "shared_key",
        "tls",
        "pre-shared-key",
        "ldap_bindpw",
        "radius_secret",
        "smtp_password",
        "influx_pass",
        "auth_token",
        "ns:secret_value",
        "password:x",  # a namespace prefix counts too
        "dns_cloudflarekey",
        "rwcommunity",
        "tcpsigpw",
        "rootpw",
        "simpin",
        "tlskey",
        "client_key",
        "access_key",
        "token_value",
        "dns_selectelsl_pswd",
        "dns_efficientipefficientip_creds",
        "dns_kaskas_authdata",
        "upsd_users",
        "telegraf_raw_config",
    ],
)
def test_secret_names(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "crt",
        "csr",
        "descr",
        "authorizedkeys",
        "publickey",
        "hash-algorithm-option",
        "lagghash",
        "pwhash",
        "bypass",
        "ipsecbypass",
        "username",
        "keylen",
        "ddnsdomainkeyname",
        "priv",
        "routemap_in",
        "pubkey",
        "widgetkey",
        "wpa_group_rekey",
        "prefetchkey",
        "tlsauth_keydir",
    ],
)
def test_non_secret_names(name: str) -> None:
    assert not is_secret_name(name)


def test_username_is_secret_only_below_pppoes() -> None:
    assert is_secret_name("username", ["pfsense", "pppoes", "pppoe"])
    assert not is_secret_name("username", ["pfsense", "dyndnses", "dyndns"])


def test_public_material_does_not_look_secret() -> None:
    assert not looks_like_secret(_FAKE_CERT_B64)
    assert not looks_like_secret(_FAKE_SSH_PUBKEY_B64)
    assert not looks_like_secret("costs 5 dollars")


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xml",
    [
        '<?xml version="1.0"?>\n<!DOCTYPE pfsense [<!ENTITY e "x">]>\n<pfsense>&e;</pfsense>\n',
        f"<pfsense><system><password>{FAKE_PASSWORD}</password>",  # truncated file
        f"<pfsense><password>{FAKE_PASSWORD}</passwrd></pfsense>",  # mismatched end tag
        f"<pfsense><password><![CDATA[{FAKE_PASSWORD}</password></pfsense>",  # open CDATA
        f"<pfsense><!-- {FAKE_PASSWORD} </pfsense>",  # open comment
        f"<pfsense><prv {FAKE_PEM_KEY_B64}</prv></pfsense>",  # broken tag
        f"<?note {FAKE_PEM_KEY}?><pfsense/>",  # key in a place the scan does not redact
    ],
    ids=["doctype", "truncated", "mismatched", "open-cdata", "open-comment", "broken-tag", "pi"],
)
def test_unsure_scan_raises(xml: str) -> None:
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(xml)
    # The message is a fixed reason plus an offset; it never quotes the file.
    assert re.fullmatch(r"[a-z ]+( at offset \d+)?", str(excinfo.value))
    assert not any(secret in str(excinfo.value) for secret in ALL_FAKE_SECRETS)


# ---------------------------------------------------------------------------
# Handler
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
    name="pfsense-test",
    host="pfsense.test.invalid",
    port=22,
    secret_ref="meho/testing/pfsense/pfsense-test",
)


def _proc(stdout: str, exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


_SECRET_CONFIG = _wrap(
    f"\t<system><user><name>u1</name><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash></user></system>\n"
    f"\t<cert><crt>{_FAKE_CERT_B64}</crt><prv>{FAKE_PEM_KEY_B64}</prv></cert>\n"
    f"\t<openvpn><openvpn-server><tls>{FAKE_OPENVPN_KEY_B64}</tls></openvpn-server></openvpn>"
)


def _assert_no_fake_secret(result: dict[str, Any]) -> None:
    blob = repr(result)
    for secret in ALL_FAKE_SECRETS:
        assert secret not in blob


@pytest.mark.asyncio
async def test_handler_returns_cleaned_xml_length_and_count() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_SECRET_CONFIG)
        result = await connector.config_show(_TARGET, {})
    _assert_no_fake_secret(result)
    assert result["redacted_count"] == 3
    assert result["config_xml"].count(REDACTED) == 3
    assert result["length"] == len(result["config_xml"])
    assert _FAKE_CERT_B64 in result["config_xml"]  # the public certificate stays
    assert "error" not in result


@pytest.mark.asyncio
async def test_handler_fails_closed_on_unsure_scan() -> None:
    connector = PfSenseConnector()
    broken = _SECRET_CONFIG.replace("</pfsense>", "")  # truncated file
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(broken)
        result = await connector.config_show(_TARGET, {})
    assert result["config_xml"] is None
    assert result["length"] == 0
    assert result["redacted_count"] == 0
    assert result["error"] == (
        "secret removal failed (unclosed element at end of file); the config was not returned"
    )
    _assert_no_fake_secret(result)


@pytest.mark.asyncio
async def test_handler_fails_closed_on_any_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected fault withholds the file; its message is never echoed."""

    def _boom(_xml: str) -> tuple[str, int]:
        raise RuntimeError(f"unexpected fault quoting {FAKE_PASSWORD}")

    monkeypatch.setattr(ops_read_module, "redact_config_xml", _boom)
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_SECRET_CONFIG)
        result = await connector.config_show(_TARGET, {})
    assert result["config_xml"] is None
    assert "RuntimeError" in result["error"]
    _assert_no_fake_secret(result)


@pytest.mark.asyncio
async def test_handler_command_failure_carries_zero_count() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("", exit_status=1)
        result = await connector.config_show(_TARGET, {})
    assert result["config_xml"] is None
    assert result["redacted_count"] == 0
    assert "exit 1" in result["error"]


def test_config_show_stays_safe_and_schema_declares_redacted_count() -> None:
    op = next(op for op in PFSENSE_OPS if op.op_id == "pfsense.config.show")
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.response_schema["properties"]["redacted_count"] == {"type": "integer"}
    validator = Draft202012Validator(op.response_schema)
    validator.validate({"config_xml": "<pfsense/>", "length": 10, "redacted_count": 0})
    assert not validator.is_valid({"config_xml": None, "length": 0, "redacted_count": "3"})
