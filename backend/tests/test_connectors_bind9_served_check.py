# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the staged-file time and the served-serial check of apply_views."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.connectors.bind9._atomic import AtomicApplyResult
from meho_backplane.connectors.bind9._served_check import (
    build_served_serial_check,
    is_zone_file,
)
from meho_backplane.connectors.bind9.connector import Bind9Connector
from meho_backplane.connectors.bind9.ops_config import (
    bind9_config_apply_views,
    pack_views_tar,
)
from tests.test_connectors_bind9_config import (  # noqa: F401
    _TARGET,
    _fingerprint_debian_default,
    _required_settings_env,
    _vault_secrets,
)

ZONE = "$TTL 3600\n@ IN SOA ns. admin. (\n  ; serial\n  2026110901 3600 600 86400 300 )\n"
CONF = textwrap.dedent(
    """\
    options {
    \tdirectory "/etc/bind";
    };
    view "alice" {
    \tzone "example.test" {
    \t\ttype master;
    \t\tfile "/etc/bind/zones/alice.db";
    \t};
    };
    view "bob" {
    \tzone "example.test" {
    \t\ttype master;
    \t\tfile "zones/bob.db";
    \t};
    };
    """
)


def test_staged_files_carry_the_current_time() -> None:
    tar_bytes = pack_views_tar({"zones/a.db": ZONE}, mtime=1_800_000_000)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        assert [m.mtime for m in tar.getmembers()] == [1_800_000_000]
    default = pack_views_tar({"zones/a.db": ZONE})
    with tarfile.open(fileobj=io.BytesIO(default), mode="r:gz") as tar:
        assert tar.getmembers()[0].mtime > 1_700_000_000


@pytest.mark.parametrize(
    ("path", "content", "zone"),
    [
        ("zones/a.db", ZONE, True),
        ("zones/a.db", "@ IN SOA . . 7 1 1 1 1\n", True),
        # "(" before the server names is valid zone file syntax.
        ("zones/a.db", "$TTL 1\n@ IN SOA ( ns. admin. 2026110902 1 1 1 1 )\n", True),
        ("zones/a.db", "$ORIGIN example.test.\n@ 60 soa ns admin 9 1 1 1 1\n", True),
        # A SOA only in a comment is not a SOA record.
        ("zones/a.db", "; @ IN SOA ns. admin. 1 1 1 1 1\nhost IN A 192.0.2.1\n", False),
        ("zones/a.db", "host IN A 192.0.2.1\n", False),
        ("zones/a.db", "", False),
        # Content the tokenizer cannot read still gets the server-side check.
        ("zones/a.db", "@ IN SOA ( ns. admin. 1\n", True),
        # A config file is never a zone file.
        ("named.conf.local", "// SOA\nview x {};\n", False),
    ],
)
def test_is_zone_file_reads_dns_syntax(path: str, content: str, zone: bool) -> None:
    assert is_zone_file(path, content) is zone


def test_no_staged_file_means_no_check() -> None:
    assert build_served_serial_check({}, bind_root="/etc/bind") is None
    # The name or content guess never skips the check: the server decides
    # which staged files a configured zone uses.
    assert build_served_serial_check({"/etc/bind/v.conf": False}, bind_root="/etc/bind")


async def test_apply_views_adds_the_served_check_for_every_staged_file() -> None:
    connector = Bind9Connector()
    result = AtomicApplyResult(state_before="", state_after="", audit_slice_path="/etc/bind/v.conf")
    atomic_mock = AsyncMock(return_value=result)
    with (
        patch.object(
            connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
        ),
        patch("meho_backplane.connectors.bind9.ops_config.atomic_apply", atomic_mock),
    ):
        await bind9_config_apply_views(
            connector, _TARGET, {"files": {"v.conf": "view x {};\n", "zones/a.db": ZONE}}
        )
        await bind9_config_apply_views(connector, _TARGET, {"files": {"zones/a.db": ""}})
    with_zone, empty_zone = (c.kwargs["verify_command"] for c in atomic_mock.await_args_list)
    assert with_zone.startswith("named-checkconf > /dev/null && printf %s ")
    assert '"/etc/bind/zones/a.db": true' in with_zone
    # An empty replacement of a zone file is still checked on the server.
    assert empty_zone.startswith("named-checkconf > /dev/null && printf %s ")
    assert '"/etc/bind/zones/a.db": false' in empty_zone


async def _verify_command(params: dict[str, object]) -> str:
    connector = Bind9Connector()
    result = AtomicApplyResult(state_before="", state_after="", audit_slice_path="/etc/bind/v.conf")
    atomic_mock = AsyncMock(return_value=result)
    with (
        patch.object(
            connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
        ),
        patch("meho_backplane.connectors.bind9.ops_config.atomic_apply", atomic_mock),
    ):
        await bind9_config_apply_views(connector, _TARGET, params)
    return str(atomic_mock.await_args.kwargs["verify_command"])


async def test_verify_source_digs_from_the_view_address_after_the_serial_check() -> None:
    files = {"v.conf": "view x {};\n", "zones/a.db": ZONE}
    command = await _verify_command(
        {"files": files, "verify_fqdn": "www.example.test", "verify_source": "192.0.2.53"}
    )
    # The serial check runs before the dig, so the dig cannot hide it.
    assert command.index("python3 -") < command.index("dig ")
    assert command.endswith(
        '[ -n "$(dig -b 192.0.2.53 @192.0.2.53 www.example.test +short 2>/dev/null)" ]'
    )
    default = await _verify_command({"files": files, "verify_fqdn": "www.example.test"})
    assert "dig @localhost www.example.test +short" in default


@pytest.mark.parametrize(
    "params",
    [
        {"verify_fqdn": "www.example.test", "verify_source": "not-an-address"},
        {"verify_fqdn": "www.example.test", "verify_source": "192.0.2.53; reboot"},
        {"verify_source": "192.0.2.53"},
    ],
)
async def test_verify_source_rejects_bad_input(params: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="verify_source"):
        await _verify_command({"files": {"v.conf": "view x {};\n"}, **params})


def _fake_bin(
    tmp_path: Path, served: dict[str, str], file_serial: str = "2026110901", conf_text: str = CONF
) -> dict[str, str]:
    """Fake ``named-checkconf``, ``named-checkzone`` and ``rndc``.

    ``rndc`` answers from *served* (view -> serial). ``named-checkzone``
    reports *file_serial* for every file, or fails when it is empty.
    """
    conf = tmp_path / "conf.txt"
    conf.write_text(conf_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkconf = bin_dir / "named-checkconf"
    checkconf.write_text(f"#!/bin/sh\ncat {conf}\n")
    cases = "\n".join(f'  "{view}") echo "serial: {serial}";;' for view, serial in served.items())
    rndc = bin_dir / "rndc"
    rndc.write_text(f'#!/bin/sh\ncase "$4" in\n{cases}\n  *) exit 1;;\nesac\n')
    checkzone = bin_dir / "named-checkzone"
    if file_serial:
        checkzone.write_text(
            f'#!/bin/sh\necho "zone $3/IN: loaded serial {file_serial}"\necho OK\n'
        )
    else:
        checkzone.write_text('#!/bin/sh\necho "zone $3/IN: bad dotted quad"\nexit 1\n')
    for tool in (checkconf, rndc, checkzone):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _run_check(env: dict[str, str], expected: dict[str, bool]) -> subprocess.CompletedProcess[str]:
    command = build_served_serial_check(expected, bind_root="/etc/bind")
    assert command is not None
    # Keep the retry loop fast: the script sleeps one second between tries.
    command = command.replace(
        "| python3 -", "| sed 's/^import time$/import time; time.sleep = int/' | python3 -"
    )
    return subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("served", "ok"),
    [
        ({"alice": "2026110901", "bob": "2026110901"}, True),
        # Old serial in one view: the case of the bug. Must fail.
        ({"alice": "2026110901", "bob": "2026110801"}, False),
        # The server cannot answer for a zone: unknown is not ok.
        ({"alice": "2026110901"}, False),
        # Serials are numbers: a leading zero is the same serial.
        ({"alice": "02026110901", "bob": "2026110901"}, True),
    ],
)
def test_served_check_compares_serial_per_view(
    tmp_path: Path, served: dict[str, str], ok: bool
) -> None:
    env = _fake_bin(tmp_path, served)
    done = _run_check(env, {"/etc/bind/zones/alice.db": True, "/etc/bind/zones/bob.db": True})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok:
        assert "does not serve" in done.stdout


def test_served_check_fails_for_a_zone_file_no_zone_uses(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, {"alice": "2026110901", "bob": "2026110901"})
    done = _run_check(
        env,
        {
            "/etc/bind/zones/alice.db": True,
            "/etc/bind/zones/carol.db": True,
            "/etc/bind/named.conf.local": False,
        },
    )
    assert done.returncode == 1
    assert "/etc/bind/zones/carol.db: no configured zone uses this file" in done.stdout
    # A config file that no zone uses is fine.
    assert "named.conf.local" not in done.stdout


def test_served_check_fails_when_checkzone_cannot_read_the_file(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, {"alice": "2026110901", "bob": "2026110901"}, file_serial="")
    done = _run_check(env, {"/etc/bind/zones/alice.db": True})
    assert done.returncode == 1
    assert "named-checkzone cannot read /etc/bind/zones/alice.db" in done.stdout


def test_served_check_fails_for_an_empty_configured_zone_file(tmp_path: Path) -> None:
    # An empty file has no SOA, so it does not look like a zone file. A
    # configured zone uses it, so the server check must still fail.
    assert is_zone_file("zones/alice.db", "") is False
    env = _fake_bin(tmp_path, {"alice": "2026110901", "bob": "2026110901"}, file_serial="")
    done = _run_check(env, {"/etc/bind/zones/alice.db": False})
    assert done.returncode == 1
    assert "named-checkzone cannot read /etc/bind/zones/alice.db" in done.stdout


@pytest.mark.parametrize(("served", "ok"), [("2026110901", True), ("2026110801", False)])
def test_served_check_checks_a_configured_zone_file_named_conf(
    tmp_path: Path, served: str, ok: bool
) -> None:
    # A zone file named like a config file is not guessed as a zone file,
    # but a configured zone uses it, so its serial is checked.
    assert is_zone_file("zones/carol.conf", ZONE) is False
    conf_text = CONF + 'view "carol" {\n\tzone "example.test" {\n\t\ttype master;\n'
    conf_text += '\t\tfile "zones/carol.conf";\n\t};\n};\n'
    env = _fake_bin(tmp_path, {"carol": served}, conf_text=conf_text)
    done = _run_check(env, {"/etc/bind/zones/carol.conf": False})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok:
        assert "zone example.test (view carol): file has serial 2026110901" in done.stdout
