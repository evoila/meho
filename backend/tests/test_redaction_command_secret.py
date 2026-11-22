# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A password on a command line is hidden in output text.

Log text, journal text and process lists show commands as they were
typed. The default policy hides the secret value in each form of the
``command_line_secret`` rule and keeps the option names and the other
words readable. A harmless look-alike stays as it is.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_logs import k8s_logs
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.redaction.command_secret import COMMAND_SECRET
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.redaction.patterns import get_pattern, secret_spans
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets
from tests.test_redaction_command_secret_logs import _JSON_FORMS, _json_string

PW: Final = "made-up-pw-4711"
#: Built from pieces, so secret scanners do not flag the lines that use them.
_USER_PW: Final = "bob" + ":" + PW
_PASSWORD_IS: Final = "Pass" + "word="
_PWD_IS: Final = "P" + "wd="
_PW_IS: Final = "p" + "w="
_PUBLIC_KEY_ID: Final = "0x" + "1234ABCD" + "5678EF90"


#: (line, text that must be gone, text that must stay)
_HIDDEN: Final[list[tuple[str, str, str]]] = [
    (f"run --password {PW} --user a", PW, "--password"),
    (f"run --passwd={PW} next", PW, "run --"),
    (f"run --pass {PW} --pwd {PW}", PW, "--pwd"),
    (f"run --token {PW} --secret {PW}", PW, "--secret"),
    (f"run --api-key {PW} --client-secret={PW}", PW, "--api-key "),
    (f"run --db-password {PW} tail", PW, "tail"),
    (f"sshpass -p {PW} ssh -p 22 host", PW, "ssh -p 22 host"),
    (f"sshpass -p{PW} ssh host", PW, "ssh host"),
    (f"mysql -u root -p{PW} appdb", PW, "appdb"),
    (f"mysqldump -p'{PW} x' appdb", PW, "appdb"),
    (f"mariadb --host h -p{PW}", PW, "--host h"),
    (f"sqlcmd -S srv -U sa -P {PW} -Q x", PW, "-S srv"),
    (f"bcp db.t out f -U sa -P {PW}", PW, "db.t out f"),
    (f"osql -P {PW} -S srv", PW, "-S srv"),
    (f"nsrsqlsv -P {PW} -s backupsrv", PW, "-s backupsrv"),
    (f"ldapsearch -x -D cn=admin -w {PW} -b dc=x", PW, "-D cn=admin"),
    (f"ldapmodify -w{PW} -f f.ldif", PW, "-f f.ldif"),
    (f"curl -u {_USER_PW} https://h/x", PW, "bob:"),
    (f"curl -u{_USER_PW} https://h/x", PW, "https://h/x"),
    (f"curl --user {_USER_PW} https://h/x", PW, "--user bob:"),
    (f"smbclient //h/share -U bob%{PW} -c ls", PW, "bob%"),
    (f"DB_PASSWORD={PW} run", PW, " run"),
    (f"APP_PASS={PW} APP_PWD={PW} APP_SECRET={PW}", PW, "APP_PASS="),
    (f"API_TOKEN={PW} SIGNING_KEY={PW}", PW, "SIGNING_KEY="),
    (f"AWS_SECRET_ACCESS_KEY={PW} SECRET_KEY={PW}", PW, "AWS_SECRET_ACCESS_KEY="),
    # Every ``_KEY`` name is hidden, except a short list of names that hold no secret.
    *[
        (f"{name}_KEY={PW} run", PW, f"{name}_KEY=")
        for name in (
            "APP",
            "JWT",
            "AES",
            "ENC",
            "TLS",
            "SSL",
            "RSA",
            "KMS",
            "DB",
            "ROOT",
            "COOKIE",
            "WEBHOOK",
            "HASH",
            "ROUTING",
            "A",
        )
    ],
    (f"run --session-token {PW} --page-size 50", PW, "--page-size 50"),
    # A value joined from quoted parts, with an escaped space or quote.
    ("run --password 'made-up'\"-pw-4711\" tail", "made-up", "tail"),
    ("run --password made-up\\ pw\\ 4711 tail", "pw\\ 4711", "tail"),
    ('run --password "made-up\\"pw 4711" tail', "pw 4711", "tail"),
    # A command quoted inside a longer command.
    (f'sh -c "mysql -p{PW}" && echo ok', PW, "echo ok"),
    # A ``;`` inside a quoted or escaped argument does not end the command.
    (f'curl -H "Type: text; charset=utf-8" -u {_USER_PW} h', PW, "charset"),
    (f"curl -H 'a; b' -H a\\;b --user {_USER_PW} https://h/x", PW, "'a; b'"),
    # A quoted argument with a space is one word, also before the option.
    (f"sshpass -P 'Enter password:' -p {PW} ssh host", PW, "ssh host"),
    # A quoted tool name: its options follow the closing quote.
    (f'"mysql" -p{PW} db', PW, '"mysql" -p'),
    (f"'/usr/bin/sshpass' -p {PW} ssh host", PW, "ssh host"),
    # A quote inside a value that is never closed is part of the value.
    ("run --password made'up --user a", "up", "--user a"),
]

#: Harmless text that looks like one of the forms.
_KEPT: Final[list[str]] = [
    "ssh -p 22 host",
    "mkdir -p /tmp/x/y",
    "ls -p 8080",
    "mysql -P 3306 -h db",
    "mysql -p -h db",
    "sshpass -f /etc/pw ssh -p 22 host",
    "sqlcmd -S srv -p -Q x",
    "curl -s https://h/x",
    "curl -u bob https://h/x",
    "ldapsearch -x -W -b dc=x",
    "docker login --password-stdin",
    "run --token-file /etc/token --secret-name app",
    "run --no-password",
    "run --password --verbose",
    "PWD=/home/user",
    "SORT_ORDER=asc",
    "registry.example.com/app@sha256:0123456789abcdef",
    'uses: actions/checkout"x"@v4',
    "a/'never closed@orcl",
    "pw=a b",
    "uses: actions/checkout@v4",
    "images/logo@2x.png",
    "Server=db;Database=d",
    "Password=;Server=db",
    "Server=db;Password=",
    "<user>u</user><password></password><password/>",
    "the password prompt was shown",
    "<password><![CDATA[x]]></user></password>",
    '"mysql" -p 3306',
    'echo "mysql" is fine',
    # Names that look like a secret but hold none: a paging token, a ``_KEY``
    # name that does not name a secret key, a compiler option, a folder.
    "aws s3api list-objects-v2 --bucket b --next-token eyJNYXJrZXIiOiBudWxsfQ==",
    "list --continuation-token 1x2y3z --page-token p2",
    "<Result><NextToken>AAAA-page-2</NextToken><IsTruncated>true</IsTruncated></Result>",
    "<ListBucketResult><ContinuationToken>1ueGcxLPRx1</ContinuationToken></ListBucketResult>",
    "NEXT_TOKEN=page-2 ./list.sh",
    "S3_KEY=path/to/object.tar.gz aws s3 cp",
    "PARTITION_KEY=tenant-1 SORT_KEY=created_at",
    "GPG_KEY=" + _PUBLIC_KEY_ID + " gpg --recv-keys",
    "PRIMARY_KEY=id FOREIGN_KEY=user_id CACHE_KEY=user:42",
    "SSH_PUBLIC_KEY=ssh-ed25519 PGP_KEY=" + _PUBLIC_KEY_ID,
    "mysql -pthread",
    "gcc $(mysql_config --cflags) main.c -pthread",
    "cd /srv; pwd=/tmp",
    "pwd=/tmp",
    "echo pwd=/srv/app; ls",
    "export PWD=/tmp; ls",
]


@pytest.mark.parametrize(("line", "gone", "kept"), _HIDDEN)
def test_hidden_forms_lose_the_value_and_keep_the_words(line: str, gone: str, kept: str) -> None:
    out = redact(line, get_default_policy()).redacted
    assert gone not in out
    assert kept in out
    assert "[REDACTED:" in out


@pytest.mark.parametrize("name", ["HASH_KEY", "ROUTING_KEY"])
def test_a_hash_or_routing_key_is_hidden_in_the_answer_and_the_trace(name: str) -> None:
    line = f"{name}={PW} run"
    trace, _fired = scrub_content(line)
    for out in (redact(line, get_default_policy()).redacted, trace):
        assert out == f"{name}={_MARK} run"


@pytest.mark.parametrize("line", _KEPT)
def test_harmless_look_alikes_stay_as_they_are(line: str) -> None:
    assert COMMAND_SECRET.secret_spans(line) == []
    assert "[REDACTED:command_line_secret]" not in redact(line, get_default_policy()).redacted


def test_the_value_is_hidden_on_every_line_of_a_log() -> None:
    log = f"start\nmysql -p{PW} db\nmiddle\nrun --token {PW}\nend"
    out = redact(log, get_default_policy()).redacted
    assert PW not in out
    assert out.startswith("start\nmysql -p")
    assert out.endswith("\nend")


#: Service names that look like a branch, a version or a commit id.
_BRANCH_LIKE: Final = ["main", "master", "latest", "head", "stable", "v19prod", "v4", "sha256"]
_BRANCH_LIKE_WITH_HEX: Final = [*_BRANCH_LIKE, "deadbeef1", "abc1234"]


@pytest.mark.parametrize(
    "line",
    [
        *(f"uses: org/action@{name}" for name in _BRANCH_LIKE_WITH_HEX),
        *(f"pip install git+https://h/org/repo@{name}" for name in _BRANCH_LIKE),
        "registry.example.com/team/app@sha256:0123456789abcdef",
        # A client in another command or on another line does not count.
        "sqlplus -v; uses: org/action@main",
        "sqlplus -v\nuses: org/action@v4",
        "echo sqlplus | tee org/action@main",
    ],
)
def test_a_repository_reference_without_a_database_client_stays(line: str) -> None:
    assert COMMAND_SECRET.secret_spans(line) == []
    assert redact(line, get_default_policy()).redacted == line


def test_a_value_that_is_a_placeholder_is_left_alone() -> None:
    assert COMMAND_SECRET.secret_spans("--token=[REDACTED:api_key] x") == []


def test_an_unclosed_quote_does_not_hide_the_rest_of_the_text() -> None:
    line = f'sh -c "mysql -p{PW}'
    out = redact(f"{line}\nnext line stays", get_default_policy()).redacted
    assert PW not in out
    assert out.endswith("\nnext line stays")
    out = redact("run --password 'half\nnext line stays", get_default_policy()).redacted
    assert out.endswith("\nnext line stays")


#: A quoted value with a single quote mark earlier on the same line, for
#: long options, tool options and environment names.
_AFTER_APOSTROPHE: Final[list[tuple[str, str]]] = [
    ("don't wait: backup-agent --password 'made up' --target s3", "--target s3"),
    ("it's done: mysql -u root -p'made up' appdb", "appdb"),
    ("job's env: DB_PASSWORD='made up' ./run.sh", "./run.sh"),
]


@pytest.mark.parametrize(("line", "kept"), _AFTER_APOSTROPHE)
def test_a_quoted_value_is_hidden_after_an_apostrophe_on_the_line(line: str, kept: str) -> None:
    out = redact(line, get_default_policy()).redacted
    assert "made" not in out
    assert out.startswith(line.split(":", 1)[0] + ":")
    assert out.endswith(kept)
    assert _MARK in out


def test_spans_never_overlap_and_are_left_to_right() -> None:
    text = f"mysql -p{PW} --password {PW} DB_PASSWORD={PW}; Server=a;{_PASSWORD_IS}{PW};"
    spans = COMMAND_SECRET.secret_spans(text)
    assert spans == sorted(spans)
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(spans))
    assert all(text[a:b] and PW in text[a:b] for a, b in spans)


_MARK: Final = "[REDACTED:command_line_secret]"
#: The mark of the api_key rule. Where that rule hides a value whole, the
#: answer is the one of main.
_KEY_MARK: Final = "[REDACTED:api_key]"

#: (line, the whole filter output): only the value goes, every other word stays.
_WHOLE_OUTPUT: Final[list[tuple[str, str]]] = [
    (
        f"run --{_PASSWORD_IS.lower()}{PW} --user alice --verbose",
        f"run --{_KEY_MARK} --user alice --verbose",
    ),
    (f"run --{_PASSWORD_IS.lower()}'made up' --user alice", f"run --password={_MARK} --user alice"),
    (f"run -{_PASSWORD_IS}{PW} --user alice", f"run -{_KEY_MARK} --user alice"),
    (
        f"login {_PASSWORD_IS.lower()}{PW} user=alice; echo ok",
        f"login {_KEY_MARK} user=alice; echo ok",
    ),
    # A command option keeps the words after its value, also before a ``;``.
    (
        f"run --{_PASSWORD_IS.lower()}{PW} --user alice; mode=fast",
        f"run --{_KEY_MARK} --user alice; mode=fast",
    ),
    (
        f"run -{_PASSWORD_IS}{PW} --user alice; mode=fast",
        f"run -{_KEY_MARK} --user alice; mode=fast",
    ),
    (
        f"login {_PASSWORD_IS.lower()}{PW} --verbose; echo ok",
        f"login {_KEY_MARK} --verbose; echo ok",
    ),
    # A quoted argument with a space before the password option of sshpass.
    (
        f"sshpass -P 'Enter password:' -p {PW} ssh host",
        f"sshpass -P 'Enter password:' -p {_MARK} ssh host",
    ),
    # A command quoted inside a longer one ends at its closing quote.
    (
        f'sh -c "mysql -p{PW}"; echo "status ok"',
        f'sh -c "mysql -p{_MARK}"; echo "status ok"',
    ),
    (
        f"sh -c 'run --password {PW}'; echo 'status ok'",
        f"sh -c 'run --password {_MARK}'; echo 'status ok'",
    ),
    (
        'sh -c "DB_PASSWORD=\'made up\'"; echo "status ok"',
        f'sh -c "DB_PASSWORD={_MARK}"; echo "status ok"',
    ),
    (
        f"don't run mysql -p{PW} it's bad",
        f"don't run mysql -p{_MARK} it's bad",
    ),
    # A quoted tool name is not a quoted command.
    (f'"mysql" -p{PW} db', f'"mysql" -p{_MARK} db'),
    (f"'/usr/bin/mysql' -p{PW} db; echo ok", f"'/usr/bin/mysql' -p{_MARK} db; echo ok"),
    # A quote inside a value that is never closed is part of the value.
    ("run --password made'up --user a", f"run --password {_MARK} --user a"),
    ('sh -c "mysql -pmade\'up"; echo "ok"', f'sh -c "mysql -p{_MARK}"; echo "ok"'),
]


@pytest.mark.parametrize(("line", "expected"), _WHOLE_OUTPUT)
def test_only_the_value_is_replaced(line: str, expected: str) -> None:
    assert redact(line, get_default_policy()).redacted == expected


def test_the_trace_scrub_hides_what_the_answer_hides() -> None:
    line = f"exec mysql -u root -p{PW} appdb; User ID=u;{_PASSWORD_IS}{PW} x y;"
    answer = redact(line, get_default_policy()).redacted
    trace, fired = scrub_content(line)
    assert fired is True
    assert PW not in answer
    assert trace == answer


# -- only hides more ---------------------------------------------------------------

_SHORT: Final = "made-up-short"

#: Command texts from the tests of the other redaction rules (the command
#: line preview filter, the cron job and job previews, the label rules).
_OTHER_RULE_TEXTS: Final[list[str]] = [
    f"PGPASSWORD={_SHORT};tail",
    f'env "PGPASSWORD={_SHORT}"suffix pg_dump app',
    f"curl -u {'admin' + ':' + _SHORT} https://example.invalid",
    f"PGPASSWORD={_SHORT} pg_dump -h db app; id",
    f'tool --password "{_SHORT}\\" phrase" run',
    f'sh -c "PGPASSWORD={_SHORT}; id" && echo done',
    f"export PGPASSWORD={_SHORT} && pg_dump -h db app",
    f"MYSQL_ROOT_PASSWORD='{_SHORT} {_SHORT}' mysqld",
    f"--password={_SHORT} --token={_SHORT} --dry-run",
    f"mytool --password '{_SHORT} phrase' run",
    f"mytool --password {_SHORT}\\ phrase run",
    f"mytool --password={_SHORT}\\ phrase run",
    f"PGPASSWORD='{_SHORT} phrase pg_dump",
    f"tool --password {_SHORT} 2>&1; id",
    f"tool -p '{_SHORT} phrase' x",
    f"env 'PGPASSWORD={_SHORT} phrase' pg_dump app",
    f'sh -c "pg_dump --password {_SHORT} app" && echo done',
    f"mytool --password\n{_SHORT}",
    f"mkdir -p /tmp/x\nsudo sshpass -p {_SHORT} ssh deploy@host id\nmariadb -p{_SHORT}\n",
    f"DB_PASSWORD={_SHORT}{_SHORT}",
    f"export GITHUB_TOKEN={_SHORT}{_SHORT}",
    f"x-api-key: {_SHORT}{_SHORT}",
    f"{_PASSWORD_IS}{_SHORT};Database=d user=x",
    f"login {_PASSWORD_IS.lower()}{_SHORT}{_SHORT};tail",
    f"<Login><userName>root</userName><password>{_SHORT}</password></Login>",
]

_WORD: Final = re.compile(r"[A-Za-z0-9][\w.-]*")
_ANY_PLACEHOLDER: Final = re.compile(r"\[REDACTED:[^\]]*\]")
#: A label of a secret (``password``, ``DB_PASSWORD``, ``x-api-key``). Some
#: rules hide the label with the value; this rule keeps it readable.
_LABEL: Final = re.compile(
    r"(?i)[\w.-]*(?:pass(?:word|wd)?|pwd|pw|token|secret|key|id)", re.IGNORECASE
)


def _without_command_rule(policy: RedactionPolicy | None = None) -> RedactionPolicy:
    """*policy* (the default policy when not given) as it is on main: without this rule."""
    policy = policy or get_default_policy()
    rules = tuple(rule for rule in policy.rules if rule.pattern != "command_line_secret")
    assert len(rules) == len(policy.rules) - 1
    return policy.model_copy(update={"rules": rules})


def _texts_of_other_tests() -> list[str]:
    from tests.test_redaction_patterns import _PATTERN_TEST_ROWS

    return [text for _name, text, _hit in _PATTERN_TEST_ROWS] + _OTHER_RULE_TEXTS


#: (text, what each policy shows): fields after a connection string password.
#: Where the api_key rule hides the password whole, the answer is the one of
#: main. Where it does not, only the value is hidden: the label and the
#: fields after it stay.
_AFTER_A_PASSWORD: Final[list[tuple[str, str]]] = [
    (f"{label}example-password;Token=demo12", "[REDACTED:api_key]")
    for label in (_PWD_IS, _PASSWORD_IS)
] + [
    (
        f"Server=db;{_PWD_IS}'made up';ApplicationIntent=ReadOnly",
        f"Server=db;{_PWD_IS}{_MARK};ApplicationIntent=ReadOnly",
    ),
    (
        f"Server=db;{_PWD_IS}'{PW}';User ID=app;Encrypt=true; Token = t1",
        "Server=db;[REDACTED:api_key];User ID=app;Encrypt=true; Token = t1",
    ),
    (f"{_PW_IS}{PW};Database=d", f"{_PW_IS}{_MARK};Database=d"),
    (f"{_PASSWORD_IS} {PW};Token=demo12 words", "[REDACTED:api_key] words"),
    # A last field may hold spaces: the whole password is hidden, also its
    # part that the api_key rule hides.
    (f"Server=db;{_PWD_IS}{PW} long phrase", f"Server=db;{_MARK}"),
    (f"Database=demo;{_PWD_IS}made up long phrase", f"Database=demo;{_PWD_IS}{_MARK}"),
]


def _shown(text: str) -> str:
    return _ANY_PLACEHOLDER.sub(" ", text)


_FIELD_SECRET: Final = "tk-41"
#: Connection strings with odd parts after the password.
_STOPS: Final[list[str]] = [
    ";Database=d;;",  # a double semicolon
    ";Database=d;1x=v;",  # a name that does not start with a letter
    ";Data$base=d;",  # a name with other characters
    ";" + "N" * 70 + "=v;",  # a name longer than 64 characters
    ";Database=d;plain;",  # a part with no ``=``
    ";Database={d;",  # a ``{`` value that is never closed
    ";Database=" + "d" * 1100 + ";",  # a field more than 1,024 characters further on
]
_STOPPED: Final[list[str]] = [
    f"{label}{value}{stop}{name}={_FIELD_SECRET}"
    for label in (_PWD_IS, _PASSWORD_IS)
    for value in ("example-password", "'made up'")
    for stop in _STOPS
    for name in ("AccountKey", "SharedAccessKey", "Token")
]


def _content_policy() -> RedactionPolicy:
    from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY

    return _CONTENT_POLICY


def _hidden_characters(text: str, policy: RedactionPolicy) -> set[int]:
    """The places of *text* that the answer under *policy* hides, rule by rule.

    It runs the rules the way the engine does on one text, and keeps for
    each character of the answer the place in *text* it came from. The
    answer it builds is checked against the engine's answer.
    """
    current, origin, hidden = text, list(range(len(text))), set()
    for rule in policy.rules:
        spans = secret_spans(get_pattern(rule.pattern), current)
        parts: list[str] = []
        places: list[int] = []
        pos = 0
        for start, end in spans:
            mark = f"[REDACTED:{rule.pattern}]"
            parts += [current[pos:start], mark]
            places += origin[pos:start] + [-1] * len(mark)
            hidden.update(place for place in origin[start:end] if place >= 0)
            pos = end
        current = "".join([*parts, current[pos:]])
        origin = places + origin[pos:]
    assert current == redact(text, policy).redacted
    return hidden


def _not_a_secret_value_change(text: str, policy: RedactionPolicy) -> list[str]:
    """Each difference to the answer of main that is not a secret value replaced by the mark.

    The answer now shows nothing that main hides. It hides a part that
    main shows only inside a value this rule reads as a secret.
    """
    on_main = _hidden_characters(text, _without_command_rule(policy))
    now = _hidden_characters(text, policy)
    values = {place for a, b in COMMAND_SECRET.secret_spans(text) for place in range(a, b)}
    wrong = [f"shown now: {text[place]!r} at {place}" for place in sorted(on_main - now)]
    wrong += [f"hidden now: {text[place]!r} at {place}" for place in sorted(now - on_main - values)]
    return wrong


@pytest.mark.parametrize(
    "text", [*_STOPPED, *(json.dumps({"msg": line, "ok": 1}) for line in _STOPPED)]
)
def test_odd_parts_after_a_connection_password_are_shown_or_hidden_as_on_main(
    text: str,
) -> None:
    for policy in (get_default_policy(), _content_policy()):
        if _FIELD_SECRET not in redact(text, _without_command_rule(policy)).redacted:
            assert _FIELD_SECRET not in redact(text, policy).redacted
        assert _not_a_secret_value_change(text, policy) == []


def _all_test_texts() -> list[str]:
    texts = _texts_of_other_tests() + [line for line, _gone, _kept in _HIDDEN]
    texts += [line for line, _expected in _WHOLE_OUTPUT]
    texts += [line for line, _kept in _AFTER_APOSTROPHE]
    texts += [line for line, _expected in _AFTER_A_PASSWORD]
    texts += _STOPPED + _KEPT + _JSON_FORMS
    return texts + [_json_string(form) for form in _JSON_FORMS]


def test_the_rule_only_hides_more() -> None:
    """No word that the policy without this rule hides is shown with it."""
    before_policy = _without_command_rule()
    shown_now: dict[str, set[str]] = {}
    for text in _all_test_texts():
        before = _shown(redact(text, before_policy).redacted)
        now = _shown(redact(text, get_default_policy()).redacted)
        # A word shown now must be shown without the rule too (also as a part
        # of a longer word there: ``-p`` of ``-pvalue``), or be a label.
        more = {
            word for word in _WORD.findall(now) if word not in before and not _LABEL.fullmatch(word)
        }
        if more:
            shown_now[text] = more
    assert shown_now == {}


_CMD_MARK: Final = "[REDACTED:" + "command_line_secret]"


def _in_json(line: str, levels: int) -> str:
    """*line* as a JSON log line, inside *levels* JSON documents."""
    text = line
    for level in range(levels):
        text = json.dumps({"log": text, "ok": level})
    return text


def _as_json(secret: str, levels: int) -> str:
    """*secret* as it stands in the text of :func:`_in_json`."""
    for _level in range(levels):
        secret = json.dumps(secret)[1:-1]
    return secret


#: A user and a password in double quotes, with an escaped quote, a dollar
#: sign, a backtick or a backslash in one of the parts. Built from pieces, so
#: secret scanners do not flag the lines that use them.
_ESCAPED: Final = ("pa" + '\\"ss\\$wo\\`rd', "pa" + "\\\\ss$wd", "pa" + '\\`ss\\"wd')
_QUOTED_LOGINS: Final[list[tuple[str, str]]] = [
    ('curl -u "ad\\"min:' + PW + '" https://h/x', PW),  # gitleaks:allow
    ('curl -u "admin:' + _ESCAPED[0] + '" https://h/x', _ESCAPED[0]),  # gitleaks:allow
    ('curl --user "ad\\\\min:' + PW + '" https://h/x', PW),  # gitleaks:allow
    ('curl --user "ad`min:' + _ESCAPED[1] + '" https://h/x', _ESCAPED[1]),  # gitleaks:allow
    ('smbclient //h/share -U "bo\\"b%' + PW + '" -c ls', PW),
    ('smbclient //h/share -U "bob%' + _ESCAPED[2] + '" -c ls', _ESCAPED[2]),
]
#: A password with an escaped double quote in the middle, at the start and
#: at the end. In plain text ``\\"`` is part of the shell word.
_WITH_QUOTE: Final = ('ab\\"cd', '\\"abcd', 'abcd\\"')
#: The forms, each with ``{}`` where the value stands.
_QUOTE_FORMS: Final = (
    "run --password {} next",
    "run --password={} next",
    "sshpass -p {} ssh h",
    "mysql -p{} db",
    "sqlcmd -S h -P {} -Q x",
    "ldapsearch -w {} -b x",
    "curl -u admin:{} https://h/x",  # gitleaks:allow
    "smbclient //h/share -U bob%{} -c ls",
    "DB_PASS={} app",
)
_ESCAPED_QUOTES: Final[list[tuple[str, str]]] = [
    (form.format(value), value) for form in _QUOTE_FORMS for value in _WITH_QUOTE
]
#: A JSON log line with a mask of bullets after ``--token``, written as JSON escapes.
_MASK_IN_JSON: Final = json.dumps({"msg": "run --token \u2022\u2022\u2022\u2022 x"})
#: A double-quoted string inside a single-quoted command or string: the
#: value ends at its closing double quote, and the words after it stay. An
#: apostrophe in log text also opens a single-quoted string.
_INNER_DOUBLE: Final[list[tuple[str, str]]] = [
    ("bash -c 'echo \"DB_PASS=" + PW + '" > /tmp/env && echo "done"\'', PW),
    ("sh -c 'run \"--db-pass=" + PW + '" "--target" "s3"\'', PW),
    ('app --data \'{"args": "--token ' + PW + '", "mode": "fast"}\'', PW),
    ("it's starting: sh -c \"mysql -p" + PW + '" --label "nightly" --owner "ops"', PW),
]
#: A quoted tool name inside a single-quoted command: its options end at the
#: closing quote of that command.
_QUOTED_TOOL_IN_SINGLE: Final = 'bash -c \'"mysql" -p' + PW + ' db\' && echo "ok"'
#: Lines of :data:`_SECRETS_OF` that are read in plain text only. In a JSON
#: string a quoted value starts with an escaped quote, and that ends the value.
_PLAIN_TEXT_ONLY: Final = {
    _MASK_IN_JSON,
    _QUOTED_TOOL_IN_SINGLE,
    *(line for line, _secret in _QUOTED_LOGINS),
    *(line for line, _secret in _ESCAPED_QUOTES),
}
#: (line, its secret values). Each value is hidden whole, and nothing else
#: changes against main: a mark that was in the text before is read as on
#: main, a value hidden already stays (also a mask written as JSON escapes),
#: a value the api_key rule hides is left to it, and a short value is hidden
#: unless a character other than white space or a quote is glued after it.
_SECRETS_OF: Final[list[tuple[str, tuple[str, ...]]]] = [
    (f"token={_CMD_MARK};{PW}", (PW,)),
    (f"token={_CMD_MARK},{PW}", (PW,)),
    (f"token={_CMD_MARK}-{PW}", (PW,)),
    (f"x-api-key: {_CMD_MARK}{PW}", (PW,)),
    ("AWS_SECRET_ACCESS_KEY=***REDACTED*** app", ()),
    ("run --password [REDACTED] x", ()),
    ("run --password '****' x", ()),
    ("run --token \u2022\u2022\u2022\u2022 x", ()),
    ("curl -u admin:***REDACTED*** https://example.invalid", ()),
    ("<password>[REDACTED]</password>", ()),
    (f"cd /tmp; {_PASSWORD_IS.lower()}{PW} --user admin", (PW,)),
    (_MASK_IN_JSON, ()),
    *((line, (secret,)) for line, secret in _QUOTED_LOGINS),
    *((line, (secret,)) for line, secret in _ESCAPED_QUOTES),
    *((line, (secret,)) for line, secret in _INNER_DOUBLE),
    (_QUOTED_TOOL_IN_SINGLE, (PW,)),
    ("run --password=ab12cd next", ("ab12cd",)),
    ("DB_PASSWORD=ab12cd next", ("ab12cd",)),
    ("run --password=ab12cd; echo ok", ()),
    ("DB_PASSWORD=ab12cd; echo ok", ()),
]
#: (line, its secret values, JSON levels): the line as a JSON log line, also
#: inside more JSON documents. Every level of the answer that parses on main
#: still parses.
_SECRETS_IN_JSON: Final[list[tuple[str, tuple[str, ...], int]]] = [
    *((line, secrets, 1) for line, secrets in _SECRETS_OF if line not in _PLAIN_TEXT_ONLY),
    *(
        (line, (secret,), levels)
        for line, secret in ((f"run --password {PW}", PW),)
        for levels in (2, 3)
    ),
]


def _places(text: str, parts: tuple[str, ...]) -> set[int]:
    places: set[int] = set()
    for part in parts:
        for found in re.finditer(re.escape(part), text):
            places.update(range(*found.span()))
    return places


def _changes_besides(text: str, secrets: tuple[str, ...], policy: RedactionPolicy) -> list[str]:
    """Each difference to main that is not one of *secrets* replaced by the mark."""
    on_main = _hidden_characters(text, _without_command_rule(policy))
    now = _hidden_characters(text, policy)
    values = _places(text, secrets)
    wrong = [f"shown now: {text[place]!r} at {place}" for place in sorted(on_main - now)]
    wrong += [f"hidden now: {text[place]!r} at {place}" for place in sorted(now - on_main - values)]
    wrong += [f"not hidden: {text[place]!r} at {place}" for place in sorted(values - now)]
    return wrong


def _parses(text: str, levels: int) -> bool:
    for _level in range(levels):
        try:
            text = json.loads(text)["log"]
        except (ValueError, KeyError, TypeError):
            return False
    return True


def test_every_change_to_an_answer_is_a_secret_value_replaced_by_the_mark() -> None:
    """Over the texts of main's own tests and of the form tests, in the answer and the trace."""
    wrong: dict[str, list[str]] = {}
    for text in _all_test_texts():
        for policy in (get_default_policy(), _content_policy()):
            if found := _not_a_secret_value_change(text, policy):
                wrong[text] = found
    cases = [(line, secrets, 0) for line, secrets in _SECRETS_OF] + _SECRETS_IN_JSON
    for line, secrets, levels in cases:
        text = _in_json(line, levels)
        values = tuple(_as_json(secret, levels) for secret in secrets)
        for policy in (get_default_policy(), _content_policy()):
            if found := _changes_besides(text, values, policy):
                wrong[text] = found
            on_main = redact(text, _without_command_rule(policy)).redacted
            if _parses(on_main, levels) and not _parses(redact(text, policy).redacted, levels):
                wrong[text] = ["the answer does not parse"]
    assert wrong == {}


#: (form, value): each form with ``{}`` where the value stands, at the end.
#: A single-quoted value is hidden with its quotes. ``--password=`` is left
#: out: the api_key rule hides that value and what follows it, as on main.
_LINE_END_FORMS: Final = (
    *(
        (form.split("{}")[0] + "{}", PW)
        for form in _QUOTE_FORMS
        if not form.startswith("run --password=")
    ),
    ("run --password {}", "'" + PW + "'"),
)
#: What follows the value in the line: a line end and more words, a line end
#: at the end of the JSON string, a tab and a word, a carriage return and a
#: line end, and two line ends. In a JSON string each is an escape.
_LINE_END_TAILS: Final = ("\nnext line", "\n", "\tnext", "\r\nnext line", "\n\nnext line")
#: (line, its secret value, JSON levels): inside a JSON string an escaped line
#: end or tab ends the value, so the words after it stay as on main.
_LINE_ENDS_IN_JSON: Final[list[tuple[str, str, int]]] = [
    (form.format(value) + tail, value, levels)
    for form, value in _LINE_END_FORMS
    for tail in _LINE_END_TAILS
    for levels in (1, 2)
] + [
    ('echo "{\\"msg\\": \\"run --password ' + PW + tail + 'more\\"}" | app', PW, levels)
    for tail in ("\\n", "\\t", "\\r\\n")
    for levels in (0, 1, 2)
]


@pytest.mark.parametrize(("line", "secret", "levels"), _LINE_ENDS_IN_JSON)
def test_an_escaped_line_end_or_tab_in_a_json_string_ends_the_value(
    line: str, secret: str, levels: int
) -> None:
    text = _in_json(line, levels)
    value = _as_json(secret, levels)
    for policy in (get_default_policy(), _content_policy()):
        assert _changes_besides(text, (value,), policy) == []
        on_main = redact(text, _without_command_rule(policy)).redacted
        assert not _parses(on_main, levels) or _parses(redact(text, policy).redacted, levels)


#: (first line, later lines): a tool on the first line, without and with a
#: password, and plain options of other tools on a later line that the tool
#: on the first line would read as its own.
_TOOL_THEN_OTHER: Final = (
    (
        ("mysql --version", f"mysql -u root -p{PW}"),
        ("tar -pcf archive.tar files", "gcc -pedantic x.c"),
    ),
    (("mariadb --version", f"mariadb -p{PW} db"), ("tar -pcf archive.tar files",)),
    (("sqlcmd -S h -Q x", f"sqlcmd -S h -P {PW}"), ("ssh -P 2222 h", "mvn -P release package")),
    (("bcp t out f", f"bcp t out f -P {PW}"), ("mvn -P release package",)),
    (("ldapsearch -x", f"ldapsearch -w {PW} -x"), ("grep -w word file", "nc -w 5 h 80")),
    (("ldapmodify -x", f"ldapmodify -w {PW} -x"), ("grep -w word file",)),
    (
        ("curl https://h/x", "curl -u admin" + ":" + PW + " https://h/x"),
        ("docker run -u 1000:1000 img",),
    ),
    (("smbclient //h/share", f"smbclient //h/share -U bob%{PW}"), ("rpm -U pkg-1%2.rpm",)),
)
#: How the lines are split: a line end, a carriage return and a line end,
#: two line ends, and a blank before the line end.
_LINE_SPLITS: Final = ("\n", "\r\n", "\n\n", " \n")
#: (line, its secret values, JSON levels): inside a JSON string or a
#: double-quoted command an escaped line end ends the command of the tool,
#: so the options on the later line stay as on main.
_COMMAND_ENDS_IN_JSON: Final[list[tuple[str, tuple[str, ...], int]]] = [
    (first + split + later, (PW,) if PW in first else (), levels)
    for firsts, laters in _TOOL_THEN_OTHER
    for first in firsts
    for later in laters
    for split in _LINE_SPLITS
    for levels in (1, 2)
] + [
    (line, (PW,) if PW in line else (), levels)
    for firsts, laters in _TOOL_THEN_OTHER
    for first in firsts
    for later in laters
    for line in (
        'sh -c "' + first + "\\n" + later + '" && echo ok',
        'sh -c "' + first + "\\r\\n" + later + '" && echo ok',
        "sh -c '" + first + "\n" + later + "' && echo ok",
    )
    for levels in (0, 1, 2)
    if levels or line.startswith('sh -c "')
]


@pytest.mark.parametrize(("line", "secrets", "levels"), _COMMAND_ENDS_IN_JSON)
def test_an_escaped_line_end_in_a_json_string_ends_the_command_of_a_tool(
    line: str, secrets: tuple[str, ...], levels: int
) -> None:
    text = _in_json(line, levels)
    values = tuple(_as_json(secret, levels) for secret in secrets)
    for policy in (get_default_policy(), _content_policy()):
        assert _changes_besides(text, values, policy) == []
        on_main = redact(text, _without_command_rule(policy)).redacted
        assert not _parses(on_main, levels) or _parses(redact(text, policy).redacted, levels)


#: Windows paths with a backslash before ``n`` or ``r``: a written
#: backslash, not a line end.
_PATHS_WITH_N_OR_R: Final = (
    "C:\\new\\client.cnf",
    "C:\\Users\\rob\\notes.txt",
    "\\\\nas\\new\\run.cnf",
)
#: (line, its secret values, JSON levels): a path before the password of
#: each tool. In a JSON string the scan reads on past the path, so the
#: password after it stays hidden.
_PASSWORD_AFTER_PATH: Final[list[tuple[str, tuple[str, ...], int]]] = [
    (line, (PW,), levels)
    for path in _PATHS_WITH_N_OR_R
    for line in (
        f"mysql --defaults-file={path} -p{PW}",
        f"mariadb {path} -p{PW} db",
        f"sqlcmd -i {path} -S h -P {PW}",
        f"bcp t out {path} -P {PW}",
        f"ldapsearch -f {path} -w {PW} -x",
        "curl -o " + path + " -u admin" + ":" + PW + " https://h/x",
        f"smbclient //h/share -A {path} -U bob%{PW}",
    )
    for levels in (1, 2)
]


@pytest.mark.parametrize(("line", "secrets", "levels"), _PASSWORD_AFTER_PATH)
def test_a_backslash_before_n_or_r_in_a_json_string_does_not_end_the_command(
    line: str, secrets: tuple[str, ...], levels: int
) -> None:
    text = _in_json(line, levels)
    values = tuple(_as_json(secret, levels) for secret in secrets)
    for policy in (get_default_policy(), _content_policy()):
        assert _changes_besides(text, values, policy) == []
        assert _parses(redact(text, policy).redacted, levels)


#: Quoted words before the tool, in the same JSON string: they are opened
#: and closed before the tool, so they do not change its escape level.
_QUOTED_BEFORE_TOOL: Final = (
    'echo "ok"; ',
    'printf "%s" "a b" && ',
    'cd "C:\\work" ; ',
)
#: (line, its secret values, JSON levels): quoted text, then a path, then
#: the password of each tool.
_PASSWORD_AFTER_QUOTED_TEXT_AND_PATH: Final[list[tuple[str, tuple[str, ...], int]]] = [
    (prefix + line, secrets, levels)
    for prefix in _QUOTED_BEFORE_TOOL
    for line, secrets, levels in _PASSWORD_AFTER_PATH
]


@pytest.mark.parametrize(("line", "secrets", "levels"), _PASSWORD_AFTER_QUOTED_TEXT_AND_PATH)
def test_quoted_text_before_the_tool_does_not_change_its_escape_level(
    line: str, secrets: tuple[str, ...], levels: int
) -> None:
    text = _in_json(line, levels)
    values = tuple(_as_json(secret, levels) for secret in secrets)
    for policy in (get_default_policy(), _content_policy()):
        assert _changes_besides(text, values, policy) == []
        assert _parses(redact(text, policy).redacted, levels)


#: A Windows path in quotes: ``\\u`` there is no Unicode escape.
_WINDOWS_PATH: Final = 'copy "C:\\users\\public\\file.txt" backup'


@pytest.mark.parametrize(
    "text",
    [
        _WINDOWS_PATH,
        json.dumps({"msg": _WINDOWS_PATH, "ok": 1}),
        _json_string(json.dumps({"msg": _WINDOWS_PATH})),
        'note "C:\\u12\\x" and "\\u00zz" end',
    ],
)
def test_a_plain_backslash_stays_as_it_is(text: str) -> None:
    for policy in (get_default_policy(), _content_policy()):
        assert redact(text, policy).redacted == text
    assert scrub_content(text)[0] == text


def test_a_password_after_a_windows_path_is_hidden() -> None:
    line = f"{_WINDOWS_PATH} --password 'made up {PW}'"
    for text in (line, json.dumps({"msg": line, "ok": 1})):
        for out in (redact(text, get_default_policy()).redacted, scrub_content(text)[0]):
            assert PW not in out
            assert "made up" not in out
            assert "C:" in out
        if text.startswith("{"):
            assert json.loads(redact(text, get_default_policy()).redacted)["ok"] == 1


def test_a_value_the_api_key_rule_would_hide_stays_in_a_policy_with_only_this_rule() -> None:
    """Such a value is left to the api_key rule, also when that rule is not in the policy."""
    rules = tuple(r for r in get_default_policy().rules if r.pattern == "command_line_secret")
    policy = get_default_policy().model_copy(update={"rules": rules})
    line = "run --password=example-password next"
    assert redact(line, policy).redacted == line
    assert redact("run --password=ab12cd next", policy).redacted == f"run --password={_MARK} next"


#: Ordinary command lines with no secret in them.
_ORDINARY: Final[list[str]] = [
    *_KEPT,
    "kubectl get pods -n kube-system -o wide",
    "systemctl restart nginx.service && journalctl -u nginx -n 50",
    "tar -czf /tmp/backup.tgz /etc/nginx; ls -la /tmp",
    "psql -h db -U app -d appdb -c 'select 1'",
    "mysql -u root -h db -P 3306 appdb",
    "ssh -i ~/.ssh/id_ed25519 -p 2222 admin@host uptime",
    "curl -sS -H 'Accept: application/json' https://example.invalid/api/v1/x",
    'sh -c "echo hello; date"',
    "find / -name '*.log' -mtime +7 -print",
    "Server=db;Database=d;User ID=app;Encrypt=true",
    "rsync -avz --delete src/ dest/\nrsync done",
    "docker run --rm -e LOG_LEVEL=debug -p 8080:80 nginx:1.27",
    "don't stop: sh -c 'mysql --version'; echo 'ok'",
]


@pytest.mark.parametrize("line", _ORDINARY)
def test_a_line_without_a_secret_is_the_same_as_on_main(line: str) -> None:
    """The answer and the trace are byte for byte what main makes of the line."""
    from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY

    on_main = redact(line, _without_command_rule()).redacted
    assert redact(line, get_default_policy()).redacted == on_main
    trace_on_main = redact(line, _without_command_rule(_CONTENT_POLICY)).redacted
    assert redact(line, _CONTENT_POLICY).redacted == trace_on_main


_LONG_LINE: Final = 200_000


@pytest.mark.parametrize(
    "unit",
    [
        "mysql -p ",
        '--password " ',
        "--password '",
        "--password a'",
        "Server=a;Pwd=b'",
        '"mysql" -p',
        'A_KEY="x ',
        "A_KEY=",
        _PW_IS,
        _PASSWORD_IS + '"',
        "Pwd={",
        'curl -H "a;b" ',
        "curl a\\; ",
        "a/'b' ",
        'a/"b"',
        "Password=a ",
        "<password><![CDATA[",
        "<password>\n<![CDATA[x]]>\n",
        'sh -c "mysql -p',
        '"curl -u a:',
        "'--password ",
        'x"DB_PASSWORD=',
        "pw=a -b",
        "<password>",
        "a/b@",
        "sshpass -p ",
        "curl -u a:",
        "\\\\",
        "a",
        "/",
        "[REDACTED:",
        "Server=db;Pwd=[REDACTED:",
        # A short value, then a value the api_key rule hides: each value is
        # checked against one match of that rule only.
        "--password=ab token=abcdefghij ",
    ],
)
def test_a_200000_character_line_is_read_in_under_a_second(unit: str) -> None:
    text = unit * (_LONG_LINE // len(unit))
    started = time.perf_counter()
    redact(text, get_default_policy())
    assert time.perf_counter() - started < 1.0


# -- through the three operations ----------------------------------------------------

_LINE_FOR: Final[dict[str, str]] = {
    "k8s.logs": f"2026-11-15 job started: backup-agent --password {PW} --target s3",
    "linux.log.tail": f"Nov 15 agent[41]: exec mysqldump -u root -p{PW} appdb",
    "linux.journal.tail": f"sshpass -p {PW} ssh admin@host",
}


@dataclass
class _LinuxTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_SECRET_PATH = "meho/testing/linux/command-secret-test"
_LINUX_TARGET = _LinuxTarget("linux-command-secret", "linux-host.test.invalid", 22, _SECRET_PATH)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    with stub_ssh_vault_secrets({_SECRET_PATH: {"username": "root", "password": "made-up-ssh-1"}}):
        yield
    get_settings.cache_clear()


def _through_the_boundary(op: str, result: Any) -> Any:
    done = apply_connector_boundary_redaction(
        result, connector_id=op.split(".")[0], tenant=None, op=op
    )
    return done.redacted


def _proc(stdout: str) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.exit_status = 0
    return proc


@dataclass
class _K8sTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    id: object = None
    tenant_id: object = None


@pytest.mark.asyncio
async def test_k8s_logs_hides_a_password_on_a_command_line() -> None:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub"}}],
        }

    target = _K8sTarget("k8s-test", "k8s.test.invalid", 6443, "k8s/test", uuid4(), UUID(int=0))
    operator = Operator(
        sub="op-command-secret",
        name="Test",
        email=None,
        raw_jwt="op.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )
    pod = MagicMock()
    pod.metadata.name = "backup-agent-0"
    container = MagicMock()
    container.name = "agent"
    pod.spec.containers = [container]
    pod.spec.init_containers = []
    pod_list = MagicMock()
    pod_list.items = [pod]
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod = AsyncMock(return_value=pod_list)
    core_v1.read_namespaced_pod_log = AsyncMock(return_value=_LINE_FOR["k8s.logs"] + "\n")
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.ops_logs.client.CoreV1Api",
            return_value=core_v1,
        ),
    ):
        result = await k8s_logs(
            KubernetesConnector(kubeconfig_loader=_loader),
            target,
            operator,
            {"pod_name": "backup-agent", "namespace": "ops"},
        )

    shown = _through_the_boundary("k8s.logs", result)
    assert PW not in str(shown)
    assert "backup-agent --password" in str(shown)
    assert "--target s3" in str(shown)


@pytest.mark.asyncio
async def test_linux_log_tail_hides_a_password_on_a_command_line() -> None:
    path = "/var/log/agent.log"
    content = _LINE_FOR["linux.log.tail"] + "\n"
    connector = LinuxSshConnector()
    hex_path = (path + "\n").encode().hex()
    stdout = f"{hex_path}\n{len(content)}\n===MEHO_CONTENT===\n{content}"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as run:
        run.return_value = _proc(stdout)
        result = await connector.log_tail(_LINUX_TARGET, {"path": path})

    assert result["rows"], "the stub log must give rows"
    shown = _through_the_boundary("linux.log.tail", result)
    assert PW not in str(shown)
    assert "mysqldump -u root -p" in str(shown)
    assert "appdb" in str(shown)


@pytest.mark.asyncio
async def test_linux_journal_tail_hides_a_password_on_a_command_line() -> None:
    entry = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1700000000000000",
            "PRIORITY": "4",
            "MESSAGE": _LINE_FOR["linux.journal.tail"],
        }
    )
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as run:
        run.return_value = _proc(entry + "\n\nMEHO_JOURNAL_RC=0\n")
        result = await connector.journal_tail(_LINUX_TARGET, {})

    assert result["rows"], "the stub journal must give rows"
    shown = _through_the_boundary("linux.journal.tail", result)
    assert PW not in str(shown)
    assert "sshpass -p" in str(shown)
    assert "ssh admin@host" in str(shown)
