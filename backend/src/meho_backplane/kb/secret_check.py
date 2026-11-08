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
so a page can show what a setting looks like.
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

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"<[^<>\s]+>|\$\{[^{}\s]+\}")
_VALUE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\s=:]+")


def find_secret(text: str) -> str | None:
    """Return where *text* holds what looks like a secret, or ``None``.

    The answer names the pattern and the line, for example
    ``"api_key on line 12"``. It never holds the secret itself.
    """
    for name in SECRET_PATTERNS:
        for start, end in secret_spans(get_pattern(name), text):
            if _is_placeholder(text[start:end]):
                continue
            line = text.count("\n", 0, start) + 1
            return f"{name} on line {line}"
    return None


def _is_placeholder(found: str) -> bool:
    """Return whether the value part of *found* is only a placeholder.

    The value is the last part after a space, ``=`` or ``:``, without
    quotes and without a full stop or comma after it.
    ``password: <password>`` and ``Bearer ${TOKEN}.`` are placeholders;
    ``password=abc<x>`` is not.
    """
    value = _VALUE_SPLIT.split(found.strip())[-1].strip("\"'.,;")
    return _PLACEHOLDER.fullmatch(value) is not None
