# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the check that no install command holds a secret as an argument.

The check is ``scripts/ci/check_install_secrets.py``. It reads the install
guide and the install notes. Test values are made up.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_install_secrets.py"

_spec = importlib.util.spec_from_file_location("check_install_secrets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cis = importlib.util.module_from_spec(_spec)
sys.modules["check_install_secrets"] = cis
_spec.loader.exec_module(cis)

# The header name is split, so a secret scanner does not read the test as a leak.
_AUTH = "Authorization" + ":"
_VALUE = "stand-in" + "-value"

# Each command of the old install guide and notes that held a secret.
_BAD_COMMANDS = [
    "kubectl create secret generic meho-postgres -n meho \\\n"
    "  --from-literal=url='postgresql+asyncpg://<user>:<password>@<host>:5432/meho'",
    "kubectl create secret generic meho-postgres-app -n meho \\\n"
    "  --from-literal=url='postgresql+asyncpg://meho_app:" + _VALUE + "@<host>:5432/meho'",
    "kubectl create secret generic keycloak-db-admin \\\n"
    "  --from-literal=username=<admin-user> \\\n"
    "  --from-literal=password=<admin-password>",
    'kubectl create secret generic meho-company-login -n meho --from-literal=client_secret="$CLIENT_SECRET"',  # noqa: E501
    'kubectl config --kubeconfig=$KC set-credentials installer --token="$TOKEN"',
    "kubectl -n $NS create secret docker-registry registry-pull --docker-server=$REG_HOST "
    '--docker-username="$PULL_USER" --docker-password="$PULL_PASSWORD"',
    "kubectl -n <namespace> create secret generic <release>-core-console-admin \\\n"
    "  --from-literal=username=console-admin \\\n"
    "  --from-literal=password='stand-in-value'",
    'vault write auth/jwt/login role=meho-mcp jwt="$RUNNER_TOKEN"',
    "bao write auth/approle/login role_id=stand-in-role secret_id=stand-in-value",
    "vault login token=stand-in-value",
    'curl -s -H "' + _AUTH + ' Bearer $TOKEN" https://example.test/ready',
    "curl -s --header '" + _AUTH + " stand-in-value' https://example.test/ready",
    "curl -s -H " + _AUTH + "stand-in-value https://example.test/ready",
    'curl -s https://example.test/ready --data "x" -- "Bearer <token>"',
    "some-tool --client-secret=stand-in-value",
    "some-tool --password=stand-in-value",
    "some-tool --from-literal=api-key=stand-in-value",
    "some-tool --from-literal=url=postgresql://user:" + _VALUE + "@host/db",
]

# The new forms: a secret comes from a file or from standard input.
_GOOD_COMMANDS = [
    "printf '%s' \"$PW\" | kubectl create secret generic meho-x -n meho --from-file=password=/dev/stdin",  # noqa: E501
    "printf 'postgresql+asyncpg://<user>:%s@<host>:5432/meho' \"$DB_PASSWORD\" \\\n"
    "  | kubectl create secret generic meho-postgres -n meho --from-file=url=/dev/stdin",
    "printf '%s' \"$PW\" \\\n"
    "  | kubectl create secret generic keycloak-db-admin \\\n"
    "      --from-literal=username=<admin-user> --from-file=password=/dev/stdin",
    "printf '%s' \"$RUNNER_TOKEN\" | vault write auth/jwt/login role=meho-mcp jwt=-",
    "vault write auth/jwt/login role=meho-mcp jwt=@token.txt",
    "printf '%s' \"$PUSH_PASSWORD\" | crane auth login registry.example.com -u <user> --password-stdin",  # noqa: E501
    "kubectl -n meho get secret x -o jsonpath='{.data.token}' \\\n"
    "  | base64 -d | sed 's/^/Authorization: Bearer /' \\\n"
    "  | curl -s -H @- https://<your-backplane>/ready",
    'kubectl config --kubeconfig=$KC set-cluster vks --server="$SERVER"',
    "kubectl create secret generic x --from-literal=username=console-admin --from-file=password=/dev/stdin",  # noqa: E501
    'printf \'{"auths":{"%s":{"auth":"%s"}}}\' "$REG_HOST" \\\n'
    '  "$(printf \'%s:%s\' "$U" "$P" | base64 | tr -d \'\\n\')" \\\n'
    "  | kubectl create secret generic registry-pull --type=kubernetes.io/dockerconfigjson \\\n"
    "      --from-file=.dockerconfigjson=/dev/stdin",
    "read -rs CLIENT_SECRET",
]


def _page(command: str, language: str = "bash") -> str:
    return f"Text.\n\n```{language}\n{command}\n```\n\nMore text.\n"


@pytest.mark.parametrize("command", _BAD_COMMANDS)
def test_a_secret_argument_fails_in_a_page(command: str) -> None:
    assert cis.check_page(_page(command)), command


@pytest.mark.parametrize("command", _BAD_COMMANDS)
def test_a_secret_argument_fails_in_the_notes(command: str) -> None:
    assert cis.check_notes("  " + command.replace("\n", "\n  ")), command


@pytest.mark.parametrize("command", _GOOD_COMMANDS)
def test_a_file_or_standard_input_form_passes(command: str) -> None:
    assert cis.check_page(_page(command)) == []
    assert cis.check_notes("  " + command.replace("\n", "\n  ")) == []


@pytest.mark.parametrize("language", ["bash", "sh", "zsh", "shell", "console", ""])
def test_each_command_language_is_read(language: str) -> None:
    assert cis.check_page(_page("some-tool --token=stand-in-value", language))


def test_other_languages_are_not_read() -> None:
    assert cis.check_page(_page("token: --token=stand-in-value", "yaml")) == []
    assert cis.check_page("Plain text with --token=stand-in-value.\n") == []


def test_a_sql_password_fails_only_in_a_sql_block() -> None:
    sql = "CREATE ROLE meho_app LOGIN NOSUPERUSER PASSWORD 'stand-in-value';"
    assert cis.check_page(_page(sql, "sql"))
    assert cis.check_page(_page("CREATE ROLE meho_app LOGIN NOSUPERUSER;", "sql")) == []


def test_an_indented_fence_is_read() -> None:
    page = "6. Step:\n\n    ```bash\n    some-tool --password=stand-in-value\n    ```\n"
    found = cis.check_page(page)
    assert [line for line, _ in found] == [4]


def test_a_continued_command_reports_its_first_line() -> None:
    page = _page("kubectl create secret generic x \\\n  --from-literal=password=stand-in-value")
    assert [line for line, _ in cis.check_page(page)] == [4]


def test_the_install_guide_and_notes_of_the_repo_pass() -> None:
    assert cis.check_repo(_REPO_ROOT) == []


def test_the_command_exits_1_on_a_bad_page(tmp_path: Path) -> None:
    pages = tmp_path / "docs-site" / "install"
    pages.mkdir(parents=True)
    (pages / "page.md").write_text(_page("some-tool --token=stand-in-value"), encoding="utf-8")
    notes = tmp_path / "deploy" / "charts" / "meho" / "templates"
    notes.mkdir(parents=True)
    (notes / "NOTES.txt").write_text("  nothing here\n", encoding="utf-8")
    assert cis.main(["--root", str(tmp_path)]) == 1
    (pages / "page.md").write_text(_page("some-tool --token-file=/dev/stdin"), encoding="utf-8")
    assert cis.main(["--root", str(tmp_path)]) == 0
