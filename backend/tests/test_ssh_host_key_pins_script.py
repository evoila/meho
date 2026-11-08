# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for ``scripts/acceptance/ssh-host-key-pins.sh``.

The script runs before an upgrade to v0.34.0 or later. It lists the SSH
targets and fails when a target's secret has no ``known_hosts`` entry and
no ``known_hosts_insecure`` opt-out, because the SSH connector refuses
such a target after the upgrade.

The tests run the real script with fake ``curl`` and ``vault`` commands
first on ``PATH``. The fake ``curl`` answers each page of the target list
from a file; the fake ``vault`` answers each secret path from a file. No
backplane and no Vault are contacted. ``jq`` is the only real tool the
script needs; where it is missing the tests skip.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import meho_backplane.connectors as connectors_pkg
from meho_backplane.connectors.adapters.ssh import SshConnector

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "acceptance" / "ssh-host-key-pins.sh"

_needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the script runs jq"
)

# Answers each page from <dir>/page-<cursor or "first">.json. It reads the
# header from stdin, as the script sends the token there, and records it.
_FAKE_CURL = """#!/usr/bin/env bash
cursor=first
for arg in "$@"; do
    case "$arg" in cursor=*) cursor="${arg#cursor=}" ;; esac
done
cat > "$FAKE_DIR/curl-stdin"
printf '%s\\n' "$@" > "$FAKE_DIR/curl-args"
cat "$FAKE_DIR/page-$cursor.json"
"""

# Answers ``vault kv get -format=json -mount=<m> <path>`` from
# <dir>/secret-<mount>-<path with / as _>.json; a missing file fails.
_FAKE_VAULT = """#!/usr/bin/env bash
mount="" path=""
for arg in "$@"; do
    case "$arg" in -mount=*) mount="${arg#-mount=}" ;; -*|kv|get) ;; *) path="$arg" ;; esac
done
file="$FAKE_DIR/secret-$mount-${path//\\//_}.json"
[ -f "$file" ] || { echo "no secret" >&2; exit 2; }
cat "$file"
"""


def _target(
    name: str, product: str, secret_ref: str | None, field_map: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "product": product,
        "secret_ref": secret_ref,
        "secret_field_map": field_map or {},
    }


class _Fake:
    def __init__(self, root: Path) -> None:
        self.dir = root / "fake"
        self.bin = root / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name, body in (("curl", _FAKE_CURL), ("vault", _FAKE_VAULT)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

    def page(
        self, items: list[dict[str, Any]], cursor: str = "first", nxt: str | None = None
    ) -> None:
        body = {"items": items, "next_cursor": nxt}
        (self.dir / f"page-{cursor}.json").write_text(json.dumps(body))

    def secret(self, path: str, data: dict[str, Any], mount: str = "secret") -> None:
        name = path.replace("/", "_")
        (self.dir / f"secret-{mount}-{name}.json").write_text(json.dumps({"data": {"data": data}}))

    def run(self, *args: str, token: str | None = "tok-123") -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DIR": str(self.dir),
        }
        if token is not None:
            env["MEHO_ACCESS_TOKEN"] = token
        return subprocess.run(
            ["bash", str(_SCRIPT), "--backplane", "https://meho.example.com", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


@pytest.fixture
def fake(tmp_path: Path) -> _Fake:
    return _Fake(tmp_path)


@_needs_jq
def test_pinned_targets_pass(fake: _Fake) -> None:
    fake.page(
        [
            _target("web-1", "linux", "targets/web-1"),
            _target("dns-1", "bind9", "vault:targets/dns-1"),
        ]
    )
    fake.secret("targets/web-1", {"username": "root", "known_hosts": "web-1 ssh-ed25519 AAAA"})
    fake.secret("targets/dns-1", {"known_hosts": "dns-1 ssh-ed25519 AAAA\n"})

    result = fake.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ok]   web-1 (linux): host key pinned" in result.stdout
    assert "[ok]   dns-1 (bind9): host key pinned" in result.stdout
    assert "Checked 2 SSH target(s)." in result.stdout


@_needs_jq
@pytest.mark.parametrize("flag", [True, "true", " Yes ", "1", "on"])
def test_explicit_opt_out_warns_but_passes(fake: _Fake, flag: object) -> None:
    fake.page([_target("fw-1", "pfsense", "targets/fw-1")])
    fake.secret("targets/fw-1", {"known_hosts_insecure": flag})

    result = fake.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[WARN] fw-1 (pfsense): known_hosts_insecure is set" in result.stdout


@_needs_jq
@pytest.mark.parametrize(
    "secret",
    [
        {"username": "root", "password": "x"},
        {"known_hosts": "   "},
        {"known_hosts_insecure": False},
        {"known_hosts_insecure": "false"},
        {"known_hosts_insecure": "nope"},
    ],
)
def test_missing_pin_fails(fake: _Fake, secret: dict[str, Any]) -> None:
    fake.page([_target("node-1", "rke2", "targets/node-1")])
    fake.secret("targets/node-1", secret)

    result = fake.run()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] node-1 (rke2): no known_hosts entry and no known_hosts_insecure" in result.stdout


@_needs_jq
def test_unreadable_secret_fails(fake: _Fake) -> None:
    fake.page([_target("node-2", "linux", "targets/gone")])

    result = fake.run()

    assert result.returncode == 1
    assert "[FAIL] node-2 (linux): cannot read the secret from Vault" in result.stdout


@_needs_jq
def test_mapped_pin_passes(fake: _Fake) -> None:
    fake.page([_target("web-1", "linux", "targets/web-1", {"known_hosts": "host_pin"})])
    fake.secret("targets/web-1", {"host_pin": "web-1 ssh-ed25519 AAAA"})

    result = fake.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ok]   web-1 (linux): host key pinned" in result.stdout


@_needs_jq
def test_mapped_opt_out_warns_but_passes(fake: _Fake) -> None:
    fake.page([_target("fw-1", "pfsense", "targets/fw-1", {"known_hosts_insecure": "no_check"})])
    fake.secret("targets/fw-1", {"no_check": "true"})

    result = fake.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[WARN] fw-1 (pfsense): known_hosts_insecure is set" in result.stdout


@_needs_jq
@pytest.mark.parametrize("field", ["known_hosts", "known_hosts_insecure"])
def test_missing_mapped_field_fails(fake: _Fake, field: str) -> None:
    # The secret has the field under its own name, but the map points the
    # connector at another field, so the connector does not see it.
    fake.page([_target("node-1", "rke2", "targets/node-1", {field: "gone_field"})])
    value = "node-1 ssh-ed25519 AAAA" if field == "known_hosts" else True
    fake.secret("targets/node-1", {field: value})

    result = fake.run()

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "[FAIL] node-1 (rke2): the target's secret_field_map points to a field "
        "the secret does not have (gone_field)"
    ) in result.stdout
    assert "AAAA" not in result.stdout


@_needs_jq
def test_missing_mapped_pin_with_opt_out_passes(fake: _Fake) -> None:
    fake.page([_target("node-1", "rke2", "targets/node-1", {"known_hosts": "gone_field"})])
    fake.secret("targets/node-1", {"known_hosts_insecure": True})

    result = fake.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[WARN] node-1 (rke2): known_hosts_insecure is set" in result.stdout


@_needs_jq
def test_mixed_targets_fail_and_only_ssh_products_are_checked(fake: _Fake) -> None:
    fake.page(
        [
            _target("vc-1", "vmware", "targets/vc-1"),
            _target("web-1", "linux", "targets/web-1"),
            _target("box-1", "linux", None),
            _target("gcp-1", "winsrv", "gsm:proj/secret"),
        ],
        nxt="web-1",
    )
    fake.page([_target("dc-1", "msad", "targets/dc-1")], cursor="web-1")
    fake.secret("targets/web-1", {"known_hosts": "web-1 ssh-ed25519 AAAA"})
    fake.secret("targets/dc-1", {"password": "x"})

    result = fake.run()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "vc-1" not in result.stdout
    assert "[ok]   web-1 (linux)" in result.stdout
    assert "[WARN] box-1 (linux): no secret_ref" in result.stdout
    assert "[WARN] gcp-1 (winsrv): the secret is in the 'gsm' store" in result.stdout
    assert "[FAIL] dc-1 (msad)" in result.stdout
    assert "Checked 4 SSH target(s)." in result.stdout


@_needs_jq
def test_extra_product_and_mount(fake: _Fake) -> None:
    fake.page([_target("sw-1", "acme-switch", "targets/sw-1")])
    fake.secret("targets/sw-1", {"known_hosts": "sw-1 ssh-rsa AAAA"}, mount="kv")

    skipped = fake.run()
    checked = fake.run("--product", "acme-switch", "--mount", "kv")

    assert skipped.returncode == 0
    assert "Checked 0 SSH target(s)." in skipped.stdout
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "[ok]   sw-1 (acme-switch): host key pinned" in checked.stdout


@_needs_jq
def test_token_goes_to_stdin_not_args_or_output(fake: _Fake) -> None:
    fake.page([])

    result = fake.run(token="secret-token-value")

    assert result.returncode == 0
    assert "secret-token-value" not in result.stdout + result.stderr
    assert "secret-token-value" not in (fake.dir / "curl-args").read_text()
    assert (fake.dir / "curl-stdin").read_text() == "Authorization: Bearer secret-token-value\n"


def test_missing_token_is_a_usage_error(fake: _Fake) -> None:
    result = fake.run(token=None)

    assert result.returncode == 2
    assert "MEHO_ACCESS_TOKEN is not set" in result.stderr


def _ssh_subclasses(cls: type[SshConnector]) -> Iterator[type[SshConnector]]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _ssh_subclasses(sub)


def _built_in_ssh_products() -> set[str]:
    """Return the products of the SSH connectors that ship in the connectors package.

    It imports every connector module first, so the answer does not depend on
    what else the test run imported. Other test files define their own
    ``SshConnector`` subclasses; those live outside the package and are left out.
    """
    for mod in pkgutil.walk_packages(connectors_pkg.__path__, connectors_pkg.__name__ + "."):
        if mod.name.endswith(".connector"):
            importlib.import_module(mod.name)
    prefix = connectors_pkg.__name__ + "."
    return {
        sub.product for sub in _ssh_subclasses(SshConnector) if sub.__module__.startswith(prefix)
    }


def test_product_list_matches_ssh_connectors() -> None:
    """The script's list names every built-in connector that uses SSH."""

    class _TestOnlySsh(SshConnector):  # a test-only subclass must not count
        product = "test-only-ssh"

    products = _built_in_ssh_products()
    assert "test-only-ssh" not in products

    match = re.search(r"^SSH_PRODUCTS=\(([^)]*)\)$", _SCRIPT.read_text(), re.MULTILINE)
    assert match is not None
    assert set(match.group(1).split()) == products
