#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of build.sh against a stub govc: the build reads the VM's power
state and finds an existing VM without govc vm.info. vm.info also reads the
VM's host, and the lab login has no rights on the host, so the stub refuses
it the way vCenter does. No vCenter, no SSH."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APPLIANCE = Path(__file__).resolve().parents[1]
BUILD = APPLIANCE / "build.sh"
VERSION = "0.38.0"
VM = f"meho-appliance-{VERSION}"

STUB_GOVC = """#!/usr/bin/env bash
echo "$*" >>"$GOVC_LOG"
case $1 in
    vm.info) echo "govc: NoPermission" >&2; exit 1 ;;
    object.collect) cat "$GOVC_POWER_STATE" ;;
    find) cat "$GOVC_FOUND" ;;
esac
"""


class BuildWithoutVmInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "govc").write_text(STUB_GOVC)
        for tool in ("ssh", "scp", "ssh-keygen", "curl"):
            (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        self.log = self.root / "govc.log"
        self.power_state = self.root / "power-state"
        self.found = self.root / "found"
        self.found.write_text("")
        download = self.root / f"meho-linux-{VERSION}-amd64.tar"
        download.write_text("fake download\n")
        key = self.root / "build-key"
        key.write_text("fake key\n")
        (self.root / "build-key.pub").write_text("ssh-ed25519 fake\n")
        cache = self.root / "cache"
        cache.mkdir()
        base_image = cache / "base.ova"
        base_image.write_text("fake base image\n")
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GOVC_LOG": str(self.log),
            "GOVC_POWER_STATE": str(self.power_state),
            "GOVC_FOUND": str(self.found),
            "GOVC_URL": "https://vcenter.example.lab/sdk",
            "GOVC_USERNAME": "builder",
            "GOVC_PASSWORD": "fake",
            "GOVC_DATASTORE": "ds",
            "GOVC_FOLDER": "meho-appliance",
            "GOVC_RESOURCE_POOL": "pool",
            "GOVC_NETWORK": "net",
            "MEHO_DOWNLOAD": str(download),
            "MEHO_BUILD_HOSTNAME": "meho-build.example.lab",
            "MEHO_BUILD_ADDRESS": "10.0.0.10/24",
            "MEHO_BUILD_GATEWAY": "10.0.0.1",
            "MEHO_BUILD_DNS": "10.0.0.2",
            "MEHO_BUILD_SSH_KEY": str(key),
            "MEHO_BASE_IMAGE_URL": "https://images.example.invalid/base.ova",
            "MEHO_BASE_IMAGE_SHA256": hashlib.sha256(base_image.read_bytes()).hexdigest(),
            "MEHO_BASE_IMAGE_CACHE": str(cache),
        }

    def run_build(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BUILD), *options, VERSION, str(self.root / "out")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def govc_calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def test_the_export_reads_the_power_state_without_vm_info(self) -> None:
        self.power_state.write_text("poweredOn\n")

        result = self.run_build("--from-step", "5")

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("The build VM is poweredOn, not powered off.", result.stderr)
        self.assertEqual([f"object.collect -s {VM} runtime.powerState"], self.govc_calls())

    def test_an_existing_vm_is_found_without_vm_info(self) -> None:
        self.found.write_text(f"/dc/vm/meho-appliance/{VM}\n")

        result = self.run_build()

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn(f"The VM {VM} already exists in vCenter.", result.stderr)
        self.assertEqual([f"find -type m -name {VM}"], self.govc_calls())


if __name__ == "__main__":
    unittest.main()
