# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake machine with MEHO installed, for the tests of the update.

UpdateTestCase runs a first install of MEHO 0.36.0 on the fake machine of
harness.py, makes the machine look installed (rke2-server runs, Helm has the
release meho) and turns the download into the download of MEHO 0.37.0. A
test then runs install.sh again, which is the update.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from harness import LINUX_SERVER, FakeMachine, InstallTestCase

INSTALL_RECORD = "/etc/meho/install.json"
PLACE_VALUES = "/etc/meho/values-place.yaml"
RKE2_CONFIG = "/etc/rancher/rke2/config.yaml"
FIREWALL_FILES = (
    "/etc/meho/firewall.nft",
    "/etc/systemd/system/meho-firewall.service",
    "/etc/systemd/system/rke2-server.service.d/meho-firewall.conf",
)
ONE_TIME_FILES = ("/var/lib/meho/first-login-passwords", "/var/lib/meho/unseal-key")
VOLUME_FILE = "/var/lib/meho/volumes/pvc-1_meho_data-meho-postgres-0/PG_VERSION"

INSTALLER = "rke2-installer "
HELM_UPGRADE = "helm upgrade --install meho"
RKE2_RESTART = "systemctl restart --no-block rke2-server.service"
UPDATE_DATE = "2026-11-20T08:00:00Z"
NEW_VERSION = "0.37.0"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_update_lib(
    machine: FakeMachine, snippet: str, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run bash with the files of lib/ that the update uses sourced, on the
    fake machine. path replaces the stub PATH, for a test with real tools."""
    sources = "".join(
        f'source "{LINUX_SERVER}/lib/{name}.sh"\n'
        for name in ("common", "net", "checks", "core", "rke2", "update")
    )
    env = {
        "PATH": path or f"{machine.stubs}:{machine.tools}",
        "MEHO_TEST_ROOT": str(machine.root),
        "MEHO_FAKE": str(machine.fake),
        "HOME": str(machine.home),
        "LC_ALL": "C",
    }
    return subprocess.run(
        [str(machine.tools / "bash"), "-c", "set -Eeuo pipefail\n" + sources + snippet],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


class UpdateTestCase(InstallTestCase):
    """MEHO 0.36.0 with RKE2 v1.37.1 is installed, and the download holds
    MEHO 0.37.0 with the same RKE2."""

    def setUp(self) -> None:
        super().setUp()
        self.assert_succeeds(self.machine.run())
        self.machine.mark_installed()
        self.machine.host_file(VOLUME_FILE, "17\n")
        self.machine.set_download_version(NEW_VERSION)
        self.machine.add_stub("date", f"echo {UPDATE_DATE}")
        self.machine.answer("calls.log", "")

    def snapshot(self) -> dict[str, str]:
        """The checksum of every file below the fake root."""
        root = self.machine.root
        return {
            path.relative_to(root).as_posix(): sha256(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def host_text(self, path: str) -> str:
        return self.machine.host_path(path).read_text()

    def assert_stopped_before_any_change(
        self, result: subprocess.CompletedProcess[str], before: dict[str, str], *fragments: str
    ) -> str:
        """One [FAIL] line with the fragments, no file changed, and none of
        the commands of the update ran."""
        line = self.assert_fails_once(result, *fragments)
        self.assertEqual(before, self.snapshot())
        calls = self.machine.calls()
        for command in (INSTALLER, HELM_UPGRADE, RKE2_RESTART, "apt-get", "useradd"):
            with self.subTest(command=command):
                self.assertNotIn(command, calls)
        return line
