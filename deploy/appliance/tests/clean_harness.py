# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake build VM for the tests of clean.sh.

The tests run the real clean.sh without root and change nothing on the
machine that runs them:

- MEHO_TEST_ROOT points clean.sh to a temporary root folder that holds a
  machine after the form-2 install: MEHO's data, RKE2's state, the SSH host
  keys, the build user, a machine-id, logs and the appliance pieces.
- PATH holds only stub commands and links to the few real tools the script
  needs. Each stub writes its arguments to calls.log. The kubectl stub at
  RKE2's own path keeps the Secrets of the cluster in one file, one line
  each: the name and its managed-by label. The rke2-killall.sh stub stops
  the fake pods: pgrep finds a containerd-shim until it has run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

APPLIANCE = Path(__file__).resolve().parents[1]
CLEAN_SH = APPLIANCE / "clean.sh"
FAIL_LINE = re.compile(r"^\[FAIL\] .*$", re.MULTILINE)
LEFT_LINE = re.compile(r"^\[LEFT\] .*$", re.MULTILINE)

BUILD_USER = "meho-build"
RKE2 = "/var/lib/rancher/rke2"
LABEL = "meho.evoila.com/managed-by=core-bootstrap"

REAL_TOOLS = (
    "bash",
    "find",
    "rm",
    "grep",
    "head",
    "cat",
    "truncate",
    "sync",
    "mktemp",
    "mv",
    "awk",
    "mkdir",
    "sleep",
)

STUBS = {
    "systemctl": textwrap.dedent("""\
        case "$1" in
          is-enabled) cat "$MEHO_FAKE/rke2_enabled" ;;
          disable) printf 'disabled\\n' > "$MEHO_FAKE/rke2_enabled" ;;
        esac
        """),
    "loginctl": 'exit "$(cat "$MEHO_FAKE/loginctl_rc")"',
    "pkill": "exit 1",
    "userdel": textwrap.dedent("""\
        grep -v "^$2:" "$MEHO_FAKE/passwd" > "$MEHO_FAKE/passwd.new" || true
        mv "$MEHO_FAKE/passwd.new" "$MEHO_FAKE/passwd"
        rm -rf "$MEHO_TEST_ROOT/home/$2"
        """),
    "getent": 'grep "^$2:" "$MEHO_FAKE/passwd"',
    "cloud-init": textwrap.dedent("""\
        case "$*" in
          *--machine-id*) printf 'uninitialized\\n' > "$MEHO_TEST_ROOT/etc/machine-id" ;;
        esac
        case "$*" in
          *--logs*) rm -f "$MEHO_TEST_ROOT"/var/log/cloud-init*.log ;;
        esac
        rm -rf "$MEHO_TEST_ROOT/var/lib/cloud"
        """),
    "meho-host": 'exit "$(cat "$MEHO_FAKE/meho_host_rc")"',
    "pgrep": '[ "$(cat "$MEHO_FAKE/pods_running")" = 1 ]',
    "journalctl": "",
    "fstrim": "",
}

# The script passes --kubeconfig and -n before the verb; the stub drops them.
KUBECTL_STUB = textwrap.dedent(
    """\
    while [ $# -gt 0 ]; do
      case $1 in --kubeconfig|-n) shift 2 ;; *) break ;; esac
    done
    secrets="$MEHO_FAKE/secrets"
    case "$*" in
      "get secret -o name")
        [ "$(cat "$MEHO_FAKE/secrets_get_rc")" = 0 ] || exit 1
        awk '{ print "secret/" $1 }' "$secrets" ;;
      "delete secret -l """
    + LABEL
    + """")
        [ "$(cat "$MEHO_FAKE/secrets_stick")" = 1 ] && exit 0
        awk '$2 != "core-bootstrap"' "$secrets" > "$secrets.new" && mv "$secrets.new" "$secrets" ;;
      "delete secret "*)
        [ "$(cat "$MEHO_FAKE/secrets_stick")" = 1 ] && exit 0
        awk -v name="$3" '$1 != name' "$secrets" > "$secrets.new" && mv "$secrets.new" "$secrets" ;;
      *) exit 1 ;;
    esac
    """
)

# The pods must stop before MEHO's data goes, so the stub fails when the
# data is already gone.
KILLALL_STUB = textwrap.dedent("""\
    [ -e "$MEHO_TEST_ROOT/var/lib/meho/unseal-key" ] || exit 1
    printf '0\\n' > "$MEHO_FAKE/pods_running"
    """)

SECRETS = textwrap.dedent("""\
    meho-core-postgres-superuser core-bootstrap
    meho-core-first-login core-bootstrap
    meho-core-vault-unseal core-bootstrap
    meho-core-tls -
    sh.helm.release.v1.meho.v1 -
    """)

# What a build VM holds after the form-2 install and the appliance pieces.
# The values stand in for keys and data; they are fixed test strings.
MACHINE_FILES = {
    "/var/lib/meho/first-login-passwords": "fake-first-login\n",
    "/var/lib/meho/unseal-key": "fake-unseal-key\n",
    "/var/lib/meho/volumes/pvc-1/postgres/PG_VERSION": "17\n",
    "/var/lib/meho/volumes/pvc-2/openbao/init.json": "fake-openbao-init\n",
    "/etc/meho/values-place.yaml": 'coreKeycloak:\n  firstTenantId: "fake-tenant"\n',
    "/etc/meho/install.json": '{"options": {"public_address": "192.0.2.10"}}\n',
    "/var/lib/kubelet/pods/uid-1/volumes/kubernetes.io~empty-dir/tmp/scratch": "fake scratch\n",
    f"{RKE2}/server/db/etcd/member/snap/db": "fake-etcd\n",
    f"{RKE2}/server/tls/server-ca.key": "fake-server-ca-key\n",
    f"{RKE2}/server/cred/encryption-config.json": "fake-secrets-key\n",
    f"{RKE2}/server/token": "fake-token\n",
    f"{RKE2}/server/agent-token": "fake-agent-token\n",
    f"{RKE2}/server/manifests/local-path-storage.yaml": "fake storage manifest\n",
    f"{RKE2}/server/manifests/rke2-traefik-config.yaml": "fake traefik config\n",
    f"{RKE2}/agent/images/meho-images.linux-amd64.tar.zst": "fake MEHO images\n",
    f"{RKE2}/agent/images/rke2-images.linux-amd64.tar.zst": "fake RKE2 images\n",
    f"{RKE2}/agent/containerd/io.containerd.content.v1.content/blobs/sha256/abc": "fake layer\n",
    f"{RKE2}/agent/client-ca.crt": "fake client ca\n",
    f"{RKE2}/agent/kubelet.kubeconfig": "fake kubelet kubeconfig\n",
    "/etc/rancher/node/password": "fake-node-password\n",
    "/etc/rancher/rke2/rke2.yaml": "fake kubeconfig\n",
    "/etc/rancher/rke2/config.yaml": "node-name: build.lab.example\n",
    "/etc/ssh/ssh_host_ed25519_key": "fake host key\n",
    "/etc/ssh/ssh_host_ed25519_key.pub": "fake host key pub\n",
    "/etc/ssh/sshd_config": "PasswordAuthentication no\n",
    "/etc/machine-id": "0123456789abcdef0123456789abcdef\n",
    "/etc/netplan/60-meho-build.yaml": "network: {version: 2}\n",
    "/etc/sudoers.d/90-cloud-init-users": "meho-build ALL=(ALL) NOPASSWD:ALL\n",
    "/etc/cloud/cloud-init.disabled": "",
    "/var/lib/cloud/instance/user-data.txt": "#cloud-config\n",
    "/var/log/syslog": "many lines\n",
    "/var/log/cloud-init.log": "cloud-init lines\n",
    "/var/log/pods/meho_core/0.log": "pod lines\n",
    "/root/.bash_history": "sudo ./install.sh\n",
    "/root/.ssh/authorized_keys": 'command="echo login as meho-build" ssh-ed25519 fake build key\n',
    f"/home/{BUILD_USER}/.ssh/authorized_keys": "ssh-ed25519 fake build key\n",
    "/opt/meho/appliance/download/install.sh": "#!/usr/bin/env bash\n",
    "/opt/meho/appliance/download/VERSION": "MEHO_VERSION=0.36.0\n",
    "/opt/meho/appliance/firststart/firststart.sh": "#!/usr/bin/env bash\n",
    "/opt/meho/appliance/firststart/banner.sh": "#!/usr/bin/env bash\n",
    "/etc/systemd/system/meho-firststart.service": "[Unit]\n",
    "/etc/systemd/system/meho-banner.service": "[Unit]\n",
}

# The links systemctl enable makes for the two units of first start.
ENABLED_UNITS = ("meho-firststart.service", "meho-banner.service")
WANTS = "/etc/systemd/system/multi-user.target.wants"


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f'printf \'%s %s\\n\' "{path.name}" "$*" >> "$MEHO_FAKE/calls.log"\n'
    path.write_text("#!/bin/sh\n" + log + body)
    path.chmod(0o755)


class FakeBuildVM:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-clean-test-")
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
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        _write_script(self.host_path(f"{RKE2}/bin/kubectl"), KUBECTL_STUB)
        _write_script(self.host_path("/usr/local/bin/rke2-killall.sh"), KILLALL_STUB)
        for path, content in MACHINE_FILES.items():
            self.host_file(path, content)
        for unit in ENABLED_UNITS:
            link = self.host_path(f"{WANTS}/{unit}")
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(f"/etc/systemd/system/{unit}")
        self.answer("secrets", SECRETS)
        self.answer("secrets_stick", "0")
        self.answer("secrets_get_rc", "0")
        self.answer("rke2_enabled", "enabled\n")
        self.answer("pods_running", "1")
        self.answer("loginctl_rc", "0")
        self.answer("meho_host_rc", "0")
        self.answer(
            "passwd",
            f"root:x:0:0::/root:/bin/bash\n{BUILD_USER}:x:1000:1000::/home/{BUILD_USER}:/bin/bash\n",
        )
        self.answer("calls.log", "")

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        (self.fake / name).write_text(content)

    def host_path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def host_file(self, path: str, content: str) -> None:
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def calls(self) -> list[str]:
        return (self.fake / "calls.log").read_text().splitlines()

    def secrets(self) -> list[str]:
        return [line.split()[0] for line in (self.fake / "secrets").read_text().splitlines()]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "MEHO_TEST_ROOT": str(self.root),
            "MEHO_FAKE": str(self.fake),
            "LC_ALL": "C",
        }
        return subprocess.run(
            [str(CLEAN_SH), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )


class CleanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = FakeBuildVM()
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
            self.assertIn(fragment, output)
        return fail_lines[0]

    def left_lines(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return LEFT_LINE.findall(result.stderr)
