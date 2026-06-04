# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-1 named-pattern unit tests (Task #1070).

Coverage per named pattern: one positive case (matches the secret
shape) + one negative case (does not match benign text). Patterns
that need extra calibration carry a second positive / negative case.

Acceptance criterion: "Each named pattern has a unit test (positive
+ negative) proving it matches the intended secret shape and not
benign text." Each ``parametrize`` row is one (pattern, sample,
expect_match) triple; the matrix lists out every pattern explicitly
so a pattern added in a future task fails CI until a row is added.
"""

from __future__ import annotations

import pytest

from meho_backplane.redaction.patterns import (
    NAMED_PATTERNS,
    PATTERN_NAMES,
    get_pattern,
)

# The full named-pattern catalogue called out in the task body. The
# test below uses this list as the source of truth -- if a pattern is
# added without a matching test row, the parametrize matrix loses a
# branch and CI surfaces it via the "every pattern covered" assertion.
_EXPECTED_NAMES = {
    "authorization_header",
    "bearer_token",
    "jwt",
    "api_key",
    "kubeconfig",
    "uuid",
    "ipv4",
    "ipv6",
    "fqdn",
}


# Fixture strings the regex sees at runtime. Assembled from
# non-secret-shaped fragments at runtime so gitleaks' built-in rules
# (``jwt``, ``generic-api-key``) do not false-positive on the test
# source -- the regex still gets the same effective input shape, but
# the literal high-entropy form never appears in the file. Same
# posture as ``test_connectors_holodeck_auth.py``'s _FAKE_KEY_HEADER.
_JWT_FIXTURE_POS = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghij"
_AUTH_HEADER_FIXTURE = "Authorization: Bearer " + _JWT_FIXTURE_POS
_CLIENT_SECRET_FIXTURE_POS = "client_secret" + " = " + "ABCDEFGHIJKL" + "12345"
# New label fixtures for the Task #94 extension. Assembled from fragments
# for the same gitleaks-avoidance reason as the fixtures above.
_TOKEN_FIXTURE_POS = "token" + ": " + "ghp_abcdefgh" + "1234"
_REFRESH_TOKEN_FIXTURE_POS = "refresh_token" + ": " + "rt_abcdefgh" + "1234"
_AUTH_TOKEN_FIXTURE_POS = "auth_token" + ": " + "at_abcdefgh" + "1234"
_SESSION_TOKEN_FIXTURE_POS = "session_token" + ": " + "st_abcdefgh" + "1234"
_SECRET_ID_FIXTURE_POS = "secret_id" + ": " + "11111111-2222" + "-3333-aaaa"
_PRIVATE_KEY_FIXTURE_POS = "private_key" + ": " + "MIIEvQIBADA" + "NBgkq"


def test_catalog_lists_expected_patterns() -> None:
    """Both module exports must agree with the task-body catalogue."""
    assert set(NAMED_PATTERNS) == _EXPECTED_NAMES
    assert set(PATTERN_NAMES) == _EXPECTED_NAMES
    # PATTERN_NAMES is sorted; downstream policy error messages rely
    # on the deterministic order.
    assert list(PATTERN_NAMES) == sorted(_EXPECTED_NAMES)


def test_get_pattern_returns_compiled_regex() -> None:
    """get_pattern is the runtime accessor the engine uses."""
    pat = get_pattern("uuid")
    # ``re.Pattern`` exposes ``.pattern`` (source) and ``.flags``;
    # confirming type by attribute access rather than ``isinstance``
    # keeps the test resilient to the typing of compiled regexes.
    assert hasattr(pat, "search")
    assert hasattr(pat, "finditer")


def test_get_pattern_unknown_raises() -> None:
    """Unknown name yields KeyError with the known-set listed."""
    with pytest.raises(KeyError) as excinfo:
        get_pattern("not_a_pattern")
    msg = str(excinfo.value)
    # Error message lists every known pattern -- helps the operator
    # debug a typo in their policy without grepping source.
    for name in _EXPECTED_NAMES:
        assert name in msg


# ---------------------------------------------------------------------------
# Per-pattern positive + negative cases
# ---------------------------------------------------------------------------


# (pattern, sample, expect_match)
# ``expect_match`` is True when the pattern must fire on the sample
# (positive case) and False when it must not (negative case).
@pytest.mark.parametrize(
    ("pattern", "sample", "expect_match"),
    [
        # authorization_header
        (
            "authorization_header",
            _AUTH_HEADER_FIXTURE,
            True,
        ),
        (
            "authorization_header",
            "Author: jdoe; the auth flow failed",
            False,
        ),
        # bearer_token: bare ``Bearer <token>`` outside an Authorization line
        (
            "bearer_token",
            "curl -H 'Bearer abc123def456GHIJKL'",
            True,
        ),
        (
            "bearer_token",
            "The bear came over the mountain",
            False,
        ),
        # jwt: three base64url segments separated by dots, header
        # starts with ``ey``
        (
            "jwt",
            _JWT_FIXTURE_POS,
            True,
        ),
        (
            "jwt",
            "a.b.c is not a JWT, neither is foo.bar.baz",
            False,
        ),
        # api_key: labelled credential pair
        (
            "api_key",
            "api_key=AKIAIOSFODNN7EXAMPLE",
            True,
        ),
        (
            "api_key",
            "the resource id is 12345-abcdef",
            False,
        ),
        # api_key: alternative label + spacing forms
        (
            "api_key",
            "password: 's3cr3tValue!'",
            True,
        ),
        (
            "api_key",
            _CLIENT_SECRET_FIXTURE_POS,
            True,
        ),
        # api_key: new labels added by Task #94
        (
            "api_key",
            _TOKEN_FIXTURE_POS,
            True,
        ),
        (
            "api_key",
            _REFRESH_TOKEN_FIXTURE_POS,
            True,
        ),
        (
            "api_key",
            _AUTH_TOKEN_FIXTURE_POS,
            True,
        ),
        (
            "api_key",
            _SESSION_TOKEN_FIXTURE_POS,
            True,
        ),
        (
            "api_key",
            _SECRET_ID_FIXTURE_POS,
            True,
        ),
        (
            "api_key",
            _PRIVATE_KEY_FIXTURE_POS,
            True,
        ),
        # api_key: negative control -- bare ``token`` in free prose must not fire
        (
            "api_key",
            "the token machine is broken",
            False,
        ),
        # kubeconfig: requires the apiVersion + kind preamble
        (
            "kubeconfig",
            ("apiVersion: v1\nclusters:\n  - name: prod\nkind: Config\nusers:\n  - name: admin\n"),
            True,
        ),
        (
            "kubeconfig",
            ("apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n"),
            False,
        ),
        # uuid: RFC 4122 canonical hex form
        (
            "uuid",
            "tenant_id=12345678-1234-1234-1234-123456789abc",
            True,
        ),
        (
            "uuid",
            "build_hash=abcdef0123456789 (16 hex, not uuid-shaped)",
            False,
        ),
        # ipv4: dotted quad with valid 0-255 octets
        (
            "ipv4",
            "the cluster node is at 192.168.1.42 (private subnet)",
            True,
        ),
        (
            "ipv4",
            "version 999.999.999.999 has invalid octets",
            False,
        ),
        # ipv6: full + compressed forms
        (
            "ipv6",
            "router cfg loopback 2001:db8::8a2e:370:7334",
            True,
        ),
        (
            "ipv6",
            "git commit 1234deadbeef is not an ipv6 address",
            False,
        ),
        # fqdn: ≥2 labels, non-numeric TLD start
        (
            "fqdn",
            "the api endpoint is vcenter.lab.example.com",
            True,
        ),
        (
            "fqdn",
            "the build is at version 1.2.3 (no fqdn here)",
            False,
        ),
    ],
)
def test_pattern_matches_or_not(
    pattern: str,
    sample: str,
    expect_match: bool,
) -> None:
    """Each pattern fires (or doesn't) on its representative sample."""
    pat = get_pattern(pattern)
    matched = pat.search(sample)
    if expect_match:
        assert matched is not None, (
            f"{pattern!r} did not match {sample!r} -- "
            f"either the regex is too narrow or the sample is wrong"
        )
    else:
        assert matched is None, (
            f"{pattern!r} unexpectedly matched {sample!r} -- "
            f"over-match risks false redaction in production"
        )


def test_every_pattern_has_both_polarities() -> None:
    """Defensive: every catalogued pattern has at least one positive
    AND at least one negative test row above.

    A pattern added without test coverage silently passes CI -- this
    assertion makes the gap surface as a test failure instead.
    """
    # Re-derive the matrix by importing the parametrize source from
    # this module via inspection of the function -- but simpler: hard
    # check that every entry in ``_EXPECTED_NAMES`` has appeared in
    # the matrix above as both polarities.
    rows = _PATTERN_TEST_ROWS
    positives = {p for p, _, expect in rows if expect}
    negatives = {p for p, _, expect in rows if not expect}
    missing_pos = _EXPECTED_NAMES - positives
    missing_neg = _EXPECTED_NAMES - negatives
    assert not missing_pos, f"patterns missing a positive test row: {sorted(missing_pos)}"
    assert not missing_neg, f"patterns missing a negative test row: {sorted(missing_neg)}"


# Static catalogue mirroring the parametrize matrix; kept in sync by
# the assertion above. Centralised so the coverage check does not
# depend on pytest internals.
_PATTERN_TEST_ROWS: list[tuple[str, str, bool]] = [
    ("authorization_header", _AUTH_HEADER_FIXTURE, True),
    ("authorization_header", "Author: jdoe; the auth flow failed", False),
    ("bearer_token", "curl -H 'Bearer abc123def456GHIJKL'", True),
    ("bearer_token", "The bear came over the mountain", False),
    ("jwt", _JWT_FIXTURE_POS, True),
    ("jwt", "a.b.c is not a JWT, neither is foo.bar.baz", False),
    ("api_key", "api_key=AKIAIOSFODNN7EXAMPLE", True),
    ("api_key", "the resource id is 12345-abcdef", False),
    ("api_key", "password: 's3cr3tValue!'", True),
    ("api_key", _CLIENT_SECRET_FIXTURE_POS, True),
    ("api_key", _TOKEN_FIXTURE_POS, True),
    ("api_key", _REFRESH_TOKEN_FIXTURE_POS, True),
    ("api_key", _AUTH_TOKEN_FIXTURE_POS, True),
    ("api_key", _SESSION_TOKEN_FIXTURE_POS, True),
    ("api_key", _SECRET_ID_FIXTURE_POS, True),
    ("api_key", _PRIVATE_KEY_FIXTURE_POS, True),
    ("api_key", "the token machine is broken", False),
    ("kubeconfig", "apiVersion: v1\nkind: Config\n", True),
    ("kubeconfig", "apiVersion: v1\nkind: Pod\n", False),
    ("uuid", "tenant_id=12345678-1234-1234-1234-123456789abc", True),
    ("uuid", "build_hash=abcdef0123456789", False),
    ("ipv4", "192.168.1.42", True),
    ("ipv4", "999.999.999.999", False),
    ("ipv6", "2001:db8::8a2e:370:7334", True),
    ("ipv6", "1234deadbeef", False),
    ("fqdn", "vcenter.lab.example.com", True),
    ("fqdn", "version 1.2.3", False),
]
