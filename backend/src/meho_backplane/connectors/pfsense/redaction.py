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

``pfsense.config.show`` is a ``safe`` read with no approval step.
:func:`redact_config_xml` replaces the values of **known secret fields** and
values with a **known secret shape** with :data:`REDACTED`, **inside the
handler**, before the result leaves it. Every later step (the audit row's
``raw_payload``, the flight-recorder trace, a stored result, the broadcast
feed) therefore only sees the cleaned text.

The limit: this is a list of known fields and known shapes, not a proof.
Free-text fields (descriptions, notes, cron or shell commands, custom config
text, URLs) can still hold secrets that someone typed in.

What counts as a secret
=======================

A value is replaced when **any** of these rules fires:

1. **Known element names** (:data:`SECRET_ELEMENT_NAMES`), checked against
   the pfSense 2.7.2 source and the pfSense package sources. The base is
   pfSense's own "sanitized config" list (``$filtered_tags`` in
   ``src/usr/local/pfSense/include/www/status_output.inc``, tag
   ``RELENG_2_7_2``), minus ``authorizedkeys`` (SSH *public* keys, which may
   stay). On top of that come secret fields the list misses (RFC 2136
   ``keydata``, DHCP ``omapi_key``, the SNMP ``trapstring``, notification
   tokens, the backed-up SSH host keys under ``sshdata``, the cellular
   ``simpin``, ...) and package text fields (base64) that hold whole config
   files with passwords in them (NUT ``upsd_users``, Telegraf
   ``telegraf_raw_config``, BIND ``bind_custom_options``, ...).
2. **Context rules**: a name that is secret only below a given element
   (:data:`_SECRET_IN_CONTEXT`). The PPPoE server stores its users as
   ``name:base64(password)`` in ``<pppoes>...<username>``; BIND backs up the
   DNSSEC private key files in ``<dnsseckeys>...<filedata>``; FRR keeps the
   saved and running ``frr.conf`` in ``<frrglobalraw>...<frr>``; HAProxy's
   ``advanced`` text, ... Below ``<acme>``, **every** ``dns_*`` element is a
   DNS-provider setting and is replaced (most are API keys and passwords,
   and their names do not always say so: ``dns_ovhovh_as``).
3. **Name patterns** (:func:`is_secret_name`), so an unknown element from a
   package is still caught when its name says "secret": names containing
   ``password``, ``passwd``, ``passphrase``, ``secret``, ``psk``, ``token``,
   ``api_key``, ``private_key``, ``credential``, ``creds``, ``community``,
   ...; names ending in ``pass`` (not ``bypass``), ``pwd``, ``pw`` or
   ``key``; a ``-hash`` / ``_hash`` suffix.
4. **Value shapes** (:func:`looks_like_secret`), checked on **every** text
   value and CDATA section, so a secret in an element no rule knows is
   still caught: a PEM private key, an OpenVPN static / tls-crypt key, a
   DNSSEC private key file (``Private-key-format:`` / ``PrivateKey:``),
   crypt password hashes (``$1$``, ``$2y$``, ``$5$``, ``$6$``, ...), a
   password or a secret-named query parameter in a URL, a long hex run (256
   or more hex digits: key size), and base64 that decodes (or decodes and
   inflates) to a key or to a DER private key (PKCS#1, PKCS#8, SEC1).
5. **Base64 text** (:func:`looks_like_secret`): when a whole value is base64
   that decodes to text, the decoded text gets the shape checks above plus
   a check for secret words (``password``, ``secret``, ``token``, a name
   ending in ``pw`` or ``pass``, ...). On a hit, the whole value goes.

Once an element is secret, every value below it is secret too (so a whole
``<sshdata>`` block goes). Public certificates (``<crt>``), CSRs and SSH
public keys stay: their base64 decodes to a certificate or a public key,
not to a private key. A short list of names that only *look* secret
(:data:`_NOT_SECRET_NAMES`, for example the FRR ``community_set`` route-map
setting or the ``usepass`` checkbox) stays readable.

What pfSense never writes
=========================

pfSense writes ``config.xml`` with its own writer (``dump_xml_config`` in
``src/etc/inc/xmlparse.inc``): a leading ``<?xml version="1.0"?>``, then
elements, each value as **one** text or CDATA piece. It never writes
attributes, comments or other processing instructions. So:

* every attribute value and every comment is replaced, whatever it holds;
* the scan fails closed on a processing instruction other than a leading
  ``<?xml ...?>`` declaration, on text outside the root element, and on a
  value split into several text / CDATA pieces.

How the text is processed
=========================

The file is scanned as text with regular expressions. It is **never** given
to an XML parser, so no entity is ever expanded and no external resource is
ever loaded. The scan copies every byte it does not redact, so the output is
the input with only the secret values swapped -- tags, whitespace, CDATA
wrappers and entity references all stay byte for byte.

Fail closed
===========

The scan raises :exc:`ConfigRedactionError` when it cannot be sure it saw
every value: markup it does not recognise (for example a ``<!DOCTYPE``,
which pfSense never writes), a mismatched or unclosed element, elements
nested deeper than :data:`MAX_DEPTH`, one of the "never written" cases
above, or a key / hash shape that survives in the output. The error message
is a fixed reason plus a character offset; it never quotes the file. The
handler turns any exception into ``config_xml: null`` plus an error -- the
raw file is never returned.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from meho_backplane.connectors.pfsense.redaction_names import (
    SECRET_ELEMENT_NAMES,
    is_secret_name,
    is_secret_name_in,
    local_name,
)
from meho_backplane.connectors.pfsense.redaction_shapes import (
    CRYPT_HASH_RE,
    has_key_header,
    looks_like_secret,
)

__all__ = [
    "MAX_DEPTH",
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

#: Deepest element nesting the scan accepts. A real ``config.xml`` is about
#: ten levels deep; the limit keeps a hostile file from slowing the scan.
MAX_DEPTH: Final[int] = 64

#: The fixed reasons a scan can fail with. Messages never quote the file.
FailReason = Literal[
    "unrecognised markup",
    "processing instruction",
    "mismatched end tag",
    "elements nested too deep",
    "unclosed element at end of file",
    "text outside the root element",
    "value split into several pieces",
    "a secret shape survived the scan",
]


class ConfigRedactionError(ValueError):
    """The scan could not prove it removed the secrets (fail closed).

    The message is a fixed :data:`FailReason` plus, where known, the
    character offset. It never quotes the file, so it is safe to return to
    the caller and to log.
    """

    def __init__(self, reason: FailReason, offset: int | None = None) -> None:
        self.reason: FailReason = reason
        self.offset = offset
        super().__init__(reason if offset is None else f"{reason} at offset {offset}")


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

#: The only processing instruction allowed: a leading XML declaration with
#: plain values (pfSense writes ``<?xml version="1.0"?>``).
_XML_DECLARATION_RE: Final[re.Pattern[str]] = re.compile(
    r"<\?xml\s+version\s*=\s*([\"'])1\.[0-9]\1"
    r"(?:\s+encoding\s*=\s*([\"'])[A-Za-z][A-Za-z0-9._-]{0,39}\2)?"
    r"(?:\s+standalone\s*=\s*([\"'])(?:yes|no)\3)?"
    r"\s*\?>"
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
        #: Lowercased local names of the open elements, with a count each,
        #: kept up to date as elements open and close (for context rules).
        self.open_names: dict[str, int] = {}
        #: Non-blank text / CDATA pieces since the last tag.
        self.pieces = 0

    def replace(self, value: str) -> str:
        """Return :data:`REDACTED` for a non-blank *value* and count it."""
        if not value.strip():
            return value
        self.count += 1
        return _swap(value)

    def text(self, value: str, offset: int) -> str:
        """Check one text run or CDATA body; return it, or its redacted form."""
        if not value.strip():
            return value
        if not self.stack:
            raise ConfigRedactionError("text outside the root element", offset)
        self.pieces += 1
        if self.pieces > 1:
            raise ConfigRedactionError("value split into several pieces", offset)
        if self.stack[-1][1] or looks_like_secret(value):
            return self.replace(value)
        return value

    def run(self) -> str:
        xml = self.xml
        pos = 0
        while pos < len(xml):
            lt = xml.find("<", pos)
            if lt == -1:
                lt = len(xml)
            if lt > pos:
                self.out.append(self.text(xml[pos:lt], pos))
            if lt == len(xml):
                break
            match = _MARKUP_RE.match(xml, lt)
            if match is None:
                raise ConfigRedactionError("unrecognised markup", lt)
            self.markup(match)
            pos = match.end()
        if self.stack:
            raise ConfigRedactionError("unclosed element at end of file")
        return "".join(self.out)

    def markup(self, match: re.Match[str]) -> None:
        if (comment := match.group("comment")) is not None:
            # pfSense never writes comments: replace whatever one holds.
            self.out.append(f"<!--{self.replace(comment)}-->")
        elif (cdata := match.group("cdata")) is not None:
            self.out.append(f"<![CDATA[{self.text(cdata, match.start('cdata'))}]]>")
        elif match.group("pi") is not None:
            if match.start() != 0 or not _XML_DECLARATION_RE.fullmatch(match.group(0)):
                raise ConfigRedactionError("processing instruction", match.start())
            self.out.append(match.group(0))
        elif (end := match.group("end")) is not None:
            self.end_tag(end, match.start())
            self.out.append(match.group(0))
        else:
            self.start_tag(match)

    def start_tag(self, match: re.Match[str]) -> None:
        name = match.group("start")
        secret = (bool(self.stack) and self.stack[-1][1]) or is_secret_name_in(
            name, self.open_names
        )

        def _attr(attr: re.Match[str]) -> str:
            # pfSense never writes attributes: replace whatever one holds.
            new_value = self.replace(attr.group("value"))
            return f"{attr.group('lead')}{attr.group('quote')}{new_value}{attr.group('quote')}"

        attrs = _ATTR_RE.sub(_attr, match.group("attrs"))
        whole = match.group(0)
        offset = match.start()
        self.out.append(
            whole[: match.start("attrs") - offset] + attrs + whole[match.end("attrs") - offset :]
        )
        self.pieces = 0
        if match.group("empty"):
            return
        if len(self.stack) >= MAX_DEPTH:
            raise ConfigRedactionError("elements nested too deep", offset)
        self.stack.append((name, secret))
        local = local_name(name)
        self.open_names[local] = self.open_names.get(local, 0) + 1

    def end_tag(self, name: str, offset: int) -> None:
        if not self.stack or self.stack[-1][0] != name:
            raise ConfigRedactionError("mismatched end tag", offset)
        self.stack.pop()
        local = local_name(name)
        remaining = self.open_names[local] - 1
        if remaining:
            self.open_names[local] = remaining
        else:
            del self.open_names[local]
        self.pieces = 0


def redact_config_xml(xml: str) -> tuple[str, int]:
    """Return ``(cleaned_xml, redacted_count)`` for a pfSense ``config.xml``.

    The values of known secret fields and values with a known secret shape
    (see the module docstring) are replaced with :data:`REDACTED`, and so is
    every attribute value and comment; every other byte is copied unchanged.
    ``redacted_count`` is the number of values replaced (text runs, CDATA
    sections, attribute values and comments). Free-text fields can still
    hold secrets that someone typed in.

    Raises :exc:`ConfigRedactionError` when the scan cannot be sure it saw
    every value, or when a key / hash shape still appears in the output.
    The caller must then return no XML at all.
    """
    scan = _Scan(xml)
    redacted = scan.run()
    if has_key_header(redacted) or CRYPT_HASH_RE.search(redacted):
        raise ConfigRedactionError("a secret shape survived the scan")
    return redacted, scan.count
