# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The generic secret check for knowledge pages that come in from outside.

A page that holds a password, a token or a private key must not be
stored. :func:`find_secret` looks for the common secret shapes of the
redaction engine (:mod:`meho_backplane.redaction.patterns`): login
headers, bearer tokens, JWTs, labelled keys and passwords, private key
blocks, password hashes, Ceph keys, BIND key secrets and secrets in
links.

The check is generic on purpose. It holds no list of customer names,
host names or addresses. That list stays in the repo the pages come
from, and that repo's own CI job checks it before it sends the pages
(#5728).

A placeholder such as ``<password>`` or ``${TOKEN}`` is not a secret,
so a page can show what a setting looks like. A value is exempt only
when the whole value is one placeholder, with at most quotes around it
and punctuation after it: ``password: "<password>",`` is no secret,
``password=made-up-value:${TOKEN}``, ``password: made-up-value:${TOKEN}``
and ``password=Q7<k9Tz2LmN>p4`` are. Only the credential label or the
header name in front of the value comes off before the check, never a
part of the value. A private key block is never a placeholder.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.redaction.patterns import get_pattern, secret_spans

__all__ = ["SECRET_PATTERNS", "find_secret"]

#: The credential patterns the check runs, in this order. The identifier
#: patterns (``uuid``, ``ipv4``, ``ipv6``, ``fqdn``) and ``kubeconfig``
#: are left out: product pages name versions, example addresses and
#: config shapes all the time.
SECRET_PATTERNS: Final[tuple[str, ...]] = (
    "pem_private_key",
    "authorization_header",
    "bearer_token",
    "jwt",
    "url_secret",
    "api_key",
    "password_hash",
    "ceph_key",
    "bind_key_secret",
)

#: A value that is one placeholder, with at most quotes around it and
#: punctuation after it.
_PLACEHOLDER_VALUE: Final[re.Pattern[str]] = re.compile(
    r"""['"]?(?:<[^<>\s]+>|\$\{[^{}\s]+\})['"]?[.,;:!?)\]}]*"""
)
#: What a regex pattern reports in front of the value: its credential
#: label or header name and the separator after it. Each regex starts
#: its match with exactly this, so only the label comes off, never a
#: part of the value. A pattern not listed here reports the bare value.
_LABEL_PREFIX: Final[dict[str, re.Pattern[str]]] = {
    "authorization_header": re.compile(r"Authorization\s*:\s*[A-Za-z]+\s+", re.IGNORECASE),
    "bearer_token": re.compile(r"Bearer\s+", re.IGNORECASE),
    "api_key": re.compile(r"[A-Za-z0-9_-]+\s*[=:]\s*"),
    "ceph_key": re.compile(r"key['\"]?\s*[=:]\s*", re.IGNORECASE),
}
#: A key block cut off before its ``END`` line runs to the end of the
#: text, so its last word says nothing about the block. It is never exempt.
_NEVER_A_PLACEHOLDER: Final[frozenset[str]] = frozenset({"pem_private_key"})


def find_secret(text: str) -> str | None:
    """Return where *text* holds what looks like a secret, or ``None``.

    The answer names the pattern and the line, for example
    ``"api_key on line 12"``. It never holds the secret itself.
    """
    for name in SECRET_PATTERNS:
        pattern = get_pattern(name)
        for start, end in secret_spans(pattern, text):
            if name not in _NEVER_A_PLACEHOLDER and _is_placeholder(name, text[start:end]):
                continue
            line = text.count("\n", 0, start) + 1
            return f"{name} on line {line}"
    return None


def _is_placeholder(name: str, found: str) -> bool:
    """Return whether the value in *found* is one placeholder and nothing more.

    A regex reports the label and the value, as in ``password: "<password>"``
    or ``Authorization: Bearer <token>``. Its entry in :data:`_LABEL_PREFIX`
    takes the label off, and nothing else: in ``password: made-up:${TOKEN}``
    the value is ``made-up:${TOKEN}``, not ``${TOKEN}``. A pattern without
    an entry (a JWT, a password hash, a :class:`SecretFinder`) reports
    only the value. Either way the whole value must be one placeholder:
    ``password=Q7<k9Tz2LmN>p4`` and ``password=${PREFIX}made-up-value``
    keep text of their own next to the placeholder, so they stay secrets.
    """
    prefix = _LABEL_PREFIX.get(name)
    if prefix is not None:
        label = prefix.match(found)
        if label is None:
            return False
        found = found[label.end() :]
    return _PLACEHOLDER_VALUE.fullmatch(found) is not None
