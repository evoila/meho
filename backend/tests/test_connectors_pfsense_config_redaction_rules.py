# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the secret-removal rules of ``pfsense.config.show``.

The base cases (each secret kind, byte-for-byte clean config, the handler)
are in ``test_connectors_pfsense_config_redaction.py``. This file covers:

* the ACME DNS-provider fields: all 182 password-type fields of the ACME
  package, by their real element names;
* name rules: the ``pw`` and ``key`` endings, ``simpin``, a namespace prefix;
* package text fields (base64) that hold whole config files, by name or by
  context, and base64 text with a secret inside, under any name;
* passwords and secret query parameters in URLs;
* headerless DER private keys, long hex runs, glued and wrapped base64, and
  deflated values too large to check;
* the short list of names that only look secret;
* what pfSense never writes: processing instructions, text outside the
  root element, a value split into pieces;
* error messages: a fixed reason plus an offset, never file content;
* the depth limit, and that a deep or large file stays fast.

Every value here is synthetic. PEM headers are built at runtime so the source
never carries a literal private-key block.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.pfsense import ops_read as ops_read_module
from meho_backplane.connectors.pfsense.redaction import (
    MAX_DEPTH,
    REDACTED,
    ConfigRedactionError,
    is_secret_name,
    looks_like_secret,
    redact_config_xml,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Synthetic values
# ---------------------------------------------------------------------------

#: A value with no secret word and no secret shape in it. When a rule
#: removes it, the rule fired on the name or the context, not the value.
PLAIN = "plain-value-0815"

_DASHES = "-----"
_PEM_LABEL = "PRIVATE" + " KEY"
_JUNK_B64 = base64.b64encode(b"not a real key, only test junk " * 3).decode()
FAKE_PEM_KEY = (
    f"{_DASHES}BEGIN {_PEM_LABEL}{_DASHES}\n{_JUNK_B64}\n{_DASHES}END {_PEM_LABEL}{_DASHES}\n"
)
FAKE_CERT = f"{_DASHES}BEGIN CERTIFICATE{_DASHES}\n{_JUNK_B64}\n{_DASHES}END CERTIFICATE{_DASHES}\n"
FAKE_BCRYPT = "$2y$10$" + "a" * 22 + "B" * 31
#: A BIND DNSSEC ``.private`` file: no PEM armour, a ``PrivateKey:`` line.
FAKE_DNSSEC_PRIVATE_FILE = (
    "Private-key" + "-format: v1.3\n"
    "Algorithm: 13 (ECDSAP256SHA256)\n"
    "PrivateKey: " + base64.b64encode(bytes(range(32))).decode() + "\n"
    "Created: 20260101000000\n"
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _wrap(body: str) -> str:
    return f'<?xml version="1.0"?>\n<pfsense>\n{body}\n</pfsense>\n'


def _line_wrap(text: str, width: int) -> str:
    return "\n".join(text[i : i + width] for i in range(0, len(text), width))


# ---------------------------------------------------------------------------
# ACME DNS-provider fields
# ---------------------------------------------------------------------------

#: Every field the ACME package (``security/pfSense-pkg-acme``, ``acme.inc``)
#: marks as ``'type' => 'password'``, as the element name it is stored under
#: (``<method><field name>``). Many do not say "secret" in their name.
_ACME_PASSWORD_FIELDS: tuple[str, ...] = (
    "dns-someproviderprovider_secret",
    "dns_1984hostingone984hosting_password",
    "dns_acmednsacmedns_password",
    "dns_acmeproxyacmeproxy_password",
    "dns_active24active24_apikey",
    "dns_active24active24_apisecret",
    "dns_adad_api_key",
    "dns_aliali_key",
    "dns_aliali_secret",
    "dns_alviyalviy_token",
    "dns_anxanx_token",
    "dns_artfilesaf_api_password",
    "dns_arvanarvan_token",
    "dns_auroraaurora_key",
    "dns_auroraaurora_secret",
    "dns_autodnsautodns_password",
    "dns_awsaws_secret_access_key",
    "dns_azionazion_password",
    "dns_azureazuredns_bearertoken",
    "dns_azureazuredns_clientsecret",
    "dns_begetbeget_password",
    "dns_bookmynamebookmyname_password",
    "dns_bunnybunny_api_key",
    "dns_cfcf_key",
    "dns_cfcf_token",
    "dns_clouddnsclouddns_password",
    "dns_cloudnscloudns_auth_password",
    "dns_cncn_password",
    "dns_conohaconoha_password",
    "dns_constellixconstellix_key",
    "dns_constellixconstellix_secret",
    "dns_cpanelcpanel_apitoken",
    "dns_curanetcuranet_authsecret",
    "dns_cyoncy_password",
    "dns_ddnssddnss_token",
    "dns_desecdedyn_token",
    "dns_dfdf_password",
    "dns_dgondo_api_key",
    "dns_dnsexitdnsexit_api_key",
    "dns_dnsexitdnsexit_auth_pass",
    "dns_dnshomednshome_subdomainpassword",
    "dns_dnsservicesdnsservices_password",
    "dns_doapido_letoken",
    "dns_dodo_pw",
    "dns_domeneshopdomeneshop_secret",
    "dns_domeneshopdomeneshop_token",
    "dns_dpdp_key",
    "dns_dpidpi_key",
    "dns_dreamhostdh_api_key",
    "dns_duckdnsduckdns_token",
    "dns_durablednsdd_api_key",
    "dns_dyndyn_password",
    "dns_dynudynu_secret",
    "dns_easydnseasydns_key",
    "dns_easydnseasydns_token",
    "dns_edgecenteredgecenter_api_key",
    "dns_edgednsakamai_access_token",
    "dns_edgednsakamai_client_secret",
    "dns_edgednsakamai_client_token",
    "dns_efficientipefficientip_creds",
    "dns_efficientipefficientip_token_key",
    "dns_efficientipefficientip_token_secret",
    "dns_euserveuserv_password",
    "dns_exoscaleexoscale_api_key",
    "dns_exoscaleexoscale_secret_key",
    "dns_fornexfornex_api_key",
    "dns_freednsfreedns_password",
    "dns_freemyipfreemyip_token",
    "dns_gandi_livednsgandi_livedns_key",
    "dns_gandi_livednsgandi_livedns_token",
    "dns_gcoregcore_key",
    "dns_gdgd_secret",
    "dns_geoscalinggeoscaling_password",
    "dns_googledomainsgoogledomains_access_token",
    "dns_he_ddnshe_ddns_key",
    "dns_hehe_password",
    "dns_hetznercloudhetzner_token",
    "dns_hetznerhetzner_token",
    "dns_hexonethexonet_password",
    "dns_hostingdehostingde_apikey",
    "dns_hostuphostup_api_key",
    "dns_huaweicloudhuaweicloud_password",
    "dns_infoblox_uddiinfoblox_uddi_key",
    "dns_infobloxinfoblox_creds",
    "dns_infomaniakinfomaniak_api_token",
    "dns_internetbsinternetbs_api_key",
    "dns_internetbsinternetbs_api_password",
    "dns_inwxinwx_password",
    "dns_inwxinwx_shared_secret",
    "dns_ionos_cloudionos_token",
    "dns_ionosionos_secret",
    "dns_ipv64ipv64_token",
    "dns_ispconfigispc_password",
    "dns_jdjd_access_key_secret",
    "dns_jokerjoker_password",
    "dns_kappernetkappernetdns_key",
    "dns_kappernetkappernetdns_secret",
    "dns_kaskas_authdata",
    "dns_kinghostkinghost_password",
    "dns_knotknot_key",
    "dns_lala_sk",
    "dns_leaseweblsw_key",
    "dns_limacitylimacity_apikey",
    "dns_linode_v4linode_v4_api_key",
    "dns_linodelinode_api_key",
    "dns_loopialoopia_password",
    "dns_lualua_key",
    "dns_meme_key",
    "dns_meme_secret",
    "dns_mgwmmgwm_api_hash",
    "dns_miabmiab_password",
    "dns_mijnhostmijnhost_api_key",
    "dns_misakamisaka_key",
    "dns_mydnsjpmydnsjp_password",
    "dns_mythic_beastsmb_ak",
    "dns_mythic_beastsmb_as",
    "dns_namecheapnamecheap_api_key",
    "dns_namesilonamesilo_key",
    "dns_nanelonanelo_token",
    "dns_nederhostnederhost_key",
    "dns_neodigitneodigit_api_token",
    "dns_netcupnc_apikey",
    "dns_netcupnc_apipw",
    "dns_netlifynetlify_access_token",
    "dns_nicnic_password",
    "dns_njallanetlify_access_token",
    "dns_nmnm_sha256",
    "dns_nsonens1_key",
    "dns_nwnw_api_token",
    "dns_omglolomg_apikey",
    "dns_oneonecom_password",
    "dns_onlineonline_api_key",
    "dns_openprovider_restopenprovider_rest_password",
    "dns_openprovideropenprovider_passwordhash",
    "dns_ovhovh_ak",
    "dns_ovhovh_as",
    "dns_ovhovh_ck",
    "dns_pdnspdns_token",
    "dns_pleskxmlpleskxml_pass",
    "dns_pointhqpointhq_key",
    "dns_porkbunporkbun_api_key",
    "dns_porkbunporkbun_secret_api_key",
    "dns_qcqc_api_key",
    "dns_rackcorprackcorp_apisecret",
    "dns_rage4rage4_token",
    "dns_rcode0rcode0_api_token",
    "dns_regruregru_api_password",
    "dns_schlundtechschlundtech_password",
    "dns_selectelsl_key",
    "dns_selectelsl_pswd",
    "dns_selfhostselfhostdns_password",
    "dns_servercowservercow_api_password",
    "dns_simplysimply_apikey",
    "dns_sotoonsotoon_token",
    "dns_spaceshipspaceship_api_key",
    "dns_spaceshipspaceship_api_secret",
    "dns_technitiumtechnitium_token",
    "dns_tele3tele3_key",
    "dns_tele3tele3_secret",
    "dns_tencenttencent_secretid",
    "dns_tencenttencent_secretkey",
    "dns_timewebtw_token",
    "dns_udrudr_pass",
    "dns_ultraULTRA_PWD",
    "dns_unoeurouno_key",
    "dns_variomediavariomedia_api_token",
    "dns_veespveesp_password",
    "dns_vercelvercel_token",
    "dns_virakcloudvirakcloud_api_token",
    "dns_vscalevscale_api_key",
    "dns_vultrvultr_api_key",
    "dns_websupportws_apikey",
    "dns_websupportws_apisecret",
    "dns_west_cnwest_key",
    "dns_world4youworld4you_password",
    "dns_yandex360yandex360_access_token",
    "dns_yandex360yandex360_client_secret",
    "dns_yandexpdd_token",
    "dns_zilorezilore_key",
    "dns_zoneeditzoneedit_token",
    "dns_zonomizm_key",
    "webrootftppassword",
)


def test_acme_password_field_list_is_complete() -> None:
    assert len(_ACME_PASSWORD_FIELDS) == 182
    assert len(set(_ACME_PASSWORD_FIELDS)) == 182


def test_every_acme_password_field_is_removed() -> None:
    values = {name: f"acme-fake-{index:03d}" for index, name in enumerate(_ACME_PASSWORD_FIELDS)}
    fields = "".join(f"<{name}>{value}</{name}>" for name, value in values.items())
    xml = _wrap(
        "<installedpackages><acme><certificates><item><name>cert1</name>"
        "<a_domainlist><item><name>example.invalid</name><method>dns_example</method>"
        f"{fields}</item></a_domainlist></item></certificates></acme></installedpackages>"
    )
    redacted, count = redact_config_xml(xml)
    assert [name for name, value in values.items() if value in redacted] == []
    assert count == 182
    assert "<name>example.invalid</name>" in redacted
    assert "<method>dns_example</method>" in redacted


def test_acme_dns_fields_are_secret_only_below_acme() -> None:
    assert is_secret_name("dns_ovhovh_as", ["pfsense", "installedpackages", "acme", "item"])
    assert not is_secret_name("dns_ovhovh_as", ["pfsense", "installedpackages", "otherpkg"])
    # Below <acme>, the non-secret provider settings go too (accepted).
    assert is_secret_name("dns_cfcf_zone_id", ["acme"])


# ---------------------------------------------------------------------------
# Name rules
# ---------------------------------------------------------------------------


def test_frr_tcp_md5_password_is_removed() -> None:
    xml = _wrap(
        "<installedpackages><frrglobalraw><config><row>"
        "<tcpsigsrc>src-a</tcpsigsrc><tcpsigdst>dst-b</tcpsigdst>"
        f"<tcpsigpw>{PLAIN}</tcpsigpw><tcpsigbidir>on</tcpsigbidir>"
        "</row></config></frrglobalraw></installedpackages>"
    )
    redacted, count = redact_config_xml(xml)
    assert PLAIN not in redacted
    assert "<tcpsigdst>dst-b</tcpsigdst>" in redacted
    assert count == 1


def test_cellular_sim_pin_is_removed() -> None:
    xml = _wrap(f"<ppps><ppp><type>ppp</type><simpin>{PLAIN}</simpin></ppp></ppps>")
    redacted, count = redact_config_xml(xml)
    assert PLAIN not in redacted
    assert count == 1


@pytest.mark.parametrize(
    "name",
    [
        "pw",
        "rootpw",
        "key",
        "wgkey",
        "tlskey",
        "client_key",
        "access_key",
        "license_key",
        "hmac_key",
        "haproxy_cookie_dynamic_cookie_key",
        "pin",
        "hash",
        "passcode",
        "passwort",
        "bearer",
        "tokens",
        "token_value",
    ],
)
def test_secret_name_rules_catch_package_style_names(name: str) -> None:
    redacted, count = redact_config_xml(_wrap(f"<somepkg><{name}>{PLAIN}</{name}></somepkg>"))
    assert PLAIN not in redacted
    assert count == 1


def test_a_namespace_prefix_counts() -> None:
    redacted, count = redact_config_xml(_wrap(f"<password:x>{PLAIN}</password:x>"))
    assert PLAIN not in redacted
    assert count == 1


def test_context_rules_follow_elements_as_they_open_and_close() -> None:
    xml = _wrap(
        f"<pppoes><pppoe><username>u1:{_b64(PLAIN)}:</username></pppoe></pppoes>\n"
        "<dyndnses><dyndns><username>ddns-user</username></dyndns></dyndnses>\n"
        # Two nested <acme> elements: after the inner one closes, the outer
        # one is still open, so the dns_* field is still secret.
        f"<acme><acme><item/></acme><dns_example_field>{PLAIN}</dns_example_field></acme>\n"
        "<other><dns_example_field>kept</dns_example_field></other>"
    )
    redacted, count = redact_config_xml(xml)
    assert _b64(PLAIN) not in redacted
    assert PLAIN not in redacted
    assert "<username>ddns-user</username>" in redacted
    assert "<dns_example_field>kept</dns_example_field>" in redacted
    assert count == 2


# ---------------------------------------------------------------------------
# Names that only look secret
# ---------------------------------------------------------------------------

_NOT_SECRET_NAMES = (
    "community_action",
    "community_additive",
    "community_match",
    "community_set",
    "password_type",
    "passwordencrypt",
    "snortcommunityrules",
    "usepass",
    "usetoken",
    "useproxypass",
    "hide_secrets",
    "nopasswd",
    "eve_log_files_hash",
    "pfb_dnsvip_skew",
    "varsettingsmotptokenlength",
    "publickey",
    "pubkey",
    "public_key",
    "widgetkey",
    "eve_redis_key",
    "prefetchkey",
    "wpa_gmk_rekey",
    "wpa_group_rekey",
    "wpa_strict_rekey",
    "mobilekey",
    "dhkey",
)


@pytest.mark.parametrize("name", _NOT_SECRET_NAMES)
def test_names_that_only_look_secret_stay_readable(name: str) -> None:
    xml = _wrap(f"<somepkg><{name}>setting-value</{name}></somepkg>")
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


def test_unsure_names_stay_redacted() -> None:
    redacted, count = redact_config_xml(
        _wrap("<suricata><tracked_files_hash>on</tracked_files_hash></suricata>")
    )
    assert REDACTED in redacted
    assert count == 1


def test_a_readable_name_below_a_secret_element_is_still_removed() -> None:
    redacted, count = redact_config_xml(_wrap(f"<sshdata><usepass>{PLAIN}</usepass></sshdata>"))
    assert PLAIN not in redacted
    assert count == 1


# ---------------------------------------------------------------------------
# Package text fields (base64) that hold whole config files
# ---------------------------------------------------------------------------

#: Text with no secret word and no secret shape: only the field rule can
#: remove it.
_INNOCENT_TEXT = "line one\nline two plain-value-0815\n"

#: Where each field sits: the enclosing element names, then the field.
_PACKAGE_TEXT_FIELDS = {
    "bind-dnssec-key-backup": "installedpackages/dnsseckeys/config/filedata",
    "filer-file": "installedpackages/filer/config/filedata",
    "frr-saved-config": "frrglobalraw/config/frr",
    "frr-running-config": "frrglobalraw/config/frrrunning",
    "frr-bgpd-config": "frrglobalraw/config/bgpd",
    "nut-users": "nut/config/upsd_users",
    "telegraf-raw": "telegraf/config/telegraf_raw_config",
    "haproxy-advanced": "haproxy/advanced",
    "haproxy-advanced-backend": "haproxy/ha_pools/item/advanced_backend",
    "haproxy-file": "haproxy/files/item/content",
    "squid-custom-1": "squid/config/custom_options_squid3",
    "squid-custom-2": "squid/config/custom_options2_squid3",
    "squid-custom-3": "squid/config/custom_options3_squid3",
    "netsnmp-custom": "netsnmp/config/custom_options",
    "netsnmptrapd-custom": "netsnmptrapd/config/custom_options",
    "bind-custom": "bind/config/bind_custom_options",
    "zabbix-userparams": "zabbixagentlts/config/userparams",
    "zabbix-advancedparams": "zabbixproxylts/config/advancedparams",
    "ntopng-custom": "ntopng/config/custom_config",
    "snmptt-config": "snmptt/config/snmptt_configfile",
    "syslogng-objects": "syslogngadvanced/config/objectparameters",
}


def _nest(path: str, value: str) -> str:
    names = path.split("/")
    return (
        "".join(f"<{name}>" for name in names)
        + value
        + "".join(f"</{name}>" for name in reversed(names))
    )


@pytest.mark.parametrize(
    "path", list(_PACKAGE_TEXT_FIELDS.values()), ids=list(_PACKAGE_TEXT_FIELDS)
)
def test_package_text_field_is_removed(path: str) -> None:
    value = _b64(_INNOCENT_TEXT)
    redacted, count = redact_config_xml(_wrap(_nest(path, value)))
    assert value not in redacted
    assert count == 1


def test_generic_text_field_names_stay_outside_their_package() -> None:
    value = _b64(_INNOCENT_TEXT)
    xml = _wrap(
        f"<unbound><custom_options>{value}</custom_options></unbound>\n"
        f"<otherpkg><config><filedata>{value}</filedata><content>{value}</content>"
        f"<frr>{value}</frr><advanced>{value}</advanced>"
        f"<custom_config>{value}</custom_config></config></otherpkg>"
    )
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


def test_notes_are_free_text() -> None:
    """The Notes package is not removed by name; only a secret inside it counts."""
    plain_note = _b64("Change window is on Monday.\n")
    note_with_password = _b64("vpn admin password plain-value-0815\n")
    xml = _wrap(
        f"<notes><config><notes>{plain_note}</notes></config>"
        f"<config><notes>{note_with_password}</notes></config></notes>"
    )
    redacted, count = redact_config_xml(xml)
    assert plain_note in redacted
    assert note_with_password not in redacted
    assert count == 1


# ---------------------------------------------------------------------------
# Base64 text with a secret inside, under any name
# ---------------------------------------------------------------------------

_SECRET_TEXTS = {
    "htpasswd-crypt-hash": f"u1:{FAKE_BCRYPT}\n",
    "dnssec-private-key-file": FAKE_DNSSEC_PRIVATE_FILE,
    "frr-neighbor-password": "router bgp 65001\n neighbor peer-b password plain-value-0815\n",
    "telegraf-token": '[[outputs.influxdb_v2]]\n  token = "plain-value-0815"\n',
    "word-ending-pw": "vpn admin pw plain-value-0815\n",
    "word-ending-pass": "requirepass plain-value-0815\n",
    "env-pwd": "MYSQL_PWD=plain-value-0815\n",
    "client-secret": "client_secret: plain-value-0815\n",
    "bearer-token": "Authorization: Bearer plain-value-0815\n",
    "url-password": f"curl https://admin:{PLAIN}@feeds.example.invalid/list\n",
    "base64-pem-inside": _b64(FAKE_PEM_KEY),
}


@pytest.mark.parametrize("text", list(_SECRET_TEXTS.values()), ids=list(_SECRET_TEXTS))
def test_base64_text_with_a_secret_inside_is_removed(text: str) -> None:
    value = _b64(text)
    redacted, count = redact_config_xml(_wrap(f"<somepkg><blob_text>{value}</blob_text></somepkg>"))
    assert value not in redacted
    assert count == 1


_PLAIN_TEXTS = {
    "resolver-options": "server:\n  private-domain: example.invalid\n  prefetch: yes\n",
    "portal-page": "<h1>Welcome</h1>\n<p>Please sign in.</p>\n",
    "certificate": FAKE_CERT,
    "ssh-public-key": "ssh-ed25519 " + "A" * 68 + " admin@example.invalid\n",
    "firewall-rule": "pass in quick on lan from any to any\n",
    "bypass": "bypass the proxy for example.invalid\n",
}


@pytest.mark.parametrize("text", list(_PLAIN_TEXTS.values()), ids=list(_PLAIN_TEXTS))
def test_base64_text_without_a_secret_stays(text: str) -> None:
    value = _b64(text)
    xml = _wrap(f"<somepkg><blob_text>{value}</blob_text></somepkg>")
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


def test_plain_free_text_is_not_checked_for_words() -> None:
    """Descriptions stay readable: secret words only count in decoded base64 text."""
    xml = _wrap("<system><descr>password reset on Monday</descr></system>")
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:{PLAIN}@feeds.example.invalid/list",
        f"ftp://:{PLAIN}@files.example.invalid/",
        f"https://feeds.example.invalid/list?apikey={PLAIN}",
        f"https://feeds.example.invalid/list?format=txt&amp;api_key={PLAIN}",
        f"https://dyn.example.invalid/update?hostname=h1&amp;password={PLAIN}",
        f"https://dyn.example.invalid/update?passwd={PLAIN}",
        f"https://example.invalid/?access_token={PLAIN}",
        f"https://example.invalid/?key={PLAIN}",
        f"https://example.invalid/?secret={PLAIN}",
        f"https://example.invalid/?auth={PLAIN}",
    ],
)
def test_credentials_in_urls_are_removed(url: str) -> None:
    redacted, count = redact_config_xml(
        _wrap(f"<aliases><alias><url>{url}</url></alias></aliases>")
    )
    assert PLAIN not in redacted
    assert count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://feeds.example.invalid/list.txt",
        "https://feeds.example.invalid/list?format=txt&amp;v=2",
        "https://user@feeds.example.invalid/list",
        "https://feeds.example.invalid:8443/list",
    ],
)
def test_plain_urls_stay(url: str) -> None:
    xml = _wrap(f"<aliases><alias><url>{url}</url></alias></aliases>")
    redacted, count = redact_config_xml(xml)
    assert redacted == xml
    assert count == 0


# ---------------------------------------------------------------------------
# Value shapes: DER keys, hex, glued / wrapped / deflated base64
# ---------------------------------------------------------------------------

#: A junk body after a DER header (no real key material).
_DER_JUNK = bytes(range(256)) * 4

_DER_PRIVATE_KEY_HEADS = {
    "pkcs8-rsa": "308204bd020100300d06092a864886f70d0101010500048204a7",
    "pkcs1-rsa": "308204a40201000282010100",
    "sec1-ec": "30770201010420",
    "pkcs8-ed25519": "302e020100300506032b657004220420",
    "pkcs8-encrypted": "308204fe304006092a864886f70d01050d",
}

_DER_PUBLIC_HEADS = {
    "x509-certificate": "308203a33082028ba003020102",
    "public-key-info": "30820122300d06092a864886f70d01010105000382010f00",
    "pkcs1-public-key": "3082010a0282010100",
    "certificate-request": "308202a130820189020100",
}


def _der_b64(head_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(head_hex) + _DER_JUNK).decode()


@pytest.mark.parametrize(
    "head", list(_DER_PRIVATE_KEY_HEADS.values()), ids=list(_DER_PRIVATE_KEY_HEADS)
)
def test_headerless_der_private_key_is_removed(head: str) -> None:
    value = _der_b64(head)
    assert looks_like_secret(value)
    assert looks_like_secret("key follows:\n" + _line_wrap(value, 64))
    redacted, count = redact_config_xml(_wrap(f"<blob_of_bytes>{value}</blob_of_bytes>"))
    assert value not in redacted
    assert count == 1


@pytest.mark.parametrize("head", list(_DER_PUBLIC_HEADS.values()), ids=list(_DER_PUBLIC_HEADS))
def test_public_der_stays(head: str) -> None:
    assert not looks_like_secret(_der_b64(head))


def test_long_hex_run_is_removed() -> None:
    assert looks_like_secret("a1b2" * 64)  # 256 hex digits: key size
    assert looks_like_secret(FAKE_PEM_KEY.encode().hex())
    assert not looks_like_secret("a1b2" * 63)
    assert not looks_like_secret(" ".join(str(port) for port in range(1000, 1400)))


def test_base64_glued_to_text_is_caught() -> None:
    assert looks_like_secret("abc" + _b64(FAKE_PEM_KEY))


@pytest.mark.parametrize("width", [17, 39, 64])
def test_base64_wrapped_inside_text_is_caught(width: int) -> None:
    assert looks_like_secret("key follows:\n" + _line_wrap(_b64(FAKE_PEM_KEY), width))


def test_double_base64_private_key_is_caught() -> None:
    assert looks_like_secret(_b64(_b64(FAKE_PEM_KEY)))


def test_dnssec_private_key_file_is_caught_by_shape() -> None:
    redacted, count = redact_config_xml(
        _wrap(f"<somepkg><blob_text>{FAKE_DNSSEC_PRIVATE_FILE}</blob_text></somepkg>")
    )
    assert "PrivateKey: " not in redacted
    assert count == 1


def test_deflated_value_too_large_to_check_is_removed() -> None:
    """A key header after the first 4 KiB of inflated data cannot hide."""
    hidden = base64.b64encode(_deflate(b"#" * 5000 + b"\n" + FAKE_PEM_KEY.encode())).decode()
    assert looks_like_secret(hidden)
    large = base64.b64encode(_deflate(b"x" * 10_000 + bytes(range(256)))).decode()
    assert looks_like_secret(large)  # too large to check in full: removed too


def test_small_deflated_value_without_a_key_stays() -> None:
    text = " ".join(f"word{index}" for index in range(60)).encode()
    assert not looks_like_secret(base64.b64encode(_deflate(text)).decode())


# ---------------------------------------------------------------------------
# What pfSense never writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xml",
    [
        '<?xml version="1.0"?>\n<pfsense/>\n',
        "<?xml version='1.0' encoding='UTF-8'?><pfsense/>",
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?><pfsense/>',
    ],
)
def test_leading_xml_declaration_is_kept(xml: str) -> None:
    assert redact_config_xml(xml) == (xml, 0)


@pytest.mark.parametrize(
    "xml",
    [
        '\n<?xml version="1.0"?><pfsense/>',
        '<pfsense><?xml version="1.0"?></pfsense>',
        '<?xml version="1.0"?><?xml-stylesheet href="x"?><pfsense/>',
        f'<?xml version="{PLAIN}"?><pfsense/>',
        f'<?xml version="1.0" note="{PLAIN}"?><pfsense/>',
        f"<pfsense><?note {_b64(FAKE_PEM_KEY)}?></pfsense>",
        f"<pfsense><?note password={PLAIN}?></pfsense>",
    ],
)
def test_other_processing_instructions_fail_closed(xml: str) -> None:
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(xml)
    assert excinfo.value.reason == "processing instruction"


@pytest.mark.parametrize(
    "xml",
    [
        f"{FAKE_BCRYPT}\n<pfsense></pfsense>",
        f"<pfsense></pfsense>\npassword={PLAIN}",
        f"<pfsense></pfsense><![CDATA[{PLAIN}]]>",
    ],
)
def test_text_outside_the_root_fails_closed(xml: str) -> None:
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(xml)
    assert excinfo.value.reason == "text outside the root element"


@pytest.mark.parametrize(
    "body",
    [
        # A PEM header split across two CDATA sections.
        f"<blob><![CDATA[{_DASHES}BEGIN PRIV]]><![CDATA[ATE KEY{_DASHES}\n{_JUNK_B64}]]></blob>",
        # A base64 key split across two CDATA sections.
        f"<blob><![CDATA[{_b64(FAKE_PEM_KEY)[:30]}]]><![CDATA[{_b64(FAKE_PEM_KEY)[30:]}]]></blob>",
        # A crypt hash split between text and CDATA.
        f"<blob>$2y$1<![CDATA[0${'a' * 22}]]></blob>",
        # Two text pieces around a comment.
        f"<blob>{PLAIN[:5]}<!-- x -->{PLAIN[5:]}</blob>",
    ],
)
def test_value_split_into_pieces_fails_closed(body: str) -> None:
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(_wrap(body))
    assert excinfo.value.reason == "value split into several pieces"


def test_whitespace_around_one_cdata_value_is_fine() -> None:
    xml = _wrap("<descr>\n\t<![CDATA[kept]]>\n</descr>")
    assert redact_config_xml(xml) == (xml, 0)


# ---------------------------------------------------------------------------
# Error messages: a fixed reason plus an offset, never file content
# ---------------------------------------------------------------------------

_FAIL_CASES = {
    "unrecognised markup": f'<!DOCTYPE x [<!ENTITY e "{PLAIN}">]><pfsense/>',
    "processing instruction": f"<pfsense><?note {PLAIN}?></pfsense>",
    "mismatched end tag": f"<pfsense><password>abc</{PLAIN}></password></pfsense>",
    "elements nested too deep": "<a>" * (MAX_DEPTH + 1) + PLAIN + "</a>" * (MAX_DEPTH + 1),
    "unclosed element at end of file": f"<pfsense><password>{PLAIN}</password>",
    "text outside the root element": f"<pfsense/>{PLAIN}",
    "value split into several pieces": f"<pfsense><a>{PLAIN}<![CDATA[x]]></a></pfsense>",
    "a secret shape survived the scan": "<pfsense><x$2y$10$abc/></pfsense>",
}


@pytest.mark.parametrize(("reason", "xml"), list(_FAIL_CASES.items()), ids=list(_FAIL_CASES))
def test_error_names_a_fixed_reason_and_never_quotes_the_file(reason: str, xml: str) -> None:
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(xml)
    assert excinfo.value.reason == reason
    assert re.fullmatch(rf"{reason}( at offset \d+)?", str(excinfo.value))
    assert PLAIN not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Depth limit and speed
# ---------------------------------------------------------------------------


def test_nesting_up_to_the_limit_is_fine() -> None:
    xml = "<a>" * MAX_DEPTH + "kept" + "</a>" * MAX_DEPTH
    assert redact_config_xml(xml) == (xml, 0)


def test_deep_input_fails_closed_fast() -> None:
    """40,000 levels took 38 s before the depth limit; now the scan stops at once."""
    xml = "<pfsense>" + "<a>" * 40_000 + "x" + "</a>" * 40_000 + "</pfsense>"
    start = time.perf_counter()
    with pytest.raises(ConfigRedactionError) as excinfo:
        redact_config_xml(xml)
    assert time.perf_counter() - start < 1.0
    assert excinfo.value.reason == "elements nested too deep"


def _large_config(target_bytes: int) -> str:
    """A synthetic config of rules, certificates, users and base64 blobs."""
    pem_b64 = _b64(FAKE_PEM_KEY)
    cert_b64 = _b64(
        f"{_DASHES}BEGIN CERTIFICATE{_DASHES}\n"
        + _line_wrap(base64.b64encode(hashlib.sha512(b"cert").digest() * 20).decode(), 64)
        + f"\n{_DASHES}END CERTIFICATE{_DASHES}\n"
    )
    blob_b64 = base64.b64encode(hashlib.sha512(b"blob").digest() * 40).decode()
    parts = ['<?xml version="1.0"?>\n<pfsense>\n']
    size = 0
    index = 0
    while size < target_bytes:
        index += 1
        part = (
            f"\t<filter><rule><tracker>{index}</tracker><type>pass</type>"
            f"<descr><![CDATA[rule {index} &amp; more]]></descr>"
            f"<destination><port>{index % 65535}</port></destination></rule></filter>\n"
        )
        if index % 20 == 0:
            part += f"\t<cert><crt>{cert_b64}</crt><prv>{pem_b64}</prv></cert>\n"
        if index % 15 == 0:
            part += (
                f"\t<user><name>u{index}</name><bcrypt-hash>{FAKE_BCRYPT}</bcrypt-hash></user>\n"
            )
        if index % 50 == 0:
            part += f"\t<unbound><custom_options>{blob_b64}</custom_options></unbound>\n"
        parts.append(part)
        size += len(part)
    parts.append("</pfsense>\n")
    return "".join(parts)


def test_large_input_stays_fast() -> None:
    """About 600 KB takes about 0.1 s on a laptop; the bound leaves room for slow CI."""
    xml = _large_config(600_000)
    start = time.perf_counter()
    redacted, count = redact_config_xml(xml)
    assert time.perf_counter() - start < 5.0
    assert FAKE_BCRYPT not in redacted
    assert count > 0


# ---------------------------------------------------------------------------
# Handler: worker thread, fixed error text, fixed log reason
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


def _proc(stdout: str) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = 0
    return proc


@pytest.mark.asyncio
async def test_handler_runs_the_scan_in_a_worker_thread() -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        patch.object(
            ops_read_module.asyncio, "to_thread", wraps=asyncio.to_thread
        ) as to_thread_spy,
    ):
        mock_cmd.return_value = _proc(_wrap(f"<password>{PLAIN}</password>"))
        result = await connector.config_show(_TARGET, {})
    to_thread_spy.assert_called_once()
    assert to_thread_spy.call_args.args[0] is ops_read_module.redact_config_xml
    assert result["redacted_count"] == 1


@pytest.mark.asyncio
async def test_handler_error_and_log_never_quote_the_file() -> None:
    connector = PfSenseConnector()
    broken = f"<pfsense><password>abc</{PLAIN}></password></pfsense>"
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        capture_logs() as logs,
    ):
        mock_cmd.return_value = _proc(broken)
        result = await connector.config_show(_TARGET, {})
    assert result["config_xml"] is None
    assert re.fullmatch(
        r"secret removal failed \(mismatched end tag at offset \d+\); the config was not returned",
        result["error"],
    )
    events = [entry for entry in logs if entry["event"] == "pfsense_config_redaction_failed"]
    assert len(events) == 1
    assert PLAIN not in repr(events) + result["error"]
