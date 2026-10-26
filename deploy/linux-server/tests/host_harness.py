# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake installed machine for the tests of meho-host.

The tests run the real meho-host without root, without RKE2 and without a
network, and they change nothing on the machine that runs them:

- MEHO_TEST_ROOT points meho-host to a temporary root folder. It holds the
  files meho-host reads (RKE2's kubectl and rke2 at their real places,
  /etc/os-release, the two one-time files) and receives the support bundle.
- PATH holds only stub commands and wrappers of the few real tools meho-host
  needs. Each stub answers from one file in the "fake" folder and writes its
  arguments to calls.log, so a test changes one answer and reads the calls.
  Each wrapper logs its arguments the same way and then runs the real tool,
  so a test can check that no secret value was on any command line. The
  date stub answers "now" from a file and passes every other call on to the
  real date, which parses the expiry dates of the certificates. The nft
  stub accepts every file, and the systemctl stub only reloads.
- Standard input is empty unless a test gives answers (stdin="yes\n"), and
  there is no SSH session unless a test gives one (ssh_client=...).

The default answers describe a healthy machine on 2026-10-23 at 12:00 UTC:
the node is Ready, every pod runs or has finished, both disks have most of
their space free, and every certificate is valid for a year or longer.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness import LINUX_SERVER, REAL_TOOLS, UBUNTU_2404, _write_script

MEHO_HOST = LINUX_SERVER / "meho-host"
NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
HOST_REAL_TOOLS = (*REAL_TOOLS, "cp", "rm", "find", "tar", "gzip")


def expires(days: int) -> str:
    """An EXPIRES value of 'rke2 certificate check', this many days after NOW."""
    return (NOW + dt.timedelta(days=days)).strftime("%b %d, %Y %H:%M UTC")


def certificate_table(*rows: tuple[str, str, int, str]) -> str:
    """The table of 'rke2 certificate check --output table': a blank line, the
    header, and one row per (file name, subject, days left, status)."""
    lines = [
        "",
        f"{'FILENAME':<30}{'SUBJECT':<28}{'USAGES':<26}{'EXPIRES':<25}{'RESIDUAL TIME':<16}STATUS",
    ]
    for file_name, subject, days, status in rows:
        lines.append(
            f"{file_name:<30}{subject:<28}{'ClientAuth':<26}"
            f"{expires(days):<25}{'1 year':<16}{status}"
        )
    return "\n".join(lines) + "\n"


def pod_line(namespace: str, name: str, phase: str, ready: str = "", reason: str = "") -> str:
    """One line of the fake answer of 'kubectl get pods -A': the fields with "|" between them."""
    return f"{namespace}|{name}|{phase}|{ready}|{reason}\n"


def df_answer(size_gb: int, free_gb: int) -> str:
    """What 'df -P -k' prints for a file system of the size with the free space."""
    size_kib = size_gb * 10**9 // 1024
    free_kib = free_gb * 10**9 // 1024
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/sda2 {size_kib} {size_kib - free_kib} {free_kib} 1% /\n"
    )


STUBS = {
    "id": 'case "$1" in -u) cat "$MEHO_FAKE/uid" ;; *) exit 1 ;; esac',
    "hostname": 'cat "$MEHO_FAKE/hostname_fqdn"',
    "date": textwrap.dedent("""\
        case "$*" in
          "-u +%s") cat "$MEHO_FAKE/now_epoch" ;;
          "-u +%Y%m%dT%H%M%SZ") cat "$MEHO_FAKE/now_stamp" ;;
          *) exec "$MEHO_REAL_DATE" "$@" ;;
        esac
        """),
    "df": textwrap.dedent("""\
        case "$*" in
          -h) cat "$MEHO_FAKE/df_h" ;;
          *"/var/lib/rancher"*) cat "$MEHO_FAKE/df_rancher" ;;
          *"/var/lib/meho"*) cat "$MEHO_FAKE/df_meho" ;;
          *) exit 1 ;;
        esac
        """),
    "free": 'cat "$MEHO_FAKE/free_out"',
    "uname": 'cat "$MEHO_FAKE/uname_a"',
    "ip": textwrap.dedent("""\
        case "$1" in
          addr) cat "$MEHO_FAKE/ip_addr" ;;
          route) cat "$MEHO_FAKE/ip_route" ;;
          *) exit 1 ;;
        esac
        """),
    "timedatectl": 'cat "$MEHO_FAKE/timedatectl_out"',
    "journalctl": 'cat "$MEHO_FAKE/journal_out"',
    "nft": textwrap.dedent("""\
        case "$*" in
          "-c -f "*) cat "$MEHO_FAKE/nft_check_out"; exit "$(cat "$MEHO_FAKE/nft_check_rc")" ;;
          "-f "*) exit 0 ;;
          *) exit 1 ;;
        esac
        """),
    "systemctl": 'case "$1" in daemon-reload) exit 0 ;; *) exit 1 ;; esac',
}

RKE2_STUB = textwrap.dedent("""\
    case "$1 $2" in
      "certificate check")
        cat "$MEHO_FAKE/rke2_cert_table"
        exit "$(cat "$MEHO_FAKE/rke2_rc")" ;;
      *) exit 1 ;;
    esac
    """)

# The fake kubectl answers each call of meho-host from one file. The values
# of the Secrets (kube_secret_values, one key=value per line) are answered
# only to the Go template the bundle uses; any other request for Secret
# content fails, so the bundle cannot read them in another way.
KUBECTL_STUB = textwrap.dedent("""\
    rc=$(cat "$MEHO_FAKE/kubectl_rc")
    [ "$rc" = 0 ] || exit "$rc"
    case "$*" in
      *"get nodes"*) cat "$MEHO_FAKE/kube_nodes" ;;
      *"get pods -A"*) cat "$MEHO_FAKE/kube_pods" ;;
      *"-n meho get pods -o name"*) cat "$MEHO_FAKE/kube_pod_names_meho" ;;
      *"-n kube-system get pods -o name"*) cat "$MEHO_FAKE/kube_pod_names_kube_system" ;;
      *"-n local-path-storage get pods -o name"*) cat "$MEHO_FAKE/kube_pod_names_storage" ;;
      *" logs "*"--previous"*)
        cat "$MEHO_FAKE/kube_logs_previous"
        exit "$(cat "$MEHO_FAKE/kube_logs_previous_rc")" ;;
      *" logs "*) cat "$MEHO_FAKE/kube_logs" ;;
      *"get secrets -o go-template="*) cat "$MEHO_FAKE/kube_secret_values" ;;
      *"get secret"*" -o "*)
        echo "error: the fake kubectl gives Secret content only to the Go template" >&2
        exit 1 ;;
      *"get secrets"*) cat "$MEHO_FAKE/kube_secret_names" ;;
      *"get all,"*) cat "$MEHO_FAKE/kube_objects" ;;
      *"get events"*) cat "$MEHO_FAKE/kube_events" ;;
      *"describe node"*) cat "$MEHO_FAKE/kube_node_describe" ;;
      *) echo "error: the fake kubectl does not know: $*" >&2; exit 1 ;;
    esac
    """)

MEHO_PODS = ("meho-backplane-7d9f8-x2k4p", "meho-keycloak-0", "meho-core-bootstrap-abc12")
TRAEFIK_POD = "rke2-traefik-5c7d9-m3n8q"
HEALTHY_PODS = (
    pod_line("meho", "meho-backplane-7d9f8-x2k4p", "Running", "True")
    + pod_line("meho", "meho-keycloak-0", "Running", "True")
    + pod_line("meho", "meho-core-bootstrap-abc12", "Succeeded", "False")
    + pod_line("kube-system", "rke2-canal-k9x2p", "Running", "True")
    + pod_line("kube-system", TRAEFIK_POD, "Running", "True")
    + pod_line("kube-system", "helm-install-rke2-traefik-zx9v2", "Succeeded", "False")
    + pod_line("local-path-storage", "local-path-provisioner-6f8b7-q4w2e", "Running", "True")
)

DEFAULT_ANSWERS = {
    "uid": "0",
    "hostname_fqdn": "meho.example.com",
    "now_epoch": str(int(NOW.timestamp())),
    "now_stamp": NOW.strftime("%Y%m%dT%H%M%SZ"),
    "kubectl_rc": "0",
    "kube_nodes": "True",
    "kube_pods": HEALTHY_PODS,
    "kube_pod_names_meho": "".join(f"pod/{pod}\n" for pod in MEHO_PODS),
    "kube_pod_names_kube_system": (
        f"pod/rke2-canal-k9x2p\npod/{TRAEFIK_POD}\npod/helm-install-rke2-traefik-zx9v2\n"
    ),
    "kube_pod_names_storage": "pod/local-path-provisioner-6f8b7-q4w2e\n",
    "kube_logs": "2026-10-23T11:59:00Z fake log line\n",
    "kube_logs_previous": "",
    "kube_logs_previous_rc": "1",
    "kube_secret_names": (
        "NAME                    TYPE     DATA   AGE\nmeho-core-first-login   Opaque   4      3h\n"
    ),
    "kube_secret_values": "admin_password=Fake-Default-Password-0a1b\nadmin_username=admin\n",
    "kube_objects": (
        "NAME                             READY   STATUS    RESTARTS   AGE\n"
        "pod/meho-backplane-7d9f8-x2k4p   1/1     Running   0          3h\n"
    ),
    "kube_events": (
        "LAST SEEN   TYPE     REASON    OBJECT                MESSAGE\n"
        "3h          Normal   Started   pod/meho-keycloak-0   Started container keycloak\n"
    ),
    "kube_node_describe": "Name:   meho.example.com\nConditions:   Ready True\n",
    "rke2_rc": "0",
    "rke2_cert_table": certificate_table(
        ("client-admin.crt", "system:admin", 365, "OK"),
        ("server-ca.crt", "rke2-server-ca", 3650, "OK"),
    ),
    "df_rancher": df_answer(200, 150),
    "df_meho": df_answer(200, 150),
    "df_h": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda2 200G 50G 150G 25% /\n",
    "free_out": "       total   used   free\nMem:   15921   6000   9921\n",
    "uname_a": "Linux meho 6.8.0-45-generic #45-Ubuntu SMP x86_64 GNU/Linux\n",
    "ip_addr": "2: ens18 inet 10.0.0.5/24 scope global ens18\n",
    "ip_route": "default via 10.0.0.1 dev ens18\n",
    "timedatectl_out": "System clock synchronized: yes\n",
    "journal_out": "2026-10-23T11:00:00+0000 meho rke2[1234]: fake journal line\n",
    "nft_check_rc": "0",
    "nft_check_out": "",
}


class FakeHost:
    """One fake installed machine in a temporary folder."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-host-test-")
        base = Path(self._tempdir.name)
        self.root = base / "root"
        self.fake = base / "fake"
        self.stubs = base / "stubs"
        self.tools = base / "tools"
        for folder in (self.root, self.fake, self.stubs, self.tools):
            folder.mkdir()
        for tool in HOST_REAL_TOOLS:
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            _write_script(self.tools / tool, f'exec "{found}" "$@"')
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        _write_script(self.host_path("/var/lib/rancher/rke2/bin/kubectl"), KUBECTL_STUB)
        _write_script(self.host_path("/usr/local/bin/rke2"), RKE2_STUB)
        for name, content in DEFAULT_ANSWERS.items():
            self.answer(name, content)
        (self.fake / "calls.log").write_text("")
        self.host_file("/etc/os-release", UBUNTU_2404)
        self.host_file("/etc/rancher/rke2/rke2.yaml", "fake access file\n")
        self.host_path("/var/lib/meho").mkdir(parents=True)

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        """Set what one stub command answers. The names are in DEFAULT_ANSWERS."""
        (self.fake / name).write_text(content)

    def host_path(self, path: str) -> Path:
        """The place of a machine path, such as /var/lib/meho, below the fake root."""
        return self.root / path.lstrip("/")

    def host_file(self, path: str, content: str) -> None:
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def calls(self) -> str:
        """Every stub call so far, one line each: the command and its arguments."""
        return (self.fake / "calls.log").read_text()

    def run(
        self, *args: str, stdin: str = "", ssh_client: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run meho-host with the arguments. stdin holds the answers to its
        questions; ssh_client is the SSH_CLIENT value of an SSH session."""
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "MEHO_TEST_ROOT": str(self.root),
            "MEHO_FAKE": str(self.fake),
            "MEHO_REAL_DATE": shutil.which("date") or "/bin/date",
            "LC_ALL": "C",
        }
        if ssh_client is not None:
            env["SSH_CLIENT"] = ssh_client
        return subprocess.run(
            [str(MEHO_HOST), *args],
            capture_output=True,
            text=True,
            env=env,
            input=stdin,
            check=False,
            timeout=60,
        )


class HostTestCase(unittest.TestCase):
    """Gives each test its own fake machine in self.host."""

    def setUp(self) -> None:
        self.host = FakeHost()
        self.addCleanup(self.host.close)

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_usage_error(self, result: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(2, result.returncode, self.output(result))
        self.assertIn(fragment, result.stderr)
        self.assertEqual("", result.stdout)
