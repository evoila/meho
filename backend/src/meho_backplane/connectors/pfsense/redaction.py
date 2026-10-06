# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret removal for the pfSense configuration read (``pfsense.config.show``).

pfSense keeps its whole configuration in ``/cf/conf/config.xml``. Next to
rules, interfaces and routes, that file holds the firewall's secrets:

* local user password hashes (``bcrypt-hash``, ``sha512-hash``, ...);
* certificate and CA private keys (``<prv>``, a base64-encoded PEM block);
* OpenVPN shared keys and TLS keys (``<shared_key>``, ``<tls>``);
* IPsec pre-shared keys, RADIUS / LDAP passwords, HA sync passwords,
  notification API tokens, dynamic DNS passwords, package secrets.

``pfsense.config.show`` is a ``safe`` read with no approval step, so it must
never return any of these. :func:`redact_config_xml` replaces every secret
value with :data:`REDACTED` **inside the handler**, before the result leaves
it. Every later step (the audit row's ``raw_payload``, the flight-recorder
trace, a stored result, the broadcast feed) therefore only ever sees the
cleaned text.

What counts as a secret
=======================

A value is removed when **any** of three rules fires:

1. **Known element names** (:data:`SECRET_ELEMENT_NAMES`), checked against the
   pfSense 2.7.2 source. The base is pfSense's own "sanitized config" list
   (``$filtered_tags`` in ``src/usr/local/pfSense/include/www/status_output.inc``,
   tag ``RELENG_2_7_2``), minus ``authorizedkeys`` (SSH *public* keys, which
   may stay). On top of that come secret fields found in the core source
   that the list misses (RFC 2136 ``keydata``, DHCP ``omapi_key``, the SNMP
   ``trapstring``, notification tokens, the backed-up SSH host keys under
   ``sshdata``, ...), plus one context rule: the PPPoE server stores its
   users as ``name:base64(password)`` inside ``<pppoes>...<username>``.
2. **Name patterns** (:func:`is_secret_name`), so an unknown element from a
   package is still caught when its name says "secret": ``password``,
   ``passwd``, ``passphrase``, ``secret``, ``psk``, ``bindpw``, ``prv``,
   ``shared_key``, ``api_key``, ``private_key``, ``credential``,
   ``community``, a ``-hash`` / ``_hash`` suffix, names ending in ``pass`` /
   ``pwd`` / ``token`` / ``authkey``, and the ACME package's
   ``dns_*key|password|secret|token|pwd|pw`` fields.
3. **Value shapes** (:func:`looks_like_secret`), checked on **every** text
   value, CDATA section, attribute value and comment, so a secret in an
   element no rule knows is still caught: a PEM private key, an OpenVPN
   static / tls-crypt key, base64 that decodes (or decodes and inflates)
   to one of those, and crypt password hashes (``$1$``, ``$2a$`` / ``$2b$``
   / ``$2y$``, ``$5$``, ``$6$`` and a few relatives).

Once an element is secret, every value below it is secret too (so a whole
``<sshdata>`` block goes). Public certificates (``<crt>``), CSRs and SSH
public keys stay: their base64 decodes to a certificate or a public key,
not to a private key.

How the text is processed
=========================

The file is scanned as text with regular expressions. It is **never** given
to an XML parser, so no entity is ever expanded and no external resource is
ever loaded. The scan copies every byte it does not redact, so the output is
the input with only the secret values swapped -- tags, attributes, comments,
whitespace, CDATA wrappers and entity references all stay byte for byte.

Fail closed
===========

The scan raises :exc:`ConfigRedactionError` when it cannot be sure it saw
every value: markup it does not recognise (for example a ``<!DOCTYPE`` with
an internal subset, which pfSense never writes), a mismatched or unclosed
element, or a secret shape that survives in the output. The handler turns
any exception into ``config_xml: null`` plus an error -- the raw file is
never returned.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import zlib
from collections.abc import Sequence
from typing import Final

__all__ = [
    "REDACTED",
    "SECRET_ELEMENT_NAMES",
    "ConfigRedactionError",
    "is_secret_name",
    "looks_like_secret",
    "redact_config_xml",
]

#: Written in place of every removed value. Same marker the sibling
#: connector scrubs use (keycloak, argocd, nsx, sddc_manager); it holds no
#: XML-special character, so it is safe in text, CDATA and attribute values.
REDACTED: Final[str] = "***REDACTED***"


class ConfigRedactionError(ValueError):
    """The scan could not prove it removed every secret (fail closed)."""


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
    }
)

#: ``(ancestor, element)`` pairs that are secret only below that ancestor.
#: The PPPoE server keeps its users as ``name:base64(password):ip`` in one
#: ``<username>`` element under ``<pppoes>``; a plain ``<username>`` elsewhere
#: (dynamic DNS, HA sync, PPP client) is not a secret.
_SECRET_IN_CONTEXT: Final[frozenset[tuple[str, str]]] = frozenset({("pppoes", "username")})

#: A name (lowercased, ``-`` / ``_`` / ``.`` removed) is secret when it
#: contains any of these.
_SECRET_NAME_PARTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "psk",
    "bindpw",
    "prv",
    "sharedkey",
    "apikey",
    "privatekey",
    "privkey",
    "credential",
    "community",
    "radiuskey",
)

#: A name (same normal form) is secret when it ends with any of these.
#: ``pass`` is checked separately so ``bypass`` stays readable.
_SECRET_NAME_ENDINGS: Final[tuple[str, ...]] = ("pwd", "token", "authkey")

#: ``bcrypt-hash``, ``sha512-hash``, ``md5-hash``, ``nt-hash``, ``*_hash``.
#: A bare "contains hash" rule would also hide non-secret settings such as
#: the IPsec ``hash-algorithm`` choices, the LAGG ``lagghash`` policy and the
#: ``pwhash`` algorithm name, so only the ``-hash`` / ``_hash`` suffix counts.
_HASH_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[-_]hash$")

#: The ACME package's DNS-provider credentials (``dns_<provider><field>``),
#: the same rule pfSense's own sanitizer applies.
_ACME_DNS_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^dns_.+(?:key|password|secret|token|pwd|pw)$"
)

_NAME_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[-_.]")


def is_secret_name(name: str, ancestors: Sequence[str] = ()) -> bool:
    """Return ``True`` when an element / attribute *name* holds a secret.

    *ancestors* are the enclosing element names (any case), used for the
    context rules. A namespace prefix (``ns:name``) is ignored.
    """
    local = name.lower().rsplit(":", 1)[-1]
    if local in SECRET_ELEMENT_NAMES:
        return True
    open_names = {ancestor.lower() for ancestor in ancestors}
    if any(local == element and parent in open_names for parent, element in _SECRET_IN_CONTEXT):
        return True
    compact = _NAME_NOISE_RE.sub("", local)
    if any(part in compact for part in _SECRET_NAME_PARTS):
        return True
    if compact.endswith(_SECRET_NAME_ENDINGS):
        return True
    if compact.endswith("pass") and not compact.endswith("bypass"):
        return True
    return bool(_HASH_NAME_RE.search(local) or _ACME_DNS_NAME_RE.match(local))


# ---------------------------------------------------------------------------
# Value shapes
# ---------------------------------------------------------------------------

#: PEM private keys of every kind (``PRIVATE KEY``, ``RSA PRIVATE KEY``,
#: ``EC PRIVATE KEY``, ``ENCRYPTED PRIVATE KEY``, ``OPENSSH PRIVATE KEY``,
#: ``PGP PRIVATE KEY BLOCK``).
_PEM_PRIVATE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY", re.IGNORECASE
)

#: OpenVPN armours only key material: ``Static key V1`` (shared / tls-auth /
#: tls-crypt) and the ``tls-crypt-v2`` server and client keys.
_OPENVPN_KEY_RE: Final[re.Pattern[str]] = re.compile(r"-----BEGIN OpenVPN\b", re.IGNORECASE)

#: crypt(3) password hashes: MD5 ``$1$``, bcrypt ``$2a$``/``$2b$``/``$2x$``/
#: ``$2y$``, SHA-256 ``$5$``, SHA-512 ``$6$``, plus scrypt / yescrypt /
#: Apache MD5 / Argon2 / Sun MD5 / NetBSD SHA-1.
_CRYPT_HASH_RE: Final[re.Pattern[str]] = re.compile(
    r"\$(?:1|2[abxy]|5|6|7|y|gy|apr1|argon2(?:id|i|d)|md5|sha1)\$"
)

#: A run of base64 long enough to carry a PEM / OpenVPN header. Shorter
#: runs cannot decode to ``-----BEGIN ... PRIVATE KEY``.
_BASE64_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Inflating a base64 value stops after this many bytes: the key headers
#: sit at the very start, and the cap keeps a compression bomb harmless.
_INFLATE_LIMIT: Final[int] = 4096


def _has_key_header(text: str) -> bool:
    return bool(_PEM_PRIVATE_KEY_RE.search(text) or _OPENVPN_KEY_RE.search(text))


def _b64_decode_prefix(run: str) -> bytes | None:
    """Decode the whole 4-character groups of a base64 *run* (``None`` on failure)."""
    body = run.rstrip("=")
    body = body[: len(body) - len(body) % 4]
    if not body:
        return None
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return None


def _inflate_prefix(data: bytes) -> bytes:
    """Return up to :data:`_INFLATE_LIMIT` bytes of *data* inflated, or ``b""``.

    Tries raw deflate (PHP ``gzdeflate``, which pfSense uses for the
    ``sshdata`` key backups) and zlib / gzip framing.
    """
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS | 32):
        try:
            return zlib.decompressobj(wbits).decompress(data, _INFLATE_LIMIT)
        except zlib.error:
            continue
    return b""


def _base64_hides_key(candidate: str) -> bool:
    """``True`` when base64 *candidate* decodes (or inflates) to key material."""
    data = _b64_decode_prefix(candidate)
    if not data:
        return False
    if _has_key_header(data.decode("latin-1")):
        return True
    return _has_key_header(_inflate_prefix(data).decode("latin-1"))


def looks_like_secret(value: str) -> bool:
    """Return ``True`` when *value* has the shape of a secret, whatever its name.

    Checks the raw text and its entity-decoded form (pfSense writes values
    through ``htmlentities``): a PEM private key, an OpenVPN key, a crypt
    hash, or base64 that decodes (or decodes and inflates) to a key. The
    whitespace-stripped whole value is tried as one base64 blob too, so a
    line-wrapped base64 key is caught.
    """
    forms = {value, html.unescape(value)}
    for text in forms:
        if _has_key_header(text) or _CRYPT_HASH_RE.search(text):
            return True
        compact = _WHITESPACE_RE.sub("", text)
        if _BASE64_RUN_RE.fullmatch(compact) and _base64_hides_key(compact):
            return True
        if any(_base64_hides_key(m.group(0)) for m in _BASE64_RUN_RE.finditer(text)):
            return True
    return False


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: One markup token at a ``<``. Anything else at a ``<`` (``<!DOCTYPE``,
#: ``<!ENTITY``, a broken tag) matches nothing and fails the scan closed.
_MARKUP_RE: Final[re.Pattern[str]] = re.compile(
    r"<!--(?P<comment>.*?)-->"
    r"|<!\[CDATA\[(?P<cdata>.*?)\]\]>"
    r"|<\?(?P<pi>.*?)\?>"
    r"|</(?P<end>[^\s<>/!?\"'=]+)\s*>"
    r"|<(?P<start>[^\s<>/!?\"'=]+)"
    r"(?P<attrs>(?:\s+[^\s<>/=\"']+\s*=\s*(?:\"[^\"<]*\"|'[^'<]*'))*)"
    r"\s*(?P<empty>/?)>",
    re.DOTALL,
)

_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<lead>\s+(?P<name>[^\s<>/=\"']+)\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)


def _swap(value: str) -> str:
    """Replace *value* with :data:`REDACTED`, keeping its outer whitespace."""
    lead = len(value) - len(value.lstrip())
    trail = len(value.rstrip())
    return value[:lead] + REDACTED + value[trail:]


class _Scan:
    """One pass over a ``config.xml`` text. Use :func:`redact_config_xml`."""

    def __init__(self, xml: str) -> None:
        self.xml = xml
        self.out: list[str] = []
        self.count = 0
        #: Open elements as ``(name, secret)``; ``secret`` is inherited.
        self.stack: list[tuple[str, bool]] = []

    @property
    def in_secret(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def value(self, value: str, *, secret: bool) -> str:
        """Return *value*, or its redacted form when it is a secret."""
        if not value.strip():
            return value
        if secret or looks_like_secret(value):
            self.count += 1
            return _swap(value)
        return value

    def run(self) -> str:
        xml = self.xml
        pos = 0
        while pos < len(xml):
            lt = xml.find("<", pos)
            if lt == -1:
                lt = len(xml)
            if lt > pos:
                self.out.append(self.value(xml[pos:lt], secret=self.in_secret))
            if lt == len(xml):
                break
            match = _MARKUP_RE.match(xml, lt)
            if match is None:
                raise ConfigRedactionError(f"unrecognised markup at offset {lt}")
            self.markup(match)
            pos = match.end()
        if self.stack:
            raise ConfigRedactionError(f"{len(self.stack)} element(s) left open")
        return "".join(self.out)

    def markup(self, match: re.Match[str]) -> None:
        if match.group("comment") is not None:
            body = self.value(match.group("comment"), secret=self.in_secret)
            self.out.append(f"<!--{body}-->")
        elif match.group("cdata") is not None:
            body = self.value(match.group("cdata"), secret=self.in_secret)
            self.out.append(f"<![CDATA[{body}]]>")
        elif match.group("pi") is not None:
            self.out.append(match.group(0))
        elif match.group("end") is not None:
            self.end_tag(match.group("end"))
            self.out.append(match.group(0))
        else:
            self.start_tag(match)

    def start_tag(self, match: re.Match[str]) -> None:
        name = match.group("start")
        ancestors = [open_name for open_name, _ in self.stack]
        secret = self.in_secret or is_secret_name(name, ancestors)

        def _attr(attr: re.Match[str]) -> str:
            attr_secret = secret or is_secret_name(attr.group("name"))
            new_value = self.value(attr.group("value"), secret=attr_secret)
            return f"{attr.group('lead')}{attr.group('quote')}{new_value}{attr.group('quote')}"

        attrs = _ATTR_RE.sub(_attr, match.group("attrs"))
        whole = match.group(0)
        offset = match.start()
        self.out.append(
            whole[: match.start("attrs") - offset] + attrs + whole[match.end("attrs") - offset :]
        )
        if not match.group("empty"):
            self.stack.append((name, secret))

    def end_tag(self, name: str) -> None:
        if not self.stack or self.stack[-1][0] != name:
            raise ConfigRedactionError(f"mismatched end tag </{name}>")
        self.stack.pop()


def redact_config_xml(xml: str) -> tuple[str, int]:
    """Return ``(xml_without_secrets, redacted_count)`` for a pfSense ``config.xml``.

    Every secret value (see the module docstring) is replaced with
    :data:`REDACTED`; every other byte is copied unchanged.
    ``redacted_count`` is the number of values replaced (text runs, CDATA
    sections, attribute values and comments).

    Raises :exc:`ConfigRedactionError` when the scan cannot be sure it saw
    every value, or when a key / hash shape still appears in the output.
    The caller must then return no XML at all.
    """
    scan = _Scan(xml)
    redacted = scan.run()
    if _has_key_header(redacted) or _CRYPT_HASH_RE.search(redacted):
        raise ConfigRedactionError("a secret shape survived the scan")
    return redacted, scan.count
