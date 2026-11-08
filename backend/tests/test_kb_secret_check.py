# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The generic secret check for imported knowledge pages (#5728)."""

from __future__ import annotations

import pytest

from meho_backplane.kb.secret_check import SECRET_PATTERNS, find_secret
from meho_backplane.redaction.patterns import PATTERN_NAMES

MADE_UP = "planted-secret-Qw3rTy8Zx1"
# Built from parts, so a secret scanner on the repo does not flag the test.
_KEY_LABEL = "RSA PRIVATE" + " KEY"
PEM = f"-----BEGIN {_KEY_LABEL}-----\nbWFkZS11cC1rZXktYm9keQ==\n-----END {_KEY_LABEL}-----"
JWT = ".".join(["ey" + "JhbGciOiJub25lIn0", "ey" + "JzdWIiOiJtYWRlLXVwIn0", "bWFkZS11cA"])


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (f"password={MADE_UP}", "api_key"),
        (f"DB_PASSWORD: {MADE_UP}", "api_key"),
        (PEM, "pem_private_key"),
        (f"Authorization: Bearer {MADE_UP}", "authorization_header"),
        (f"curl -H 'X: Bearer {MADE_UP}'", "bearer_token"),
        (JWT, "jwt"),
        (f"https://store.example.com/file?sig={MADE_UP}", "url_secret"),
        ("root:$6$saltsalt$" + "a" * 40 + ":19000:0", "password_hash"),
    ],
)
def test_a_secret_shape_is_found_and_not_echoed(text: str, pattern: str) -> None:
    found = find_secret(f"intro line\n{text}\n")
    assert found is not None
    assert found.startswith(f"{pattern} on line 2")
    assert MADE_UP not in found


@pytest.mark.parametrize(
    "text",
    [
        "A plain page about snapshot limits in version 9.0.",
        "Set password: <password> in the form.",
        'password: "<password>"',
        "Send Bearer ${TOKEN}.",
        "api_key=${API_KEY}",
        "https://store.example.com/file?sig=${SIG}",
        'key "x" {\n  algorithm hmac-sha256;\n  secret "${SECRET}";\n};',
        "The host is host.example.com.",
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    ],
)
def test_text_without_a_secret_passes(text: str) -> None:
    assert find_secret(text) is None


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (f"password={MADE_UP}<x>", "api_key"),
        (f"password={MADE_UP}:${{TOKEN}}", "api_key"),
        (f"password=${{PREFIX}}{MADE_UP}", "api_key"),
        (f"Authorization: Bearer {MADE_UP}<suffix>", "authorization_header"),
        (f"Bearer ${{A}}{MADE_UP}${{B}}", "bearer_token"),
        (f"https://store.example.com/file?sig={MADE_UP}${{X}}", "url_secret"),
    ],
)
def test_a_value_with_a_placeholder_in_it_is_still_a_secret(text: str, pattern: str) -> None:
    found = find_secret(text)
    assert found == f"{pattern} on line 1"
    assert MADE_UP not in found


@pytest.mark.parametrize(
    "text",
    [
        PEM + " <placeholder>",
        PEM.split("-----END")[0] + "<placeholder>",
        PEM.split("-----END")[0] + "${KEY}",
        f"-----BEGIN {_KEY_LABEL}-----\n<key body>",
    ],
)
def test_a_private_key_block_is_never_a_placeholder(text: str) -> None:
    assert find_secret(text) == "pem_private_key on line 1"


def test_every_checked_pattern_exists_and_no_identifier_pattern_is_checked() -> None:
    assert set(SECRET_PATTERNS) <= set(PATTERN_NAMES)
    assert not {"uuid", "ipv4", "ipv6", "fqdn"} & set(SECRET_PATTERNS)
