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
  stub accepts every file, and the systemctl stub only reloads. userdel and
  groupdel remove their line from the fake /etc/passwd and /etc/group. Like
  the real userdel with USERGROUPS_ENAB yes in /etc/login.defs (the default
  of Ubuntu and RHEL), userdel also removes the group of the same name; the
  answer usergroups_enab turns that off.
- Standard input is empty unless a test gives answers (stdin="yes\n"), and
  there is no SSH session unless a test gives one (ssh_client=...).
- The real openssl checks the console certificate. console_certificate()
  makes test certificates with fixed dates around NOW, with the real
  openssl, so no test depends on the day it runs.

The default answers describe a healthy machine on 2026-10-23 at 12:00 UTC:
the node is Ready, every pod runs or has finished, both disks have most of
their space free, and every RKE2 certificate is valid for a year or longer.
The MEHO firewall is loaded and enabled, RKE2's CIS profile is set up as
install.sh sets it up, and the console certificate names meho.example.com
and has a year left. The tests of check add the install record, which
holds the host name (INSTALL_RECORD).
"""

from __future__ import annotations

import datetime as dt
import functools
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness import (
    FIXTURES,
    LINUX_SERVER,
    PREROUTING_CHAIN,
    REAL_TOOLS,
    RKE2_CIS_SYSCTL,
    UBUNTU_2404,
    _write_script,
    kernel_path,
)

MEHO_HOST = LINUX_SERVER / "meho-host"
NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
HOST_REAL_TOOLS = (*REAL_TOOLS, "cp", "rm", "rmdir", "find", "tar", "gzip", "head", "openssl")
HOSTNAME = "meho.example.com"

# The files of RKE2's CIS profile as install.sh writes them (lib/cis.sh).
RKE2_CONFIG = (
    'write-kubeconfig-mode: "0600"\nprofile: cis\n'
    'pod-security-admission-config-file: "/etc/rancher/rke2/meho-pss.yaml"\n'
)
CIS_FILES = {
    "/etc/rancher/rke2/config.yaml": RKE2_CONFIG,
    "/etc/rancher/rke2/meho-pss.yaml": (FIXTURES / "rke2-pod-security-cis.yaml").read_text(),
    "/etc/sysctl.d/60-rke2-cis.conf": RKE2_CIS_SYSCTL,
    "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\netcd:x:998:998:etcd user:/:/sbin/nologin\n",
    "/etc/group": "root:x:0:\netcd:x:998:\n",
}
INSTALL_RECORD = (
    f'{{\n  "options": {{\n    "hostname": "{HOSTNAME}",\n    "from_disk": false\n  }}\n}}\n'
)

# openssl ca signs with the dates it is given; "openssl x509 -days" can only
# count from the real time.
_CA_CONFIG = """\
[ ca ]
default_ca = test_ca
[ test_ca ]
database = index.txt
new_certs_dir = .
default_md = sha256
policy = any
unique_subject = no
rand_serial = yes
[ any ]
commonName = optional
[ ca_ext ]
basicConstraints = critical,CA:true,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[ root_ext ]
basicConstraints = critical,CA:true,pathlen:1
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""


def _openssl_time(days: int) -> str:
    return (NOW + dt.timedelta(days=days)).strftime("%Y%m%d%H%M%SZ")


def _openssl(work: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=work, check=True, capture_output=True, timeout=30)


def _sign(work: Path, csr: str, out: str, start: int, end: int, *extra: str) -> str:
    """Sign a request with openssl ca, valid from NOW + start to NOW + end days."""
    (work / "index.txt").write_text("")
    dates = ("-startdate", _openssl_time(start), "-enddate", _openssl_time(end))
    _openssl(
        work,
        "ca",
        "-batch",
        "-notext",
        "-config",
        "ca.cnf",
        "-in",
        csr,
        "-out",
        out,
        *dates,
        *extra,
    )
    return (work / out).read_text()


def _request(work: Path, name: str, subject: str) -> None:
    key = (
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        f"{name}.key",
    )
    _openssl(work, "req", "-new", *key, "-subj", subject, "-out", f"{name}.csr")


@functools.cache
def console_certificate(
    host: str = HOSTNAME,
    *,
    start: int = -30,
    end: int = 365,
    ca: str = "MEHO install CA",
    intermediate_end: int | None = None,
) -> tuple[str, str]:
    """(tls.crt, ca.crt) of a test certificate for the host, signed by a test
    CA with the given name, valid from NOW + start to NOW + end days. The
    CA is valid from 400 days before NOW to ten years after it.

    With intermediate_end, the chain has three levels, as a customer's
    certificate usually has: the CA is the root, it signs an intermediate CA
    that is valid until NOW + intermediate_end days, and the intermediate
    signs the server certificate. tls.crt is then the server certificate and
    the intermediate; ca.crt is the root."""
    with tempfile.TemporaryDirectory(prefix="meho-host-cert-") as folder:
        work = Path(folder)
        (work / "ca.cnf").write_text(_CA_CONFIG)
        _request(work, "ca", f"/CN={ca}")
        ca_pem = _sign(
            work,
            "ca.csr",
            "ca.crt",
            -400,
            3650,
            "-selfsign",
            "-keyfile",
            "ca.key",
            "-extensions",
            "ca_ext" if intermediate_end is None else "root_ext",
        )
        issuer, chain_tail = "ca", ""
        if intermediate_end is not None:
            _request(work, "intermediate", f"/CN={ca} intermediate")
            chain_tail = _sign(
                work,
                "intermediate.csr",
                "intermediate.crt",
                -400,
                intermediate_end,
                "-cert",
                "ca.crt",
                "-keyfile",
                "ca.key",
                "-extensions",
                "ca_ext",
            )
            issuer = "intermediate"
        (work / "leaf.ext").write_text(
            "basicConstraints = critical,CA:false\nkeyUsage = critical,digitalSignature\n"
            f"extendedKeyUsage = serverAuth\nsubjectAltName = DNS:{host}\n"
        )
        _request(work, "leaf", f"/CN={host}")
        leaf_pem = _sign(
            work,
            "leaf.csr",
            "leaf.crt",
            start,
            end,
            "-cert",
            f"{issuer}.crt",
            "-keyfile",
            f"{issuer}.key",
            "-extfile",
            "leaf.ext",
        )
    return leaf_pem + chain_tail, ca_pem


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
          "list table inet meho") exit "$(cat "$MEHO_FAKE/nft_table_rc")" ;;
          "list chain inet meho prerouting")
            cat "$MEHO_FAKE/nft_chain_out"
            exit "$(cat "$MEHO_FAKE/nft_chain_rc")" ;;
          *) exit 1 ;;
        esac
        """),
    "systemctl": textwrap.dedent("""\
        case "$1" in
          daemon-reload) exit 0 ;;
          is-enabled) grep -qxF -- "$3" "$MEHO_FAKE/enabled_units" ;;
          *) exit 1 ;;
        esac
        """),
}

# userdel and groupdel change the fake /etc/passwd and /etc/group. A failed
# call prints a message like the real one. The test of install, update and
# uninstall in test_meho_host.py puts them on the install's fake machine too.
USERDEL_STUB = textwrap.dedent("""\
    rc=$(cat "$MEHO_FAKE/userdel_rc")
    if [ "$rc" != 0 ]; then
        echo "userdel: user etcd is currently used by process 4242" >&2
        exit "$rc"
    fi
    for name; do :; done
    sed -i "/^$name:/d" "$MEHO_TEST_ROOT/etc/passwd"
    if [ "$(cat "$MEHO_FAKE/usergroups_enab")" = yes ]; then
        sed -i "/^$name:/d" "$MEHO_TEST_ROOT/etc/group"
    fi
    """)
GROUPDEL_STUB = textwrap.dedent("""\
    rc=$(cat "$MEHO_FAKE/groupdel_rc")
    if [ "$rc" != 0 ]; then
        echo "groupdel: cannot remove the primary group of user 'etcd'" >&2
        exit "$rc"
    fi
    for name; do :; done
    sed -i "/^$name:/d" "$MEHO_TEST_ROOT/etc/group"
    """)
ACCOUNT_ANSWERS = {"userdel_rc": "0", "groupdel_rc": "0", "usergroups_enab": "yes"}
STUBS["userdel"] = USERDEL_STUB
STUBS["groupdel"] = GROUPDEL_STUB

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
# only to the Go template the bundle uses, and the two public certificates
# of the console's TLS Secret only to the template of the check, which
# prints tls.crt, the line "#ca.crt" and ca.crt; any other request for
# Secret content fails, so the bundle cannot read them in another way.
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
      *"get secret meho-core-tls -o go-template="*)
        cat "$MEHO_FAKE/console_tls_crt"
        printf '\\n#ca.crt\\n'
        cat "$MEHO_FAKE/console_ca_crt"
        exit "$(cat "$MEHO_FAKE/console_tls_rc")" ;;
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
    "nft_table_rc": "0",
    "nft_chain_rc": "0",
    "nft_chain_out": PREROUTING_CHAIN,
    "enabled_units": "meho-firewall.service\n",
    "console_tls_rc": "0",
    **ACCOUNT_ANSWERS,
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
        # A trust folder for openssl's default (SSL_CERT_DIR), set by machine_trusts().
        self.trust_folder: Path | None = None
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
        self.console_certificate(*console_certificate())
        (self.fake / "calls.log").write_text("")
        self.host_file("/etc/os-release", UBUNTU_2404)
        self.host_file("/etc/rancher/rke2/rke2.yaml", "fake access file\n")
        self.host_path("/var/lib/meho").mkdir(parents=True)
        for path, content in CIS_FILES.items():
            self.host_file(path, content)
        for line in RKE2_CIS_SYSCTL.splitlines():
            key, value = line.split("=", 1)
            self.host_file(kernel_path(key), value + "\n")

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        """Set what one stub command answers. The names are in DEFAULT_ANSWERS."""
        (self.fake / name).write_text(content)

    def console_certificate(self, chain: str, ca: str) -> None:
        """Set tls.crt and ca.crt of the console's TLS Secret."""
        self.answer("console_tls_crt", chain)
        self.answer("console_ca_crt", ca)

    def machine_trusts(self, ca: str) -> None:
        """Put the CA certificate into a trust folder that openssl uses as the
        machine's default (SSL_CERT_DIR), as a public CA is on a real machine.
        The folder holds the certificate under its subject hash, as
        'openssl rehash' names it."""
        folder = self.fake / "machine-trust"
        folder.mkdir(exist_ok=True)
        subject_hash = subprocess.run(
            ["openssl", "x509", "-noout", "-subject_hash"],
            input=ca,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        (folder / f"{subject_hash}.0").write_text(ca)
        self.trust_folder = folder

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
        if self.trust_folder is not None:
            env["SSL_CERT_DIR"] = str(self.trust_folder)
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
