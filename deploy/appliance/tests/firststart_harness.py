# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake appliance at its first boot, for the tests of firststart.sh and
banner.sh.

The tests run the real scripts without root and change nothing on the
machine that runs them:

- MEHO_TEST_ROOT points the scripts to a temporary root folder that holds an
  appliance as the build leaves it: the copy of the download (with the real
  lib/net.sh and lib/checks.sh of the form-2 install and a stub install.sh),
  Ubuntu's 10_linux line, the root partition in /sys, and /dev/tty1 as a
  plain file that collects what the console would show.
- PATH holds only stub commands and links to the real tools the scripts
  need. Each stub writes its arguments to calls.log, never its input, so a
  password piped to chpasswd stays out of the log. vmtoolsd prints the OVF
  environment of the test; the mount stub puts it on the fake CD-ROM.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from xml.sax.saxutils import quoteattr

APPLIANCE = Path(__file__).resolve().parents[1]
FIRSTSTART_SH = APPLIANCE / "firststart" / "firststart.sh"
BANNER_SH = APPLIANCE / "firststart" / "banner.sh"
LINUX_SERVER_LIB = APPLIANCE.parent / "linux-server" / "lib"
FAIL_LINE = re.compile(r"^\[FAIL\] .*$", re.MULTILINE)

DOWNLOAD = "/opt/meho/appliance/download"
STATE = "/var/lib/meho/firststart"
STEPS = ("ssh-host-keys", "disk", "wizard-fields", "clock", "core", "bootloader-password")
UBUNTU_CLASS_LINE = 'CLASS="--class gnu-linux --class gnu --class os"\n'

PASSWORD = "Wizard&Pass<word>\"1'"
SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTests admin@example"
JOIN_TOKEN = "K10fake-join-token-secret"

# Every field of the product section, as an admin fills them for a static address.
STATIC_FIELDS = {
    "hostname": "MEHO.Example.com",
    "net.mode": "static",
    "net.ip": "10.0.0.10/24",
    "net.gateway": "10.0.0.1",
    "net.dns": "10.0.0.2, 10.0.0.3",
    "net.ntp": "ntp1.example.com,10.0.0.4",
    "net.admin_network": "10.0.50.0/24",
    "admin.password": PASSWORD,
    "admin.ssh_key": SSH_KEY,
    "cluster.join.address": "",
    "cluster.join.token": "",
    "cluster.vip": "",
}

REAL_TOOLS = (
    "bash",
    "cat",
    "mkdir",
    "rmdir",
    "rm",
    "mv",
    "cp",
    "mktemp",
    "chmod",
    "touch",
    "tee",
    "sed",
    "grep",
    "awk",
    "head",
    "base64",
    "tr",
    "paste",
    "date",
)

STUBS = {
    "vmtoolsd": textwrap.dedent("""\
        [ -f "$MEHO_FAKE/guestinfo-ovfenv" ] || { echo "No value found" >&2; exit 1; }
        cat "$MEHO_FAKE/guestinfo-ovfenv"
        """),
    # mount -o ro -t iso9660 <device> <folder>
    "mount": textwrap.dedent("""\
        [ -f "$MEHO_FAKE/iso-ovfenv" ] || exit 32
        cp "$MEHO_FAKE/iso-ovfenv" "$6/ovf-env.xml"
        """),
    "umount": 'rm -f "$1"/*',
    "ssh-keygen": textwrap.dedent("""\
        case "$1" in
          -A)
            mkdir -p "$MEHO_TEST_ROOT/etc/ssh"
            [ -f "$MEHO_TEST_ROOT/etc/ssh/ssh_host_ed25519_key" ] ||
              date +%s%N > "$MEHO_TEST_ROOT/etc/ssh/ssh_host_ed25519_key" ;;
          -l)
            read -r type key rest
            case "$type" in ssh-*) [ -n "$key" ] ;; *) exit 1 ;; esac ;;
        esac
        """),
    "findmnt": "echo /dev/sda1",
    "lsblk": "echo sda",
    "growpart": 'exit "$(cat "$MEHO_FAKE/growpart_rc")"',
    "resize2fs": "",
    "hostnamectl": 'printf "%s\\n" "$2" > "$MEHO_TEST_ROOT/etc/hostname"',
    "netplan": "",
    "timedatectl": textwrap.dedent("""\
        [ "$1" = show ] && cat "$MEHO_FAKE/ntp_synchronized"
        exit 0
        """),
    "systemctl": "",
    "getent": textwrap.dedent("""\
        case "$1" in
          passwd) grep "^$2:" "$MEHO_FAKE/passwd" ;;
          shadow) grep "^$2:" "$MEHO_FAKE/shadow" ;;
        esac
        """),
    "useradd": 'eval "user=\\${$#}"; printf "%s:x:1000:1000::/home/%s:/bin/bash\\n" "$user" "$user" >> "$MEHO_FAKE/passwd"',
    "chpasswd": 'cat > "$MEHO_FAKE/chpasswd_input"',
    "passwd": "",
    "chown": "",
    "meho-host": 'exit "$(cat "$MEHO_FAKE/meho_host_rc")"',
    "grub-mkpasswd-pbkdf2": textwrap.dedent("""\
        read -r first; read -r second
        [ "$first" = "$second" ] || exit 1
        printf 'Enter password: \\nReenter password: \\n'
        printf 'PBKDF2 hash of your password is grub.pbkdf2.sha512.10000.FAKESALT.FAKEHASH\\n'
        """),
    "update-grub": "",
    "sleep": "",
}

# The install.sh of the download: exits as the test says.
INSTALL_STUB = 'exit "$(cat "$MEHO_FAKE/install_rc")"\n'


def ovf_env(fields: dict[str, str]) -> str:
    """An OVF environment as vCenter hands it over, with escaped values."""
    properties = "\n".join(
        f"    <Property oe:key={quoteattr(key)} oe:value={quoteattr(value)}/>" for key, value in fields.items()
    )
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <Environment xmlns="http://schemas.dmtf.org/ovf/environment/1"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             xmlns:oe="http://schemas.dmtf.org/ovf/environment/1"
             xmlns:ve="http://www.vmware.com/schema/ovfenv"
             oe:id="" ve:vCenterId="vm-42">
          <PlatformSection>
            <Kind>VMware ESXi</Kind>
          </PlatformSection>
          <PropertySection>
        """) + properties + "\n  </PropertySection>\n</Environment>\n"


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f'printf \'%s %s\\n\' "{path.name}" "$*" >> "$MEHO_FAKE/calls.log"\n'
    path.write_text("#!/bin/sh\n" + log + body)
    path.chmod(0o755)


class FakeAppliance:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-firststart-test-")
        base = Path(self._tempdir.name)
        self.root = base / "root"
        self.fake = base / "fake"
        self.stubs = base / "stubs"
        self.tools = base / "tools"
        for folder in (self.root, self.fake, self.stubs, self.tools):
            folder.mkdir()
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            (self.tools / tool).symlink_to(found)
        (self.tools / "python3").symlink_to(sys.executable)
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        for name in ("net.sh", "checks.sh"):
            self.host_file(f"{DOWNLOAD}/lib/{name}", (LINUX_SERVER_LIB / name).read_text())
        _write_script(self.host_path(f"{DOWNLOAD}/install.sh"), INSTALL_STUB)
        self.host_file(f"{DOWNLOAD}/VERSION", "MEHO_VERSION=0.36.0\n")
        self.host_file("/etc/grub.d/10_linux", f"#! /bin/sh\n{UBUNTU_CLASS_LINE}")
        self.host_file("/sys/class/block/sda1/partition", "1\n")
        self.host_file("/dev/tty1", "")
        self.host_file("/dev/sr0", "")
        self.host_file("/etc/hostname", "ubuntu\n")
        self.host_file("/etc/issue", "Ubuntu 24.04 LTS \\n \\l\n")
        self.answer("guestinfo-ovfenv", ovf_env(STATIC_FIELDS))
        self.answer("growpart_rc", "1")
        self.answer("ntp_synchronized", "yes\n")
        self.answer("meho_host_rc", "0")
        self.answer("install_rc", "0")
        self.answer("passwd", "root:x:0:0::/root:/bin/bash\n")
        self.answer("shadow", "root:*:20000:0:99999:7:::\n")
        self.answer("calls.log", "")

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        (self.fake / name).write_text(content)

    def no_answer(self, name: str) -> None:
        (self.fake / name).unlink(missing_ok=True)

    def wizard(self, changes: dict[str, str]) -> None:
        """The wizard fields of the test: the static ones with these changes."""
        self.answer("guestinfo-ovfenv", ovf_env({**STATIC_FIELDS, **changes}))

    def host_path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def host_file(self, path: str, content: str) -> None:
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def read(self, path: str) -> str:
        return self.host_path(path).read_text()

    def mode(self, path: str) -> int:
        return self.host_path(path).stat().st_mode & 0o777

    def calls(self, command: str | None = None) -> list[str]:
        lines = [line.rstrip() for line in (self.fake / "calls.log").read_text().splitlines()]
        if command is None:
            return lines
        return [line for line in lines if line.split(" ", 1)[0] == command]

    def markers(self) -> list[str]:
        folder = self.host_path(STATE)
        return sorted(path.name for path in folder.glob("*.done")) if folder.is_dir() else []

    def _run(self, script: Path) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "MEHO_TEST_ROOT": str(self.root),
            "MEHO_FAKE": str(self.fake),
            "LC_ALL": "C",
        }
        return subprocess.run([str(script)], capture_output=True, text=True, env=env, check=False, timeout=60)

    def run(self) -> subprocess.CompletedProcess[str]:
        return self._run(FIRSTSTART_SH)

    def banner(self) -> str:
        result = self._run(BANNER_SH)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        return self.read("/etc/issue")


class FirstStartTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = FakeAppliance()
        self.addCleanup(self.vm.close)

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_succeeds(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual([], FAIL_LINE.findall(self.output(result)))

    def assert_fails_once(self, result: subprocess.CompletedProcess[str], *fragments: str) -> str:
        output = self.output(result)
        self.assertEqual(1, result.returncode, output)
        fail_lines = FAIL_LINE.findall(output)
        self.assertEqual(1, len(fail_lines), output)
        self.assertIn(" Fix: ", fail_lines[0])
        for fragment in fragments:
            self.assertIn(fragment, fail_lines[0])
        return fail_lines[0]
