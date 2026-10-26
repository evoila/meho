# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Name rules for the pfSense config secret removal (see :mod:`.redaction`).

Which element names hold a secret: the known names, the context rules
(a name that is secret only below a given element), the name patterns and
the short list of names that only look secret.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from typing import Final

__all__ = [
    "NAME_NOISE_RE",
    "SECRET_ELEMENT_NAMES",
    "SECRET_NAME_ENDINGS",
    "SECRET_NAME_PARTS",
    "is_secret_name",
    "is_secret_name_in",
    "local_name",
]

#: Element names whose value is a secret, lowercased. See the module
#: docstring for where each group comes from.
SECRET_ELEMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        # --- pfSense's own sanitized-config list (status_output.inc,
        #     RELENG_2_7_2), minus ``authorizedkeys`` (SSH public keys). -----
        "accountkey",
        "auth_pass",
        "auth_server_shared_secret",
        "auth_server_shared_secret2",
        "auth_user",
        "barnyard_dbpwd",
        "bcrypt-hash",
        "cert_key",
        "community",
        "crypto_password",
        "crypto_password2",
        "dns_nsupdatensupdate_key",
        "ddnsdomainkey",
        "encryption_password",
        "etpro_code",
        "etprocode",
        "gold_encryption_password",
        "gold_password",
        "influx_pass",
        "ipsecpsk",
        "ldap_bindpw",
        "ldapbindpass",
        "ldap_pass",
        "lighttpd_ls_password",
        "maxmind_geoipdb_key",
        "maxmind_key",
        "md5-hash",
        "md5password",
        "md5sigkey",
        "md5sigpass",
        "nt-hash",
        "oinkcode",
        "oinkmastercode",
        "pass",
        "passphrase",
        "password",
        "passwordagain",
        "pkcs11pin",
        "postgresqlpasswordenc",
        "pre-shared-key",
        "presharedkey",
        "privatekey",
        "proxypass",
        "proxy_passwd",
        "proxyuser",
        "proxy_user",
        "prv",
        "radmac_secret",
        "radius_secret",
        "redis_password",
        "redis_passwordagain",
        "rocommunity",
        "secret",
        "secret2",
        "securiteinfo_id",
        "serverauthkey",
        "sha512-hash",
        "shared_key",
        "stats_password",
        "tls",
        "tlspskidentity",
        "tlspskfile",
        "varclientpasswordinput",
        "varclientsharedsecret",
        "varsqlconfpassword",
        "varsqlconf2password",
        "varsyncpassword",
        "varmodulesldappassword",
        "varmodulesldap2password",
        "varusersmotpinitsecret",
        "varusersmotppin",
        "varuserspassword",
        "webrootftppassword",
        # --- Secret fields in the pfSense 2.7.2 core source that the list
        #     above misses. ---------------------------------------------------
        "keydata",  # RFC 2136 dynamic DNS update: the TSIG key secret
        "omapi_key",  # DHCP server OMAPI key
        "trapstring",  # SNMP trap community string
        "vouchersyncpass",  # captive portal voucher sync password
        "apikey",  # notifications: Pushover API token
        "userkey",  # notifications: Pushover user key
        "api",  # notifications: Telegram bot token / Slack token
        "radiuskey",  # legacy captive portal RADIUS keys (upgrade path)
        "radiuskey2",
        "radiuskey3",
        "radiuskey4",
        "sshdata",  # backed-up SSH host keys: base64(gzdeflate(private key file))
        "simpin",  # cellular (PPP) link: the SIM card PIN
        # --- Short generic names that hold a secret wherever they appear.
        #     No pfSense or package setting uses them as a plain setting. ----
        "hash",
        "pin",
        # --- Package text fields (base64) that hold whole config files,
        #     with passwords and keys inside. ---------------------------------
        "upsd_users",  # NUT: the UPS user file (``password = ...``)
        "telegraf_raw_config",  # Telegraf: raw config (output tokens, passwords)
        "bind_custom_options",  # BIND: custom options (TSIG secrets)
        "custom_options_squid3",  # Squid: custom options (``cache_peer ... login=``)
        "custom_options2_squid3",
        "custom_options3_squid3",
        "userparams",  # Zabbix agent: user parameters (command lines)
        "advancedparams",  # Zabbix proxy: advanced parameters
        "snmptt_configfile",  # snmptt: config file
        "objectparameters",  # syslog-ng: object parameters
    }
)

#: ``(ancestor, element)`` pairs that are secret only below that ancestor,
#: because the element name alone is too generic.
_SECRET_IN_CONTEXT: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        # PPPoE server users: ``name:base64(password):ip``. A plain
        # ``<username>`` elsewhere (dynamic DNS, HA sync) is not a secret.
        ("pppoes", "username"),
        # BIND: backed-up DNSSEC key files, including the private keys.
        ("dnsseckeys", "filedata"),
        # Filer: the content of any file the operator manages (scripts).
        ("filer", "filedata"),
        # FRR: the saved and running frr.conf and the per-daemon raw configs
        # (BGP / OSPF passwords).
        ("frrglobalraw", "frr"),
        ("frrglobalraw", "frrrunning"),
        ("frrglobalraw", "zebra"),
        ("frrglobalraw", "bgpd"),
        ("frrglobalraw", "ospfd"),
        ("frrglobalraw", "ospf6d"),
        ("frrglobalraw", "ripd"),
        ("frrglobalraw", "bfdd"),
        # HAProxy: pass-through config text (``stats auth``, ``userlist``)
        # and uploaded files.
        ("haproxy", "advanced"),
        ("haproxy", "advanced_backend"),
        ("haproxy", "content"),
        # net-snmp: custom options (``createUser ... authpass``).
        ("netsnmp", "custom_options"),
        ("netsnmptrapd", "custom_options"),
        # ntopng: custom config text.
        ("ntopng", "custom_config"),
    }
)

#: The same pairs, looked up by element name: element -> ancestors.
_CONTEXT_ANCESTORS: Final[dict[str, frozenset[str]]] = {
    element: frozenset(parent for parent, name in _SECRET_IN_CONTEXT if name == element)
    for _, element in _SECRET_IN_CONTEXT
}

#: Below ``<acme>``, every element whose name starts with ``dns_`` is a
#: DNS-provider setting (``dns_<provider><field>``). Most are API keys and
#: passwords, and the field names do not always say so (``dns_ovhovh_as``,
#: ``dns_lala_sk``). The few non-secret ones (server name, zone) go too.
_ACME_ANCESTOR: Final[str] = "acme"
_ACME_FIELD_PREFIX: Final[str] = "dns_"

#: Names that look secret to the name patterns but only ever hold a plain
#: setting, checked against the package sources. Lowercased, exact match.
_NOT_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "community_action",  # FRR route map: set or match a BGP community
        "community_additive",  # FRR route map: append the community (checkbox)
        "community_match",  # FRR route map: BGP community list name
        "community_set",  # FRR route map: BGP community values (AS:NN)
        "password_type",  # FRR BGP neighbor: which kind of password (select)
        "passwordencrypt",  # FRR: "encrypt passwords" (checkbox)
        "snortcommunityrules",  # Suricata: use the Snort community rules (on/off)
        "usepass",  # OpenVPN client export: protect the file with a password (checkbox)
        "usetoken",  # OpenVPN client export: use the Windows certificate store (checkbox)
        "useproxypass",  # OpenVPN client export: proxy auth method (none/basic/ntlm)
        "hide_secrets",  # WireGuard: hide keys in the GUI (yes/no)
        "nopasswd",  # sudo: no password needed (checkbox)
        "eve_log_files_hash",  # Suricata: hash algorithm name for file logging
        "pfb_dnsvip_skew",  # pfBlockerNG: CARP skew number for the DNSBL VIP
        "varsettingsmotptokenlength",  # FreeRADIUS: mOTP token password length (digits)
        # Names ending in ``key`` that are not secret:
        "publickey",  # WireGuard / IPsec: public key
        "pubkey",  # WireGuard: public key
        "public_key",  # WireGuard: public key
        "widgetkey",  # dashboard widget id
        "eve_redis_key",  # Suricata: Redis list name for EVE logs
        "prefetchkey",  # DNS resolver: prefetch DNSKEY records (checkbox)
        "wpa_gmk_rekey",  # Wi-Fi: master key rekey interval (seconds)
        "wpa_group_rekey",  # Wi-Fi: group key rekey interval (seconds)
        "wpa_strict_rekey",  # Wi-Fi: strict rekey (checkbox)
        "mobilekey",  # IPsec: list of mobile client keys (each entry's key is checked)
        "dhkey",  # OpenVPN wizard: DH parameter length
    }
)

#: A name (lowercased, ``-`` / ``_`` / ``.`` removed) is secret when it
#: contains any of these. The same words are the "secret words" checked in
#: decoded base64 text.
SECRET_NAME_PARTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "passwort",
    "passphrase",
    "passcode",
    "pswd",
    "secret",
    "psk",
    "bindpw",
    "prv",
    "sharedkey",
    "apikey",
    "privatekey",
    "privkey",
    "credential",
    "creds",
    "authdata",
    "community",
    "radiuskey",
    "token",
    "bearer",
)

#: A name (same normal form) is secret when it ends with any of these.
#: ``pass`` is checked separately so ``bypass`` stays readable. A ``pin``
#: ending is not used: it would hide the FRR ``routemap_in`` setting.
SECRET_NAME_ENDINGS: Final[tuple[str, ...]] = ("pwd", "pw")

#: A name (same normal form) ending in ``key`` is secret too (``tlskey``,
#: ``client_key``, ``access_key``, ``haproxy_cookie_dynamic_cookie_key``),
#: unless it is on :data:`_NOT_SECRET_NAMES` (public keys, widget ids, Wi-Fi
#: rekey intervals). Names only: in free text the word "key" is too common.
_KEY_ENDING: Final[str] = "key"

#: ``bcrypt-hash``, ``sha512-hash``, ``md5-hash``, ``nt-hash``, ``*_hash``.
#: A bare "contains hash" rule would also hide non-secret settings such as
#: the IPsec ``hash-algorithm`` choices, the LAGG ``lagghash`` policy and the
#: ``pwhash`` algorithm name, so only the ``-hash`` / ``_hash`` suffix counts.
_HASH_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[-_]hash$")

#: The ACME package's DNS-provider credentials (``dns_<provider><field>``),
#: the same rule pfSense's own sanitizer applies. It also works outside an
#: ``<acme>`` block; inside one, every ``dns_*`` element is secret anyway.
_ACME_DNS_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^dns_.+(?:key|password|secret|token|pwd|pw)$"
)

NAME_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[-_.]")


def local_name(name: str) -> str:
    """Lowercased name without a namespace prefix (``ns:name`` -> ``name``)."""
    return name.lower().rsplit(":", 1)[-1]


def _name_part_is_secret(part: str) -> bool:
    """Apply the known-name and name-pattern rules to one lowercased name."""
    if part in SECRET_ELEMENT_NAMES:
        return True
    if part in _NOT_SECRET_NAMES:
        return False
    compact = NAME_NOISE_RE.sub("", part)
    if any(word in compact for word in SECRET_NAME_PARTS):
        return True
    if compact.endswith(SECRET_NAME_ENDINGS) or compact.endswith(_KEY_ENDING):
        return True
    if compact.endswith("pass") and not compact.endswith("bypass"):
        return True
    return bool(_HASH_NAME_RE.search(part) or _ACME_DNS_NAME_RE.match(part))


def is_secret_name_in(name: str, open_names: Collection[str]) -> bool:
    """:func:`is_secret_name` with the open elements as a ready lookup.

    *open_names* holds the lowercased local names of the enclosing elements;
    the scan keeps it up to date as elements open and close.
    """
    lowered = name.lower()
    # A namespace prefix counts too: ``password:x`` is as secret as ``password``.
    if any(_name_part_is_secret(part) for part in lowered.split(":")):
        return True
    local = lowered.rsplit(":", 1)[-1]
    ancestors = _CONTEXT_ANCESTORS.get(local)
    if ancestors is not None and any(parent in open_names for parent in ancestors):
        return True
    return local.startswith(_ACME_FIELD_PREFIX) and _ACME_ANCESTOR in open_names


def is_secret_name(name: str, ancestors: Iterable[str] = ()) -> bool:
    """Return ``True`` when an element *name* holds a secret.

    *ancestors* are the enclosing element names (any case), used for the
    context rules. Every part of a namespaced name (``ns:name``) is checked.
    """
    return is_secret_name_in(name, {local_name(ancestor) for ancestor in ancestors})
