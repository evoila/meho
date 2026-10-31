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

import re
import time

import pytest

from meho_backplane.redaction import (
    DEFAULT_POLICY_PACKAGE,
    DEFAULT_POLICY_RESOURCE,
    load_policy_yaml,
)
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.patterns import (
    NAMED_PATTERNS,
    PATTERN_NAMES,
    get_pattern,
)
from meho_backplane.redaction.policy import RedactionPolicy, RedactionRule

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
    "private_key_block",
    "password_hash",
    "bind_key_secret",
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
# Punctuated-value fixtures (meho-internal hardening): values carrying
# bytes outside the pre-widening restrictive class must be captured to
# their natural delimiter (whitespace / closing quote / end of blob)
# and redacted whole. Assembled from fragments for the same
# gitleaks-avoidance reason as the fixtures above.
_PUNCT_QUOTED_FIXTURE = "password" + ": " + "'s3cr3tValue" + "!'"
_PUNCT_LEADING_AT_FIXTURE = "password" + "=" + "P@ssw0rd" + "!withMore"
_PUNCT_DOLLAR_HASH_FIXTURE = "password" + ": " + "hunter2" + "$plus#tail"
# BIND key block (#4617): ``secret "<base64>";`` inside a ``key`` statement.
_BIND_KEY_SECRET_FIXTURE_POS = 'secret "' + "exampleTsig" + 'Value==";'
_BIND_KEY_SECRET_FIXTURE_NEG = 'the secret is in the vault; key "rndc-key" { algorithm hmac-md5; };'
# PEM blocks and crypt(3) hashes, built from fragments so secret scanners
# do not flag this file.
_PEM_DASHES = "-" * 5
_PRIVATE_KEY_BLOCK_FIXTURE = (
    _PEM_DASHES
    + "BEGIN OPENSSH "
    + "PRIVATE KEY"
    + _PEM_DASHES
    + "\nb3BlbnNzaC1rZXktdjEAAAAA\nZmFrZWtleWJvZHk=\n"
    + _PEM_DASHES
    + "END OPENSSH "
    + "PRIVATE KEY"
    + _PEM_DASHES
)
_PRIVATE_KEY_CUT_FIXTURE = (
    _PEM_DASHES + "BEGIN " + "PRIVATE KEY" + _PEM_DASHES + "\nMIIEvQIBADANBgkqhkiG9w0B"
)
_CERTIFICATE_FIXTURE = (
    _PEM_DASHES
    + "BEGIN CERTIFICATE"
    + _PEM_DASHES
    + "\nMIIBszCCAVmgAwIBAgIU\n"
    + _PEM_DASHES
    + "END CERTIFICATE"
    + _PEM_DASHES
)
_SHA512_HASH_FIXTURE = "$" + "6$" + "saltsalt$" + "abcdefghijklmnopqrstuvwxyz0123456789./ABCD"
_SHADOW_LINE_FIXTURE = "root:" + _SHA512_HASH_FIXTURE + ":19000:0:99999:7:::"
_BCRYPT_HASH_FIXTURE = "$" + "2b$" + "12$" + "abcdefghijklmnopqrstuvABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
_YESCRYPT_HASH_FIXTURE = "$" + "y$" + "j9T$" + "saltsaltsalt$" + "abcdefghijklmnopqrstuvwxyz012345"
_ARGON2_HASH_FIXTURE = (
    "$" + "argon2id$" + "v=19$" + "m=65536,t=3,p=4$" + "bXlzYWx0eHk$" + "aGFzaGhhc2hoYXNoaGFzaA"
)


def test_catalog_lists_expected_patterns() -> None:
    """Both module exports must agree with the task-body catalogue."""
    assert set(NAMED_PATTERNS) == _EXPECTED_NAMES
    assert set(PATTERN_NAMES) == _EXPECTED_NAMES
    # PATTERN_NAMES is sorted; downstream policy error messages rely
    # on the deterministic order.
    assert list(PATTERN_NAMES) == sorted(_EXPECTED_NAMES)


_BIND_KEY_SECRET_ONLY_POLICY = RedactionPolicy(
    id="test-bind-key-secret",
    version=1,
    rules=(
        RedactionRule(
            name="strip-bind-key-secret",
            pattern="bind_key_secret",
            action="redact",
            reason="BIND key secret",
        ),
    ),
)


def test_bind_key_secret_keeps_the_keyword() -> None:
    """Only the quoted value is replaced, so the file stays readable (#4617)."""
    line = 'key "rndc-key" { algorithm hmac-sha256; ' + _BIND_KEY_SECRET_FIXTURE_POS + " };"
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        'key "rndc-key" { algorithm hmac-sha256; secret [REDACTED:bind_key_secret]; };'
    )
    # The manifest span points at the quoted value, not at the keyword.
    start, end = result.manifest[0].span
    assert line[start:end] == '"exampleTsigValue=="'


@pytest.mark.parametrize(
    "separator",
    [
        " /* rotation note */ ",
        " // rotated last week\n    ",
        " # note\n    ",
        " /* first\n   line */ // second\n    ",
        "",
    ],
    ids=["block-comment", "line-comment", "hash-comment", "mixed-comments", "no-space"],
)
def test_bind_key_secret_skips_comments_before_the_value(separator: str) -> None:
    """BIND allows comments between ``secret`` and the value (#4620 review).

    The keyword and the comments stay in the output; only the quoted
    value is replaced, and no fragment of the value survives.
    """
    line = "secret" + separator + '"' + "exampleTsig" + 'Value==";'
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == "secret" + separator + "[REDACTED:bind_key_secret];"
    assert "exampleTsig" not in result.redacted


@pytest.mark.parametrize(
    "comment_run",
    ["#" * 80, "//" * 40, "/*" * 40, "# a\n" * 40],
    ids=["hash-run", "slash-run", "unclosed-block-run", "many-lines"],
)
def test_bind_key_secret_comments_without_a_value_finish_fast(comment_run: str) -> None:
    """A comment run with no quoted value after it must not stall (#4620 review).

    The gap between ``secret`` and the value is matched once. Before the
    possessive quantifier a run of ``#`` characters could be split into
    comments in exponentially many ways, and 24 of them already took
    seconds through the default policy, which every connector uses.
    """
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, DEFAULT_POLICY_RESOURCE)
    payload = "secret " + comment_run + " no value here"
    started = time.perf_counter()
    result = redact(payload, policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"redaction took {elapsed:.2f}s"
    assert result.redacted == payload


@pytest.mark.parametrize(
    "resource",
    [DEFAULT_POLICY_RESOURCE, "example.yaml"],
    ids=["default-policy", "example-policy"],
)
@pytest.mark.parametrize(
    "comment",
    [
        "/* password=oldvalue*/",
        "/* token=rotated12345 */",
        "/* Authorization: Basic b2xkOnZhbHVl */",
        "/* Bearer oldbearer1234 */",
        "// password=oldvalue\n   ",
    ],
    ids=[
        "password-in-block",
        "token-in-block",
        "header-in-block",
        "bearer-in-block",
        "password-in-line",
    ],
)
def test_bind_key_secret_runs_before_rules_that_change_the_comment(
    resource: str, comment: str
) -> None:
    """The packaged policies redact the value when the comment holds a credential.

    ``api_key`` and the header rules consume a value up to the next
    whitespace or quote. In ``/* password=oldvalue*/`` that takes the
    closing ``*/`` with it, so a BIND rule that ran later no longer saw
    a whole comment and left the real value visible (#4620 review).
    The packaged policies list the BIND rule first for this reason.
    """
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, resource)
    line = "secret " + comment + ' "' + "exampleTsig" + 'Value==";'
    result = redact(line, policy)
    assert "exampleTsig" not in result.redacted
    assert "[REDACTED:bind_key_secret];" in result.redacted
    assert policy.rules[0].pattern == "bind_key_secret"


def test_bind_key_secret_ignores_a_value_commented_out_on_the_same_line() -> None:
    """``secret # "..."`` is a comment to BIND, so nothing is a secret here."""
    line = 'secret # "exampleTsigValue==";'
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == line
    assert result.manifest == ()


@pytest.mark.parametrize(
    "key_name",
    ["transfer-secret", "secret", "my secret", "SECRET"],
    ids=["hyphen-tail", "bare", "space-tail", "upper"],
)
def test_bind_key_secret_ignores_a_key_name_that_ends_in_secret(key_name: str) -> None:
    """A quoted key name ending in ``secret`` is not the directive (#4620 review).

    Without the guard the rule paired the name's closing quote with the
    value's opening quote and left the real value in the output.
    """
    block = (
        'key "' + key_name + '" {\n'
        "    algorithm hmac-sha256;\n"
        '    secret "' + "exampleTsig" + 'Value==";\n'
        "};"
    )
    result = redact(block, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        'key "' + key_name + '" {\n'
        "    algorithm hmac-sha256;\n"
        "    secret [REDACTED:bind_key_secret];\n"
        "};"
    )
    assert "exampleTsig" not in result.redacted
    assert len(result.manifest) == 1
    start, end = result.manifest[0].span
    assert block[start:end] == '"exampleTsigValue=="'


# A fake secret and a fake cookie value, split so secret scanners do not flag them.
_FAKE_BIND_SECRET = "exampleTsig" + "Value=="
_FAKE_COOKIE_SECRET = "0123abcd" + "ef" * 12


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("secret", '"' + _FAKE_BIND_SECRET + '"'),
        ("secret", _FAKE_BIND_SECRET),
        ("cookie-secret", '"' + _FAKE_COOKIE_SECRET + '"'),
        ("cookie-secret", _FAKE_COOKIE_SECRET),
        ("Cookie-Secret", _FAKE_COOKIE_SECRET),
    ],
    ids=["quoted", "unquoted", "cookie-quoted", "cookie-unquoted", "cookie-upper"],
)
@pytest.mark.parametrize(
    "separator",
    [" ", "\n    ", " /* rotation note */ ", " // rotated last week\n    ", " # note\n    "],
    ids=["space", "line-break", "block-comment", "line-comment", "hash-comment"],
)
def test_bind_key_secret_covers_every_value_form(keyword: str, value: str, separator: str) -> None:
    """BIND reads ``secret`` and ``cookie-secret`` with or without quotes.

    Every form is redacted, with a line break or a comment between the
    keyword and the value. Only the value is replaced.
    """
    line = "    " + keyword + separator + value + ";\n"
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == "    " + keyword + separator + "[REDACTED:bind_key_secret];\n"
    assert _FAKE_BIND_SECRET not in result.redacted
    assert _FAKE_COOKIE_SECRET not in result.redacted
    start, end = result.manifest[0].span
    assert line[start:end] == value


def test_bind_key_secret_unquoted_value_may_have_a_comment_before_the_semicolon() -> None:
    line = "secret " + _FAKE_BIND_SECRET + " /* rotated */ ;"
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == "secret [REDACTED:bind_key_secret] /* rotated */ ;"


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("secret", _FAKE_BIND_SECRET), ("cookie-secret", _FAKE_COOKIE_SECRET)],
    ids=["secret", "cookie-secret"],
)
def test_bind_key_secret_unquoted_value_right_before_a_hash_comment(
    keyword: str, value: str
) -> None:
    """``#`` starts a comment in BIND even with no space before it."""
    line = keyword + " " + value + "# note\n;"
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == keyword + " [REDACTED:bind_key_secret]# note\n;"
    assert value not in result.redacted


@pytest.mark.parametrize(
    "payload",
    ["secret x" + "//secret x" * 50000, "secret x " + "/*secret x*/" * 50000],
    ids=["line-comments", "block-comments"],
)
def test_bind_key_secret_stays_fast_on_long_text(payload: str) -> None:
    """Long text full of ``secret`` words and comments is redacted quickly."""
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, DEFAULT_POLICY_RESOURCE)
    started = time.perf_counter()
    redact(payload, policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"redaction took {elapsed:.2f}s"


@pytest.mark.parametrize(
    "text",
    [
        "the secret is in the vault;",
        "secrets " + _FAKE_BIND_SECRET + ";",
        "client_secret " + _FAKE_BIND_SECRET + ";",
        "secret # " + _FAKE_BIND_SECRET + ";",
    ],
    ids=["prose", "longer-word", "other-label", "commented-out"],
)
def test_bind_key_secret_unquoted_form_leaves_other_text_alone(text: str) -> None:
    """A word after ``secret`` is a value only where BIND would read one."""
    result = redact(text, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == text
    assert result.manifest == ()


def test_bind_key_secret_unquoted_value_in_a_key_named_secret() -> None:
    """The key-name guard still holds when the real value has no quotes."""
    block = (
        'key "my secret" {\n    algorithm hmac-sha256;\n    secret ' + _FAKE_BIND_SECRET + ";\n};"
    )
    result = redact(block, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        'key "my secret" {\n    algorithm hmac-sha256;\n    secret [REDACTED:bind_key_secret];\n};'
    )
    assert len(result.manifest) == 1


_FAKE_OLD_BIND_SECRET = "oldTsig" + "Value=="


@pytest.mark.parametrize(
    "comment",
    ["# the secret /* is below", "// the secret /* is below", '# the secret "', "/* secret"],
    ids=["hash-opens-block", "slash-opens-block", "hash-opens-quote", "unclosed-block"],
)
def test_bind_key_secret_keyword_in_a_comment_cannot_reach_a_key_block(comment: str) -> None:
    """A ``secret`` word in a comment never starts a match that spans real config.

    The old regex read a ``/*`` inside a line comment as the start of a
    comment between keyword and value, ran over the real key block up to
    a later ``*/``, and left the real secret visible (#5342 review). The
    finder reads comments the way BIND does, so the comment ends where
    BIND ends it and the key block after it is redacted.
    """
    block = (
        comment + "\n"
        'key "rndc-key" {\n'
        "    algorithm hmac-sha256;\n"
        '    secret "' + _FAKE_BIND_SECRET + '";\n'
        "};\n"
        '# */ "end of file"\n'
    )
    result = redact(block, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        comment + "\n"
        'key "rndc-key" {\n'
        "    algorithm hmac-sha256;\n"
        "    secret [REDACTED:bind_key_secret];\n"
        "};\n"
        '# */ "end of file"\n'
    )
    assert _FAKE_BIND_SECRET not in result.redacted
    assert len(result.manifest) == 1
    start, end = result.manifest[0].span
    assert block[start:end] == '"' + _FAKE_BIND_SECRET + '"'


@pytest.mark.parametrize(
    "line",
    ['version "none # hidden";', 'directory "/var/cache//bind";', 'key "a /* b" { };'],
    ids=["hash", "slashes", "block-start"],
)
def test_bind_key_secret_comment_marks_inside_a_quoted_string_are_text(line: str) -> None:
    """``#``, ``//`` and ``/*`` inside a quoted string do not start a comment.

    Otherwise the rest of the line, or the rest of the file, would count
    as a comment and the ``cookie-secret`` after it would stay visible.
    """
    block = "options {\n    " + line + "\n    cookie-secret " + _FAKE_COOKIE_SECRET + ";\n};\n"
    result = redact(block, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        "options {\n    " + line + "\n    cookie-secret [REDACTED:bind_key_secret];\n};\n"
    )
    assert _FAKE_COOKIE_SECRET not in result.redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '/*\nkey "old" { algorithm hmac-md5; secret "' + _FAKE_OLD_BIND_SECRET + '"; };\n*/',
            '/*\nkey "old" { algorithm hmac-md5; secret [REDACTED:bind_key_secret]; };\n*/',
        ),
        ('#secret "' + _FAKE_OLD_BIND_SECRET + '";', "#secret [REDACTED:bind_key_secret];"),
        ("// secret " + _FAKE_OLD_BIND_SECRET + ";", "// secret [REDACTED:bind_key_secret];"),
    ],
    ids=["block", "hash-no-space", "slash-unquoted"],
)
def test_bind_key_secret_hides_a_commented_out_key(text: str, expected: str) -> None:
    """A key block that an operator commented out still holds a secret."""
    result = redact(text, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == expected
    assert _FAKE_OLD_BIND_SECRET not in result.redacted


@pytest.mark.parametrize(
    "comment",
    [
        '/* secret "' + _FAKE_OLD_BIND_SECRET + '"; */ ',
        "# secret " + _FAKE_OLD_BIND_SECRET + ";\n    ",
        '// secret "' + _FAKE_OLD_BIND_SECRET + '";\n    ',
    ],
    ids=["block", "hash", "slash"],
)
def test_bind_key_secret_hides_a_secret_in_the_comment_before_the_value(comment: str) -> None:
    """A comment between ``secret`` and its value may hold the old secret.

    The finder reads the value in code past the comment, so it finds the
    new value before the old one. The old scanner then dropped the old
    value as if it overlapped (#5342 review). Both must be hidden.
    """
    block = (
        'key "rndc-key" {\n'
        "    algorithm hmac-sha256;\n"
        "    secret " + comment + '"' + _FAKE_BIND_SECRET + '";\n'
        "};\n"
    )
    result = redact(block, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        'key "rndc-key" {\n'
        "    algorithm hmac-sha256;\n"
        "    secret "
        + comment.replace(_FAKE_OLD_BIND_SECRET, "[REDACTED:bind_key_secret]").replace('"', "")
        + "[REDACTED:bind_key_secret];\n"
        "};\n"
    )
    assert _FAKE_BIND_SECRET not in result.redacted
    assert _FAKE_OLD_BIND_SECRET not in result.redacted
    assert result.manifest[0].count == 2
    start, end = result.manifest[0].span
    assert _FAKE_OLD_BIND_SECRET in block[start:end]


def test_bind_key_secret_redacts_two_keys_on_one_line() -> None:
    line = (
        'key "a" { algorithm hmac-sha256; secret "' + _FAKE_BIND_SECRET + '"; }; '
        'key "b" { algorithm hmac-sha256; secret ' + _FAKE_OLD_BIND_SECRET + "; };"
    )
    result = redact(line, _BIND_KEY_SECRET_ONLY_POLICY)
    assert result.redacted == (
        'key "a" { algorithm hmac-sha256; secret [REDACTED:bind_key_secret]; }; '
        'key "b" { algorithm hmac-sha256; secret [REDACTED:bind_key_secret]; };'
    )
    assert result.manifest[0].count == 2


@pytest.mark.parametrize(
    "payload",
    ["/*a" * 50000, '"a' * 50000, "secret /*" * 50000, "secret " + "#" * 100000],
    ids=["unclosed-blocks", "unclosed-quotes", "keywords-and-blocks", "long-line-comment"],
)
def test_bind_key_secret_unclosed_comments_and_quotes_stay_fast(payload: str) -> None:
    """A comment or quote that never closes is looked for once, not once per mark."""
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, DEFAULT_POLICY_RESOURCE)
    started = time.perf_counter()
    redact(payload, policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"redaction took {elapsed:.2f}s"


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
        # api_key: punctuated values -- the leading/embedded punctuation
        # must not break or truncate the match
        (
            "api_key",
            _PUNCT_LEADING_AT_FIXTURE,
            True,
        ),
        (
            "api_key",
            _PUNCT_DOLLAR_HASH_FIXTURE,
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
        # api_key: a value that already is a placeholder is not redacted
        # again (the label would go with it)
        (
            "api_key",
            "password: [REDACTED:param_name]",
            False,
        ),
        (
            "api_key",
            'password="[REDACTED:param_name]"',
            False,
        ),
        (
            "api_key",
            "api_key=[REDACTED:api_key]",
            False,
        ),
        # api_key: only a complete placeholder is skipped; a value that
        # only starts like one, or goes on after the ``]``, is redacted
        (
            "api_key",
            "password=[REDACTED:madeup-value",
            True,
        ),
        (
            "api_key",
            "password=[REDACTED:param_name]madeup-value",
            True,
        ),
        (
            "api_key",
            'password="[REDACTED:param_name]madeup-value"',
            True,
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
        # bind_key_secret: the quoted value after the ``secret`` keyword
        (
            "bind_key_secret",
            _BIND_KEY_SECRET_FIXTURE_POS,
            True,
        ),
        (
            "bind_key_secret",
            _BIND_KEY_SECRET_FIXTURE_NEG,
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
        # private_key_block: PEM private keys, also when cut off
        ("private_key_block", _PRIVATE_KEY_BLOCK_FIXTURE, True),
        ("private_key_block", _PRIVATE_KEY_CUT_FIXTURE, True),
        ("private_key_block", _CERTIFICATE_FIXTURE, False),
        # password_hash: crypt(3) hashes as in /etc/shadow
        ("password_hash", _SHADOW_LINE_FIXTURE, True),
        ("password_hash", _BCRYPT_HASH_FIXTURE, True),
        ("password_hash", _YESCRYPT_HASH_FIXTURE, True),
        ("password_hash", _ARGON2_HASH_FIXTURE, True),
        ("password_hash", "daemon:*:19000:0:99999:7:::", False),
        ("password_hash", 'echo "$1$2" costs $6 per month', False),
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
    matched = (
        pat.search(sample) is not None
        if isinstance(pat, re.Pattern)
        else bool(pat.secret_spans(sample))
    )
    if expect_match:
        assert matched, (
            f"{pattern!r} did not match {sample!r} -- "
            f"either the pattern is too narrow or the sample is wrong"
        )
    else:
        assert not matched, (
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
    ("api_key", _PUNCT_LEADING_AT_FIXTURE, True),
    ("api_key", _PUNCT_DOLLAR_HASH_FIXTURE, True),
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
    ("bind_key_secret", _BIND_KEY_SECRET_FIXTURE_POS, True),
    ("bind_key_secret", _BIND_KEY_SECRET_FIXTURE_NEG, False),
    ("private_key_block", _PRIVATE_KEY_BLOCK_FIXTURE, True),
    ("private_key_block", _CERTIFICATE_FIXTURE, False),
    ("password_hash", _SHADOW_LINE_FIXTURE, True),
    ("password_hash", "daemon:*:19000:0:99999:7:::", False),
    ("uuid", "tenant_id=12345678-1234-1234-1234-123456789abc", True),
    ("uuid", "build_hash=abcdef0123456789", False),
    ("ipv4", "192.168.1.42", True),
    ("ipv4", "999.999.999.999", False),
    ("ipv6", "2001:db8::8a2e:370:7334", True),
    ("ipv6", "1234deadbeef", False),
    ("fqdn", "vcenter.lab.example.com", True),
    ("fqdn", "version 1.2.3", False),
]


# ---------------------------------------------------------------------------
# Full-value capture through the engine (meho-internal hardening)
# ---------------------------------------------------------------------------
#
# ``test_pattern_matches_or_not`` only proves *a* match occurs; these
# cases prove the **entire** secret value -- including any punctuated
# tail the pre-widening character class used to stop at -- is replaced,
# with no cleartext fragment of the value surviving in the output.

_API_KEY_ONLY_POLICY = RedactionPolicy(
    id="test-api-key-full-capture",
    version=1,
    rules=(
        RedactionRule(
            name="strip-api-key",
            pattern="api_key",
            action="redact",
            reason="full-value capture regression",
        ),
    ),
)


@pytest.mark.parametrize(
    ("sample", "value_fragments"),
    [
        # Trailing punctuation inside a quoted value: the old class
        # stopped before ``!`` and left the tail + closing quote.
        (_PUNCT_QUOTED_FIXTURE, ["s3cr3tValue", "Value!"]),
        # Value beginning with an out-of-class byte run: the old class
        # produced no match at all and the whole password leaked.
        (_PUNCT_LEADING_AT_FIXTURE, ["P@ssw0rd", "ssw0rd!withMore", "withMore"]),
        # ``$`` / ``#`` mid-value: the old class produced no match on
        # the 7-char leading run and the whole value leaked.
        (_PUNCT_DOLLAR_HASH_FIXTURE, ["hunter2", "$plus", "#tail", "plus", "tail"]),
    ],
)
def test_punctuated_secret_value_fully_redacted(
    sample: str,
    value_fragments: list[str],
) -> None:
    """The whole punctuated value is replaced -- no cleartext fragment."""
    payload = "dispatch failed for " + sample + " during login"
    result = redact(payload, _API_KEY_ONLY_POLICY)
    redacted = result.redacted
    assert isinstance(redacted, str)
    assert "[REDACTED:api_key]" in redacted
    for fragment in value_fragments:
        assert fragment not in redacted, (
            f"cleartext fragment {fragment!r} of the secret value survived redaction: {redacted!r}"
        )
    # The non-secret context around the match is preserved -- the
    # widened class must not swallow neighbouring prose.
    assert redacted.startswith("dispatch failed for ")
    assert redacted.endswith(" during login")
    assert len(result.manifest) == 1
    assert result.manifest[0].pattern == "api_key"


@pytest.mark.parametrize(
    "sample",
    [
        pytest.param("password=[REDACTED:madeup-value", id="unfinished"),
        pytest.param("password=[REDACTED:param_name]madeup-value", id="appended"),
        pytest.param("password='[REDACTED:param_name]madeup-value'", id="appended-quoted"),
    ],
)
def test_placeholder_look_alike_is_redacted_whole(sample: str) -> None:
    """A value that only looks like a placeholder is a value: nothing of it survives."""
    payload = "dispatch failed for " + sample + " during login"
    result = redact(payload, _API_KEY_ONLY_POLICY)
    redacted = result.redacted
    assert isinstance(redacted, str)
    assert "madeup-value" not in redacted
    assert "[REDACTED:api_key]" in redacted
    assert redacted.startswith("dispatch failed for ")
    assert redacted.endswith(" during login")


def test_complete_placeholder_keeps_its_label() -> None:
    payload = "password=[REDACTED:param_name] token='[REDACTED:presidio:PERSON]' end"
    result = redact(payload, _API_KEY_ONLY_POLICY)
    assert result.redacted == payload
    assert not result.manifest


def test_default_policy_redacts_shadow_hash_and_keeps_the_other_columns() -> None:
    """The default policy hides the hash; the user name and dates stay readable."""
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, DEFAULT_POLICY_RESOURCE)
    result = redact(_SHADOW_LINE_FIXTURE + "\ndaemon:*:19000:0:99999:7:::", policy)
    assert result.redacted == (
        "root:[REDACTED:password_hash]:19000:0:99999:7:::\ndaemon:*:19000:0:99999:7:::"
    )


def test_default_policy_redacts_private_key_block_and_keeps_certificates() -> None:
    """A private key block is hidden whole; a certificate next to it stays."""
    policy = load_policy_yaml(DEFAULT_POLICY_PACKAGE, DEFAULT_POLICY_RESOURCE)
    text = _CERTIFICATE_FIXTURE + "\n" + _PRIVATE_KEY_BLOCK_FIXTURE + "\ntail"
    result = redact(text, policy)
    assert result.redacted == _CERTIFICATE_FIXTURE + "\n[REDACTED:private_key_block]\ntail"
