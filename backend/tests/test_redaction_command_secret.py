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
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

PW: Final = "made-up-pw-4711"

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
    (f"curl -u bob:{PW} https://h/x", PW, "bob:"),  # gitleaks:allow -- made-up value
    (f"curl -ubob:{PW} https://h/x", PW, "https://h/x"),  # gitleaks:allow -- made-up value
    (f"curl --user bob:{PW} https://h/x", PW, "--user bob:"),  # gitleaks:allow -- made-up value
    (f"smbclient //h/share -U bob%{PW} -c ls", PW, "bob%"),
    (f"sqlplus scott/{PW}@orcl as sysdba", PW, "scott/"),  # trufflehog:ignore
    (f"rman target sys/{PW}@//dbhost:1521/svc", PW, "//dbhost:1521/svc"),  # trufflehog:ignore
    ("Server=db;Password=" + PW + ";Database=d", PW, "Database=d"),
    ("Server=db;Pwd=" + PW + ";Database=d", PW, "Database=d"),
    (f"Server=db;pw={PW}", PW, "Server=db;"),  # trufflehog:ignore
    (f'Server=db;Password="{PW};x";Database=d', PW, "Database=d"),  # trufflehog:ignore
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
    "PWD=/home/user",  # trufflehog:ignore
    "SORT_ORDER=asc",
    "registry.example.com/app@sha256:0123456789abcdef",
    "uses: actions/checkout@v4",
    "images/logo@2x.png",
    "Server=db;Database=d",  # trufflehog:ignore
    "Password=;Server=db",  # trufflehog:ignore
    "Server=db;Password=",  # trufflehog:ignore
    "<user>u</user><password></password><password/>",
    "the password prompt was shown",
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


def test_spans_never_overlap_and_are_left_to_right() -> None:
    text = (
        f"mysql -p{PW} --password {PW} DB_PASSWORD={PW}; "  # trufflehog:ignore
        f"Server=a;Password={PW};"  # trufflehog:ignore
    )
    spans = COMMAND_SECRET.secret_spans(text)
    assert spans == sorted(spans)
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(spans))
    assert all(text[a:b] and PW in text[a:b] for a, b in spans)


_LONG_LINE: Final = 200_000


@pytest.mark.parametrize(
    "unit",
    [
        "mysql -p ",
        '--password " ',
        "--password '",
        'A_KEY="x ',
        "A_KEY=",
        "pw=",  # trufflehog:ignore
        'Password="',  # trufflehog:ignore
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
