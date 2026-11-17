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
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

PW: Final = "made-up-pw-4711"
#: Built from pieces, so secret scanners do not flag the lines that use them.
_USER_PW: Final = "bob" + ":" + PW
_PASSWORD_IS: Final = "Pass" + "word="
_PWD_IS: Final = "P" + "wd="
_PW_IS: Final = "p" + "w="


def _login(user: str, password: str) -> str:
    return user + "/" + password


#: (line, text that must be gone, text that must stay)
_HIDDEN: Final[list[tuple[str, str, str]]] = [
    (f"run --password {PW} --user a", PW, "--password"),
    (f"run --passwd={PW} next", PW, "--passwd="),
    (f"run --pass {PW} --pwd {PW}", PW, "--pwd"),
    (f"run --token {PW} --secret {PW}", PW, "--secret"),
    (f"run --api-key {PW} --client-secret={PW}", PW, "--client-secret="),
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
    (f"sqlplus {_login('scott', PW)}@orcl as sysdba", PW, "scott/"),
    (f"rman target {_login('sys', PW)}@//dbhost:1521/svc", PW, "//dbhost:1521/svc"),
    (f"Server=db;{_PASSWORD_IS}{PW};Database=d", PW, "Server=db;"),
    (f"Server=db;{_PWD_IS}{PW};Database=d", PW, "Server=db;"),
    (f"Server=db;{_PW_IS}{PW}", PW, "Server=db;"),
    (f'Server=db;{_PASSWORD_IS}"{PW};x";Database=d', PW, "Server=db;"),
    (f"DB_PASSWORD={PW} run", PW, "DB_PASSWORD="),
    (f"APP_PASS={PW} APP_PWD={PW} APP_SECRET={PW}", PW, "APP_SECRET="),
    (f"API_TOKEN={PW} SIGNING_KEY={PW}", PW, "SIGNING_KEY="),
    (f"<config><password>{PW}</password><user>u</user></config>", PW, "<user>u</user>"),
    (f"<Secret><![CDATA[{PW}]]></Secret>", PW, "<![CDATA["),
    # A value joined from quoted parts, with an escaped space or quote.
    ("run --password 'made-up'\"-pw-4711\" tail", "made-up", "tail"),
    ("run --password made-up\\ pw\\ 4711 tail", "pw\\ 4711", "tail"),
    ('run --password "made-up\\"pw 4711" tail', "pw 4711", "tail"),
    # A quoted value that runs over a line break.
    ("run --password 'made-up\npw-4711' tail", "pw-4711", "tail"),
    ('mysql -p"made-up\npw-4711" db', "pw-4711", "db"),
    # A command quoted inside a longer command.
    (f'sh -c "mysql -p{PW}" && echo ok', PW, "echo ok"),
    # A ``;`` inside a quoted or escaped argument does not end the command.
    (f'curl -H "Type: text; charset=utf-8" -u {_USER_PW} h', PW, "charset"),
    (f"curl -H 'a; b' -H a\\;b --user {_USER_PW} https://h/x", PW, "'a; b'"),
    # A quoted argument with a space is one word, also before the option.
    (f"sshpass -P 'Enter password:' -p {PW} ssh host", PW, "ssh host"),
    # A login password with quoted, joined and escaped parts.
    (f"sqlplus {_login('scott', chr(34) + PW + chr(34))}@orcl", PW, "@orcl"),
    (f"sqlplus {_login('bob', repr(PW))}@service", PW, "@service"),
    (
        "sqlplus " + _login("scott", "\"made @/ up\"'pw'\\ 4711") + "@orcl as sysdba",
        "pw",
        "@orcl as sysdba",
    ),
    # A connection string value with spaces, or in braces with a ``;`` in it.
    (f"User ID=u;{_PASSWORD_IS}{PW} long phrase;Database=d", "long phrase", "User ID=u;"),
    (f"Server=db;{_PWD_IS}{PW} long phrase", "long phrase", "Server=db;"),
    (f"Server=db;{_PWD_IS}{{made-up;tail}};Database=d", "tail", "Server=db;"),
    (f"{_PW_IS}{PW} -suffix;Database=d", "-suffix", "Database=d"),
    # A quoted tool name: its options follow the closing quote.
    (f'"mysql" -p{PW} db', PW, '"mysql" -p'),
    (f"'/usr/bin/sshpass' -p {PW} ssh host", PW, "ssh host"),
    # A quote inside a value that is never closed is part of the value.
    (f"Server=db;{_PWD_IS}made'up;Database=d", "up", "Server=db;"),
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
]


@pytest.mark.parametrize(("line", "gone", "kept"), _HIDDEN)
def test_hidden_forms_lose_the_value_and_keep_the_words(line: str, gone: str, kept: str) -> None:
    out = redact(line, get_default_policy()).redacted
    assert gone not in out
    assert kept in out
    assert "[REDACTED:" in out


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

#: (line, the whole filter output): only the value goes, every other word stays.
_WHOLE_OUTPUT: Final[list[tuple[str, str]]] = [
    (
        f"run --{_PASSWORD_IS.lower()}{PW} --user alice --verbose",
        f"run --{_PASSWORD_IS.lower()}{_MARK} --user alice --verbose",
    ),
    (f"run -{_PASSWORD_IS}{PW} --user alice", f"run -{_PASSWORD_IS}{_MARK} --user alice"),
    (
        f"login {_PASSWORD_IS.lower()}{PW} user=alice; echo ok",
        f"login {_PASSWORD_IS.lower()}{_MARK} user=alice; echo ok",
    ),
    (f"Server=db;{_PASSWORD_IS}{PW} long phrase", f"Server=db;{_PASSWORD_IS}{_MARK}"),
    (f"{_PW_IS}{PW} long phrase;", f"{_PW_IS}{_MARK};"),
    # A ``pw=`` value with an option-like word, followed by a next field.
    (f"{_PW_IS}{PW} -suffix;Database=d", f"{_PW_IS}{_MARK};Database=d"),
    # A command option keeps the words after its value, also before a ``;``.
    (
        f"run --{_PASSWORD_IS.lower()}{PW} --user alice; mode=fast",
        f"run --{_PASSWORD_IS.lower()}{_MARK} --user alice; mode=fast",
    ),
    (
        f"run -{_PASSWORD_IS}{PW} --user alice; mode=fast",
        f"run -{_PASSWORD_IS}{_MARK} --user alice; mode=fast",
    ),
    (
        f"login {_PASSWORD_IS.lower()}{PW} --verbose; echo ok",
        f"login {_PASSWORD_IS.lower()}{_MARK} --verbose; echo ok",
    ),
    # Quoted, joined and escaped login password parts are hidden whole.
    (
        "sqlplus " + _login("scott", "\"made up\"'pw'") + "@orcl",
        "sqlplus " + _login("scott", _MARK) + "@orcl",
    ),
    (
        f"sqlplus {_login('bob', chr(34) + PW + chr(34))}@service",
        f"sqlplus {_login('bob', _MARK)}@service",
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
        f'sh -c "DB_PASSWORD={PW}"; echo "status ok"',
        f'sh -c "DB_PASSWORD={_MARK}"; echo "status ok"',
    ),
    (
        f'sh -c "sqlplus {_login("bob", PW)}@svc"; echo "status ok"',
        f'sh -c "sqlplus {_login("bob", _MARK)}@svc"; echo "status ok"',
    ),
    (
        f"don't run mysql -p{PW} it's bad",
        f"don't run mysql -p{_MARK} it's bad",
    ),
    # A quoted tool name is not a quoted command.
    (f'"mysql" -p{PW} db', f'"mysql" -p{_MARK} db'),
    (f"'/usr/bin/mysql' -p{PW} db; echo ok", f"'/usr/bin/mysql' -p{_MARK} db; echo ok"),
    # A quote inside a value that is never closed is part of the value.
    (f"Server=db;{_PW_IS}made'up x;Database=d", f"Server=db;{_PW_IS}{_MARK};Database=d"),
    ("run --password made'up --user a", f"run --password {_MARK} --user a"),
    ('sh -c "mysql -pmade\'up"; echo "ok"', f'sh -c "mysql -p{_MARK}"; echo "ok"'),
    # A CDATA section with white space or other text around it.
    (
        f"<password>\n<![CDATA[{PW}]]>\n</password>",
        f"<password>\n<![CDATA[{_MARK}]]>\n</password>",
    ),
    (
        f"<password> a <![CDATA[{PW}]]> b </password><user>u</user>",
        f"<password> {_MARK} <![CDATA[{_MARK}]]> {_MARK} </password><user>u</user>",
    ),
    (
        f"<token><![CDATA[{PW}]]><![CDATA[{PW}]]></token>",
        f"<token><![CDATA[{_MARK}]]><![CDATA[{_MARK}]]></token>",
    ),
]


@pytest.mark.parametrize(("line", "expected"), _WHOLE_OUTPUT)
def test_only_the_value_is_replaced(line: str, expected: str) -> None:
    assert redact(line, get_default_policy()).redacted == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # The api_key rule hides ``Pwd=...`` and ``Password=...`` to the end, as on main.
        (_PWD_IS, "Server=db;[REDACTED:api_key]"),
        (_PASSWORD_IS, "Server=db;[REDACTED:api_key]"),
        (_PW_IS, f"Server=db;{_PW_IS}{_MARK};Database=d"),
    ],
)
def test_a_quote_inside_a_connection_string_value_does_not_end_it(
    label: str, expected: str
) -> None:
    line = f"Server=db;{label}made'up;Database=d"
    assert [line[a:b] for a, b in COMMAND_SECRET.secret_spans(line)] == ["made'up"]
    assert redact(line, get_default_policy()).redacted == expected


def test_no_part_of_a_braced_connection_value_is_left() -> None:
    line = f"Server=db;{_PWD_IS}{{made-up;tail}};Database=d"
    out = redact(line, get_default_policy()).redacted
    assert "made-up" not in out
    assert "tail" not in out
    assert out.startswith("Server=db;")


@pytest.mark.parametrize(
    "line",
    [
        f"Server=db;{_PWD_IS}made up long phrase ;Database=d",
        f"{_PASSWORD_IS}made up long phrase;",
        f"{_PWD_IS}made up long phrase; ",
        f"{_PASSWORD_IS}made up long phrase;Database=d",
    ],
)
def test_a_connection_string_value_runs_to_its_semicolon(line: str) -> None:
    # The finder reports the value only. Behind it, the api_key rule still
    # hides ``Password=<placeholder>;...`` as it does without this rule.
    spans = COMMAND_SECRET.secret_spans(line)
    assert [line[a:b] for a, b in spans] == ["made up long phrase"]


@pytest.mark.parametrize(
    "opening, closing",
    [('"', '"'), ("'", "'"), ("{", "}")],
)
def test_a_quoted_connection_value_is_hidden_across_a_line_break(
    opening: str, closing: str
) -> None:
    line = f"Server=db;pw={opening}made\nup{closing};Database=d"
    assert [line[a:b] for a, b in COMMAND_SECRET.secret_spans(line)] == [
        f"{opening}made\nup{closing}"
    ]
    answer = redact(line, get_default_policy()).redacted
    trace, _ = scrub_content(line)
    for out in (answer, trace):
        assert "made" not in out
        assert f"up{closing}" not in out
        assert out.endswith(";Database=d")


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


def _shown(text: str) -> str:
    return _ANY_PLACEHOLDER.sub(" ", text)


def test_the_rule_only_hides_more() -> None:
    """No word that the policy without this rule hides is shown with it."""
    before_policy = _without_command_rule()
    texts = _texts_of_other_tests() + [line for line, _gone, _kept in _HIDDEN]
    texts += [line for line, _expected in _WHOLE_OUTPUT]
    texts += [line for line, _kept in _AFTER_APOSTROPHE]
    shown_now: dict[str, set[str]] = {}
    for text in texts:
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
    ],
)
def test_a_200000_character_line_is_read_in_under_a_second(unit: str) -> None:
    text = unit * (_LONG_LINE // len(unit))
    started = time.perf_counter()
    redact(text, get_default_policy())
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "unit",
    [
        "<password><![CDATA[",
        "<password>\n<![CDATA[x]]>\n<![CDATA[",
        "<password><![CDATA[a]]><password>",
        "<token> a ",
    ],
)
def test_an_800000_character_text_of_secret_elements_is_read_in_under_a_second(
    unit: str,
) -> None:
    # Each element opens a CDATA section that is never closed. A search for
    # its end that starts again for each element takes square time.
    text = unit * (800_000 // len(unit))
    started = time.perf_counter()
    spans = COMMAND_SECRET.secret_spans(text)
    assert time.perf_counter() - started < 1.0
    assert spans == []


@pytest.mark.parametrize(
    "unit",
    ["Pwd={a\n", "a;Password='a\n", 'a;pw="a\n', "Pwd={a}}\n", "x;Pwd={"],
)
def test_a_1600000_character_text_of_unclosed_connection_values_is_read_in_under_a_second(
    unit: str,
) -> None:
    # Each line opens a quoted or braced value that is never closed. A search
    # for its closing mark that runs to the end of the text takes square time.
    text = unit * (1_600_000 // len(unit))
    started = time.perf_counter()
    COMMAND_SECRET.secret_spans(text)
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
