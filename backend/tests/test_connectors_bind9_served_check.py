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
    soa_serial,
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


def test_soa_serial_edge_inputs() -> None:
    assert soa_serial(ZONE) == "2026110901"
    assert soa_serial("@ IN SOA . . 7 1 1 1 1\n") == "7"
    assert soa_serial("host IN A 192.0.2.1\n") is None
    assert soa_serial("") is None


def test_no_zone_file_means_no_check() -> None:
    assert build_served_serial_check({}, bind_root="/etc/bind") is None


async def test_apply_views_adds_the_served_check_only_for_zone_files() -> None:
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
        await bind9_config_apply_views(connector, _TARGET, {"files": {"v.conf": "view x {};\n"}})
    with_zone, without_zone = (c.kwargs["verify_command"] for c in atomic_mock.await_args_list)
    assert "named-checkconf > /dev/null &&" in with_zone
    assert "2026110901" in with_zone
    assert without_zone == "named-checkconf > /dev/null"


def _fake_bin(tmp_path: Path, served: dict[str, str]) -> dict[str, str]:
    """Fake ``named-checkconf`` and ``rndc`` that answer from *served*."""
    conf = tmp_path / "conf.txt"
    conf.write_text(CONF)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkconf = bin_dir / "named-checkconf"
    checkconf.write_text(f"#!/bin/sh\ncat {conf}\n")
    cases = "\n".join(f'  "{view}") echo "serial: {serial}";;' for view, serial in served.items())
    rndc = bin_dir / "rndc"
    rndc.write_text(f'#!/bin/sh\ncase "$4" in\n{cases}\n  *) exit 1;;\nesac\n')
    for tool in (checkconf, rndc):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _run_check(env: dict[str, str], expected: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
    ],
)
def test_served_check_compares_serial_per_view(
    tmp_path: Path, served: dict[str, str], ok: bool
) -> None:
    env = _fake_bin(tmp_path, served)
    done = _run_check(
        env, {"/etc/bind/zones/alice.db": "2026110901", "/etc/bind/zones/bob.db": "2026110901"}
    )
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok:
        assert "does not serve" in done.stdout
