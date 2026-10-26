# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake machine for the tests of install.sh.

The tests run the real install.sh without root, without RKE2 and without a
network, and they change nothing on the machine that runs them:

- install.sh, lib/ and storage/ are copied into a temporary download folder,
  next to fake RKE2 files and a fake MEHO image archive.
- MEHO_TEST_ROOT points install.sh to a temporary root folder. It holds the
  host files the script reads (/etc/os-release, /proc/meminfo,
  /run/systemd/system) and receives every file the script writes.
- PATH holds only stub commands and links to the few real tools the script
  needs. Each stub answers from one file in the "fake" folder and writes its
  arguments to calls.log, so a test changes one answer and reads the calls.
- SSH_CONNECTION says that an admin at 10.0.0.9 runs the script over SSH.
  A test sets ssh_connection to None for a run on the machine's console.

The default answers describe a healthy Ubuntu 24.04 machine on which RKE2
gets ready at once.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

LINUX_SERVER = Path(__file__).resolve().parents[1]
INSTALL_SH = LINUX_SERVER / "install.sh"
FAIL_LINE = re.compile(r"^\[FAIL\] .*$", re.MULTILINE)

REAL_TOOLS = (
    "bash",
    "sh",
    "env",
    "cat",
    "mkdir",
    "chmod",
    "install",
    "mv",
    "mktemp",
    "dirname",
    "awk",
    "sed",
    "grep",
    "sort",
)

STUBS = {
    "id": 'case "$1" in -u) cat "$MEHO_FAKE/uid" ;; *) exit 1 ;; esac',
    "uname": 'case "$1" in -m) cat "$MEHO_FAKE/arch" ;; *) echo Linux ;; esac',
    "getconf": 'cat "$MEHO_FAKE/cpus"',
    "df": textwrap.dedent("""\
        echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
        echo "/dev/sda2 300000000 1000 $(cat "$MEHO_FAKE/disk_free_kib") 1% /"
        """),
    "pgrep": 'grep -qxF -- "$2" "$MEHO_FAKE/processes"',
    "ss": 'case "$*" in *-t*) cat "$MEHO_FAKE/ss_tcp" ;; *-u*) cat "$MEHO_FAKE/ss_udp" ;; esac',
    "timedatectl": 'cat "$MEHO_FAKE/ntp_synchronized"',
    "hostname": 'cat "$MEHO_FAKE/hostname_fqdn"',
    "systemctl": textwrap.dedent("""\
        case "$1" in
          is-active) grep -qxF -- "$3" "$MEHO_FAKE/active_units" ;;
          is-enabled) grep -qxF -- "$3" "$MEHO_FAKE/enabled_units" ;;
          enable)
            case "$*" in
              *rke2-server*) exit "$(cat "$MEHO_FAKE/systemctl_enable_rc")" ;;
            esac ;;
          restart) exit "$(cat "$MEHO_FAKE/systemctl_restart_rc")" ;;
          disable) exit "$(cat "$MEHO_FAKE/systemctl_disable_rc")" ;;
        esac
        """),
    "nft": textwrap.dedent("""\
        case "$*" in
          "list tables") exit 0 ;;
          "list table inet meho") exit "$(cat "$MEHO_FAKE/nft_table_rc")" ;;
          "-c -f "*) cat "$MEHO_FAKE/nft_check_out"; exit "$(cat "$MEHO_FAKE/nft_check_rc")" ;;
          *) exit 1 ;;
        esac
        """),
    "resolvectl": 'cat "$MEHO_FAKE/resolvectl_out"; exit "$(cat "$MEHO_FAKE/resolvectl_rc")"',
    "getent": 'cat "$MEHO_FAKE/getent_out"; exit "$(cat "$MEHO_FAKE/getent_rc")"',
    "ip": textwrap.dedent("""\
        case "$*" in
          "-4 -o addr show scope global") cat "$MEHO_FAKE/ip_addr_global" ;;
          "-4 -o addr show") cat "$MEHO_FAKE/ip_addr" ;;
          "-4 route show") cat "$MEHO_FAKE/ip_route" ;;
          *) exit 1 ;;
        esac
        """),
    "sleep": ":",
}

KUBECTL_STUB = textwrap.dedent("""\
    case "$*" in
      *"get nodes"*) cat "$MEHO_FAKE/kube_nodes" ;;
      *"get storageclass"*) cat "$MEHO_FAKE/kube_storageclass" ;;
      *"get deployment"*) cat "$MEHO_FAKE/kube_deployment" ;;
      *) exit 1 ;;
    esac
    """)

RKE2_INSTALLER_STUB = textwrap.dedent("""\
    printf 'rke2-installer INSTALL_RKE2_ARTIFACT_PATH=%s\\n' "$INSTALL_RKE2_ARTIFACT_PATH" \\
        >> "$MEHO_FAKE/calls.log"
    exit "$(cat "$MEHO_FAKE/rke2_installer_rc")"
    """)

UBUNTU_2404 = textwrap.dedent("""\
    PRETTY_NAME="Ubuntu 24.04.3 LTS"
    NAME="Ubuntu"
    VERSION_ID="24.04"
    ID=ubuntu
    ID_LIKE=debian
    """)

ADDRESS_LINE = (
    "2: ens18    inet 10.0.0.5/24 brd 10.0.0.255 scope global ens18\\"
    "       valid_lft forever preferred_lft forever\n"
)

DEFAULT_ANSWERS = {
    "uid": "0",
    "arch": "x86_64",
    "cpus": "4",
    "disk_free_kib": str(200 * 10**9 // 1024),
    "processes": "",
    "ss_tcp": "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n",
    "ss_udp": "UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*\nUNCONN 0 0 10.0.0.5:123 0.0.0.0:*\n",
    "ntp_synchronized": "yes",
    "hostname_fqdn": "meho.example.com",
    "active_units": "systemd-resolved\n",
    "enabled_units": "meho-firewall.service\n",
    "resolvectl_out": "meho.example.com: 10.0.0.5                     -- link: ens18\n",
    "resolvectl_rc": "0",
    "getent_out": "10.0.0.5        STREAM meho.example.com\n10.0.0.5        DGRAM\n",
    "getent_rc": "0",
    "ip_addr_global": ADDRESS_LINE,
    "ip_addr": "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n" + ADDRESS_LINE,
    "ip_route": (
        "default via 10.0.0.1 dev ens18 proto static\n"
        "10.0.0.0/24 dev ens18 proto kernel scope link src 10.0.0.5\n"
    ),
    "kube_nodes": "True",
    "kube_storageclass": "true",
    "kube_deployment": "1",
    "rke2_installer_rc": "0",
    "systemctl_enable_rc": "0",
    "systemctl_restart_rc": "0",
    "systemctl_disable_rc": "0",
    "nft_table_rc": "0",
    "nft_check_rc": "0",
    "nft_check_out": "",
}

# An admin at 10.0.0.9 is logged in over SSH to this machine (10.0.0.5).
SSH_SESSION = "10.0.0.9 50022 10.0.0.5 22"

DOWNLOAD_PAYLOAD = {
    "rke2/rke2.linux-amd64.tar.gz": "fake RKE2 binaries\n",
    "rke2/rke2-images.linux-amd64.tar.zst": "fake RKE2 images\n",
    "rke2/sha256sum-amd64.txt": "fake checksums\n",
    "images/meho-images.linux-amd64.tar.zst": "fake MEHO images\n",
}


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f'printf \'%s %s\\n\' "{path.name}" "$*" >> "$MEHO_FAKE/calls.log"\n'
    path.write_text("#!/bin/sh\n" + log + body)
    path.chmod(0o755)


class FakeMachine:
    """One fake machine and one fake download, in a temporary folder."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-install-test-")
        base = Path(self._tempdir.name)
        self.root = base / "root"
        self.fake = base / "fake"
        self.download = base / "download"
        self.stubs = base / "stubs"
        self.tools = base / "tools"
        self.home = base / "home"
        for folder in (self.root, self.fake, self.stubs, self.tools, self.home):
            folder.mkdir()
        self._copy_download()
        self._link_real_tools()
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        _write_script(self.host_path("/var/lib/rancher/rke2/bin/kubectl"), KUBECTL_STUB)
        for name, content in DEFAULT_ANSWERS.items():
            self.answer(name, content)
        (self.fake / "calls.log").write_text("")
        self.host_file("/etc/os-release", UBUNTU_2404)
        self.host_file("/proc/meminfo", "MemTotal:       16303756 kB\nMemFree:  1000 kB\n")
        self.host_path("/run/systemd/system").mkdir(parents=True)
        self.ssh_connection: str | None = SSH_SESSION

    def close(self) -> None:
        self._tempdir.cleanup()

    def _copy_download(self) -> None:
        self.download.mkdir()
        shutil.copy2(INSTALL_SH, self.download / "install.sh")
        shutil.copy2(LINUX_SERVER / "meho-host", self.download / "meho-host")
        shutil.copytree(LINUX_SERVER / "lib", self.download / "lib")
        shutil.copytree(LINUX_SERVER / "storage", self.download / "storage")
        _write_script(self.download / "rke2" / "install.sh", RKE2_INSTALLER_STUB)
        for name, content in DOWNLOAD_PAYLOAD.items():
            path = self.download / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def _link_real_tools(self) -> None:
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            (self.tools / tool).symlink_to(found)

    def answer(self, name: str, content: str) -> None:
        """Set what one stub command answers. The names are in DEFAULT_ANSWERS."""
        (self.fake / name).write_text(content)

    def add_stub(self, name: str, body: str = "exit 0") -> None:
        """Put one more stub command on PATH."""
        _write_script(self.stubs / name, body)

    def host_path(self, path: str) -> Path:
        """The place of a machine path, such as /etc/meho, below the fake root."""
        return self.root / path.lstrip("/")

    def host_file(self, path: str, content: str) -> None:
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def calls(self) -> str:
        """Every stub call so far, one line each: the command and its arguments."""
        return (self.fake / "calls.log").read_text()

    def run(self, *args: str, shell: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run install.sh with the arguments, directly or with the given shell."""
        command = [str(self.download / "install.sh"), *args]
        if shell is not None:
            command.insert(0, str(self.tools / shell))
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "MEHO_TEST_ROOT": str(self.root),
            "MEHO_FAKE": str(self.fake),
            "HOME": str(self.home),
            "LC_ALL": "C",
        }
        if self.ssh_connection is not None:
            env["SSH_CONNECTION"] = self.ssh_connection
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )


class InstallTestCase(unittest.TestCase):
    """Gives each test its own fake machine in self.machine."""

    def setUp(self) -> None:
        self.machine = FakeMachine()
        self.addCleanup(self.machine.close)

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_succeeds(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual([], FAIL_LINE.findall(self.output(result)))

    def assert_fails_once(self, result: subprocess.CompletedProcess[str], *fragments: str) -> str:
        """Exit code 1, exactly one [FAIL] line, and the line says what to fix."""
        output = self.output(result)
        self.assertEqual(1, result.returncode, output)
        fail_lines: list[str] = FAIL_LINE.findall(output)
        self.assertEqual(1, len(fail_lines), output)
        line = fail_lines[0]
        self.assertIn(" Fix: ", line)
        for fragment in fragments:
            self.assertIn(fragment, line)
        return line

    def assert_usage_error(self, result: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(2, result.returncode, self.output(result))
        self.assertIn(fragment, result.stderr)
        self.assertEqual([], FAIL_LINE.findall(self.output(result)))

    def written_files(self) -> list[str]:
        """Files the script wrote below the fake root: everything but the inputs."""
        inputs = {"etc/os-release", "etc/hosts", "proc/meminfo", "var/lib/rancher/rke2/bin/kubectl"}
        found = []
        for path in self.machine.root.rglob("*"):
            relative = path.relative_to(self.machine.root).as_posix()
            if path.is_file() and relative not in inputs:
                found.append(relative)
        return sorted(found)
