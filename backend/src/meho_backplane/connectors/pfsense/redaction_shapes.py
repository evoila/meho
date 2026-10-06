# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Value-shape rules for the pfSense config secret removal (see :mod:`.redaction`).

What a secret value looks like, whatever its element is called: key
headers, crypt hashes, passwords in URLs, long hex runs, base64 that
decodes to a key, and base64 that decodes to text with secret words in it.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import zlib
from typing import Final

from meho_backplane.connectors.pfsense.redaction_names import (
    NAME_NOISE_RE,
    SECRET_NAME_ENDINGS,
    SECRET_NAME_PARTS,
)

__all__ = [
    "CRYPT_HASH_RE",
    "has_key_header",
    "looks_like_secret",
]


#: PEM private keys of every kind (``PRIVATE KEY``, ``RSA PRIVATE KEY``,
#: ``EC PRIVATE KEY``, ``ENCRYPTED PRIVATE KEY``, ``OPENSSH PRIVATE KEY``,
#: ``PGP PRIVATE KEY BLOCK``).
_PEM_PRIVATE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY", re.IGNORECASE
)

#: OpenVPN armours only key material: ``Static key V1`` (shared / tls-auth /
#: tls-crypt) and the ``tls-crypt-v2`` server and client keys.
_OPENVPN_KEY_RE: Final[re.Pattern[str]] = re.compile(r"-----BEGIN OpenVPN\b", re.IGNORECASE)

#: A BIND DNSSEC private key file (``K<zone>+<alg>+<id>.private``) has no PEM
#: armour; it starts with ``Private-key-format: v1.3`` and carries the key
#: on a ``PrivateKey:`` line.
_DNSSEC_PRIVATE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:Private-key-format|PrivateKey)[ \t]*:", re.IGNORECASE | re.MULTILINE
)

#: crypt(3) password hashes: MD5 ``$1$``, bcrypt ``$2a$``/``$2b$``/``$2x$``/
#: ``$2y$``, SHA-256 ``$5$``, SHA-512 ``$6$``, plus scrypt / yescrypt /
#: Apache MD5 / Argon2 / Sun MD5 / NetBSD SHA-1.
CRYPT_HASH_RE: Final[re.Pattern[str]] = re.compile(
    r"\$(?:1|2[abxy]|5|6|7|y|gy|apr1|argon2(?:id|i|d)|md5|sha1)\$"
)

#: A run of base64 long enough to carry a key header. Shorter runs cannot
#: decode to ``-----BEGIN ... PRIVATE KEY``. The look-behind makes every run
#: start at its first character, which keeps the search linear.
_BASE64_RUN_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: 256 or more hex digits in a row: the size of a key (an OpenVPN static key
#: body without its armour, or a hex dump of a key file). Checked on the raw
#: text only, so a list of numbers split by spaces is never joined into one.
_LONG_HEX_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{256,}")

#: A password in a URL: ``scheme://user:password@host``.
_URL_PASSWORD_RE: Final[re.Pattern[str]] = re.compile(r"://[^\s/?#@:]*:[^\s/?#@]+@")

#: A query parameter with a value (``?name=value``, ``&name=value``; in the
#: raw XML text ``&`` is written ``&amp;``, so ``;`` starts a name too).
_URL_PARAM_RE: Final[re.Pattern[str]] = re.compile(r"[?&;]([A-Za-z0-9_.\-]{1,64})=[^&;#\s]")

#: A query parameter is secret when its name (lowercased, ``-_.`` removed)
#: contains any of these (``token``, ``access_token``, ``key``, ``apikey``,
#: ``api_key``, ``password``, ``passwd``, ``pwd``, ``secret``, ``auth``).
_URL_SECRET_PARAM_WORDS: Final[tuple[str, ...]] = ("token", "key", "pass", "pwd", "secret", "auth")

#: A DER length: short form, or long form with one to three length bytes.
_DER_LENGTH: Final[bytes] = (
    rb"(?:[\x00-\x7f]|\x81[\x80-\xff]|\x82[\x01-\xff][\x00-\xff]|\x83[\x01-\xff][\x00-\xff]{2})"
)

#: The start of a DER private key, as a headerless base64 key body has it.
#: A certificate, a CSR or a public key starts differently.
_DER_PRIVATE_KEY_RE: Final[re.Pattern[bytes]] = re.compile(
    rb"\x30"
    + _DER_LENGTH
    + rb"(?:"
    # PKCS#1 RSA (and OpenSSL DSA): version 0, then the next INTEGER.
    + rb"\x02\x01\x00\x02"
    # PKCS#8 PrivateKeyInfo / OneAsymmetricKey: version 0 or 1, then the
    # AlgorithmIdentifier SEQUENCE, which starts with an OID.
    + rb"|\x02\x01[\x00\x01]\x30"
    + _DER_LENGTH
    + rb"\x06"
    # SEC1 EC private key: version 1, then the key OCTET STRING.
    + rb"|\x02\x01\x01\x04"
    # Encrypted PKCS#8: AlgorithmIdentifier with a PKCS#5 or PKCS#12 PBE OID.
    + rb"|\x30"
    + _DER_LENGTH
    + rb"\x06(?:\x09\x2a\x86\x48\x86\xf7\x0d\x01\x05|\x0a\x2a\x86\x48\x86\xf7\x0d\x01\x0c\x01)"
    + rb")",
    re.DOTALL,
)

#: Inflating a base64 value stops after this many bytes. The key headers
#: sit at the very start, and the cap keeps a compression bomb harmless. A
#: stream that has more to give than this is treated as secret: it cannot
#: be checked in full.
_INFLATE_LIMIT: Final[int] = 4096

#: Whole-value base64 that may decode to text: base64 letters and padding.
_BASE64_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]+={0,2}")
#: Shorter base64 values are not decoded as text (too short to hold much).
_MIN_BASE64_TEXT: Final[int] = 8
#: Control characters that tell binary data from text.
_CONTROL_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: A PEM block of any kind. Dropped from decoded text before the word check,
#: so the random letters of a public certificate cannot spell a word.
_PEM_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN[^\n]*?-----.*?(?:-----END[^\n]*?-----|\Z)", re.DOTALL
)

#: Secret words in decoded text (lowercased, ``-_.`` removed): the name
#: parts anywhere, the name endings at the end of a word, and a word ending
#: in ``pass`` (``requirepass``, ``auth_pass``) but not ``bypass`` or the
#: bare word ``pass`` (a firewall rule keyword).
_SECRET_WORD_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(part) for part in SECRET_NAME_PARTS)
    + r"|(?:"
    + "|".join(re.escape(ending) for ending in SECRET_NAME_ENDINGS)
    + r")(?![a-z0-9])"
    + r"|(?<=[a-z0-9])(?<!by)pass(?![a-z0-9])"
)


def has_key_header(text: str) -> bool:
    return bool(
        _PEM_PRIVATE_KEY_RE.search(text)
        or _OPENVPN_KEY_RE.search(text)
        or _DNSSEC_PRIVATE_KEY_RE.search(text)
    )


def _bytes_hide_key(data: bytes) -> bool:
    """``True`` when decoded *data* is (or holds) key material."""
    return bool(_DER_PRIVATE_KEY_RE.match(data)) or has_key_header(data.decode("latin-1"))


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


def _inflate_hides_key(data: bytes) -> bool:
    """``True`` when *data* inflates to key material, or to more than we check.

    Tries raw deflate (PHP ``gzdeflate``, which pfSense uses for the
    ``sshdata`` key backups) and zlib / gzip framing.
    """
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS | 32):
        inflater = zlib.decompressobj(wbits)
        try:
            inflated = inflater.decompress(data, _INFLATE_LIMIT)
        except zlib.error:
            continue
        if _bytes_hide_key(inflated):
            return True
        if len(inflated) >= _INFLATE_LIMIT and not inflater.eof:
            return True  # too large to check in full: treat it as secret
    return False


def _base64_hides_key(run: str) -> bool:
    """``True`` when base64 *run* decodes (or inflates) to key material.

    Text glued in front of a base64 value shifts its 4-character groups, so
    all four alignments are decoded. Only the aligned start is inflated.
    """
    for shift in range(4):
        data = _b64_decode_prefix(run[shift:])
        if data is None:
            continue
        if _bytes_hide_key(data):
            return True
        if shift == 0 and _inflate_hides_key(data):
            return True
    return False


def _url_has_credentials(text: str) -> bool:
    """``True`` for ``scheme://user:password@host`` or a secret-named query parameter."""
    if _URL_PASSWORD_RE.search(text):
        return True
    for match in _URL_PARAM_RE.finditer(text):
        name = NAME_NOISE_RE.sub("", match.group(1).lower())
        if any(word in name for word in _URL_SECRET_PARAM_WORDS):
            return True
    return False


def _has_secret_shape(text: str) -> bool:
    """The shape checks on one form of a value (no base64-to-text step)."""
    if has_key_header(text) or CRYPT_HASH_RE.search(text):
        return True
    if _LONG_HEX_RE.search(text) or _url_has_credentials(text):
        return True
    runs = {match.group(0) for match in _BASE64_RUN_RE.finditer(text)}
    # The whitespace-free form catches base64 wrapped at any line width.
    compact = _WHITESPACE_RE.sub("", text)
    if compact != text:
        runs.update(match.group(0) for match in _BASE64_RUN_RE.finditer(compact))
    return any(_base64_hides_key(run) for run in runs)


def _base64_text(value: str) -> str | None:
    """The text a whole base64 *value* decodes to, or ``None`` (not base64, or binary)."""
    compact = _WHITESPACE_RE.sub("", value)
    if (
        len(compact) < _MIN_BASE64_TEXT
        or len(compact) % 4
        or not _BASE64_VALUE_RE.fullmatch(compact)
    ):
        return None
    try:
        text = base64.b64decode(compact, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return None if _CONTROL_CHAR_RE.search(text) else text


def _has_secret_word(text: str) -> bool:
    """``True`` when free *text* names a secret (``password``, ``token``, ``x_pw``, ...).

    PEM blocks and long base64 runs are dropped first, so the random letters
    inside a public certificate or SSH key cannot spell a word by chance.
    """
    words = _BASE64_RUN_RE.sub(" ", _PEM_BLOCK_RE.sub(" ", text))
    return bool(_SECRET_WORD_RE.search(NAME_NOISE_RE.sub("", words.lower())))


def looks_like_secret(value: str) -> bool:
    """Return ``True`` when *value* has the shape of a secret, whatever its name.

    Checks the raw text and its entity-decoded form (pfSense writes values
    through ``htmlentities``): a PEM / OpenVPN / DNSSEC private key, a crypt
    hash, a password in a URL, a long hex run, or base64 that decodes (or
    decodes and inflates) to a key. When the whole value is base64 that
    decodes to text, the decoded text gets the same shape checks plus the
    secret-word check.
    """
    for text in dict.fromkeys((value, html.unescape(value))):
        if _has_secret_shape(text):
            return True
        decoded = _base64_text(text)
        if decoded is not None and (_has_secret_shape(decoded) or _has_secret_word(decoded)):
            return True
    return False
