# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake installed machine whose kubectl keeps state, for the tests of
meho-host certificate, keyholder and unseal (#4738).

ClusterHost is the FakeHost of host_harness.py with three changes:

- RKE2's kubectl is fake_cluster.py, which keeps the Secrets of namespace
  meho and the state of the one OpenBao pod in $MEHO_FAKE/cluster.json.
  The stub in front of it logs every call to calls.log, like every stub.
- sleep is a stub that only logs, so the waits after a restart of OpenBao
  take no time.
- the real base64 runs behind a wrapper that logs its arguments.

At the start the machine is as after a fresh install: the install record
names meho.example.com, meho-core-tls holds the install's certificate, and
OpenBao unseals itself: the Secret meho-core-vault-unseal holds UNSEAL_KEY,
the key that opens it, and it is unsealed.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from harness import _write_script
from host_harness import INSTALL_RECORD, FakeHost, HostTestCase, certificate_with_key

FAKE_CLUSTER = Path(__file__).with_name("fake_cluster.py")
RKE2_KUBECTL = "/var/lib/rancher/rke2/bin/kubectl"
TLS_SECRET = "meho-core-tls"
UNSEAL_SECRET = "meho-core-vault-unseal"
CORE_LABELS = {"meho.evoila.com/managed-by": "core-bootstrap"}
UNSEAL_KEY_FILE = "/var/lib/meho/unseal-key"
# Made-up unseal keys in the form OpenBao gives them: 32 bytes in base64.
UNSEAL_KEY = base64.b64encode(b"made-up unseal key for the tests").decode()
OTHER_KEY = base64.b64encode(b"a made-up key that opens nothing").decode()
SUPPORT_HINT = "run 'meho-host support-bundle' and send the file to support."


def initial_state() -> dict[str, Any]:
    chain, ca, key = certificate_with_key()
    return {
        "secrets": {
            TLS_SECRET: {
                "type": "kubernetes.io/tls",
                "labels": dict(CORE_LABELS),
                "data": {"tls.crt": chain, "tls.key": key, "ca.crt": ca},
            },
            UNSEAL_SECRET: {
                "type": "Opaque",
                "labels": dict(CORE_LABELS),
                "data": {"key": UNSEAL_KEY},
            },
        },
        "openbao": {
            "uid": "uid-of-pod-1",
            "sealed": False,
            "key": UNSEAL_KEY,
            "answers": True,
            "boot_status_calls": 1,
            "terminating_calls": 2,
            "starting": 0,
            "restarts": 0,
        },
        "fail": [],
        "down": False,
        "key_inputs": [],
    }


def fake_kubectl_stub() -> str:
    """The body of the kubectl stub: run fake_cluster.py with this Python.

    meho-host runs its commands with the small environment of
    FakeHost.environment(). The CPython of actions/setup-python finds its
    libpython through LD_LIBRARY_PATH when its RUNPATH does not exist, as on
    a self-hosted runner (see firststart_harness.py of the appliance tests).
    So the stub keeps the value of the test process, for this Python only.
    """
    lines = []
    library_path = os.environ.get("LD_LIBRARY_PATH")
    if library_path:
        lines.append(f"export LD_LIBRARY_PATH={shlex.quote(library_path)}")
    lines.append(f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLUSTER))} "$@"')
    return "\n".join(lines) + "\n"


class ClusterHost(FakeHost):
    """A fake installed machine with a fake cluster that keeps state."""

    def __init__(self) -> None:
        super().__init__()
        _write_script(self.host_path(RKE2_KUBECTL), fake_kubectl_stub())
        _write_script(self.stubs / "sleep", "exit 0\n")
        found = shutil.which("base64")
        if found is None:
            raise RuntimeError("the tests need 'base64' on PATH")
        _write_script(self.tools / "base64", f'exec "{found}" "$@"')
        self.host_file("/etc/meho/install.json", INSTALL_RECORD)
        self.save(initial_state())

    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads((self.fake / "cluster.json").read_text())
        return state

    def save(self, state: dict[str, Any]) -> None:
        (self.fake / "cluster.json").write_text(json.dumps(state, indent=2))

    def change(self, **openbao: Any) -> None:
        """Change the state of the OpenBao pod, for example sealed=True."""
        state = self.state()
        state["openbao"].update(openbao)
        self.save(state)

    def fail(self, *calls: str) -> None:
        """Let these calls of the fake kubectl fail: patch, delete, create, restart."""
        state = self.state()
        state["fail"] = list(calls)
        self.save(state)

    def keyholder_mode(self, *, sealed: bool) -> None:
        """No unseal Secret: a keyholder unseals OpenBao by hand."""
        state = self.state()
        del state["secrets"][UNSEAL_SECRET]
        state["openbao"]["sealed"] = sealed
        self.save(state)

    def secret(self, name: str) -> dict[str, Any] | None:
        secret: dict[str, Any] | None = self.state()["secrets"].get(name)
        return secret


class ClusterTestCase(HostTestCase):
    """Gives each test its own ClusterHost in self.host."""

    host: ClusterHost

    def setUp(self) -> None:
        self.host = ClusterHost()
        self.addCleanup(self.host.close)

    def assert_one_fail_line(self, result: Any, line: str) -> None:
        """Exit code 1, and the error output is this one [FAIL] line."""
        self.assertEqual(1, result.returncode, self.output(result))
        self.assertEqual(line + "\n", result.stderr)

    def kubectl_calls(self, *words: str) -> list[str]:
        """The kubectl calls whose arguments hold all these words."""
        return [
            line
            for line in self.host.calls().splitlines()
            if line.startswith("kubectl ") and all(word in line for word in words)
        ]
