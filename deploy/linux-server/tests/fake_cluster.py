# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""A fake kubectl that keeps the state of a small cluster, for the tests of
meho-host certificate, keyholder and unseal.

cluster_harness.py puts a shell stub at the path of RKE2's kubectl. The stub
logs its arguments to calls.log, like every stub, and runs this script. The
state is one JSON file, $MEHO_FAKE/cluster.json:

- "secrets": the Secrets of namespace meho by name, each with its "type",
  its "labels" and its "data", decoded;
- "openbao": the one OpenBao pod: its "uid", whether it is "sealed", the
  unseal "key" that opens it, whether its server "answers" at all, and
  "boot_status_calls": how many "bao status" calls a new pod answers with
  an error before its server is up. After a restart the old pod still
  answers, with its old uid and state, for "terminating_calls" calls, as a
  pod that is shutting down does;
- "fail": the calls that fail: "patch", "delete", "create" and "restart";
- "down": true when RKE2 does not answer at all;
- "key_inputs": every call that got the unseal key on standard input.

Like the chart's unseal helper, the fake unseals a sealed server when the
Secret meho-core-vault-unseal holds the right key; it does so at the next
"bao status" call. A failed write quotes its request in the error text, as
a real error can, so the tests prove that meho-host does not show it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

STATE = Path(os.environ["MEHO_FAKE"]) / "cluster.json"
TLS_SECRET = "meho-core-tls"
UNSEAL_SECRET = "meho-core-vault-unseal"
POD = "meho-vault-0"
TLS_TEMPLATE_MARK = '{{"\\n#ca.crt\\n"}}'
UNSEAL_TEMPLATE_MARK = '{{"present\\n"}}'

State = dict[str, Any]


def _error(message: str) -> int:
    print(f"Error from server: {message}", file=sys.stderr)
    return 1


def _words(argv: list[str]) -> list[str]:
    """The arguments without the global ones meho-host always passes."""
    words: list[str] = []
    args = iter(argv)
    for arg in args:
        if arg in ("--kubeconfig", "-n"):
            next(args, None)
        elif not arg.startswith("--request-timeout="):
            words.append(arg)
    return words


def _old_pod(state: State) -> dict[str, Any] | None:
    """The pod that a restart replaces, while it still answers."""
    old: dict[str, Any] | None = state["openbao"].get("old_pod")
    if old is None or old["calls"] <= 0:
        return None
    old["calls"] -= 1
    return old


def _get(state: State, words: list[str]) -> int:
    kind, name = words[1], words[2]
    if kind == "pod" and name == POD and "jsonpath={.metadata.uid}" in words:
        old = _old_pod(state)
        print(old["uid"] if old else state["openbao"]["uid"], end="")
        return 0
    template = next((word for word in words if word.startswith("go-template=")), "")
    if kind != "secret" or not template:
        return _error(f"the fake kubectl does not know: get {' '.join(words[1:])}")
    secret = state["secrets"].get(name)
    if secret is None:
        return 0 if "--ignore-not-found" in words else _error(f'secrets "{name}" not found')
    data = secret["data"]
    if name == TLS_SECRET and TLS_TEMPLATE_MARK in template:
        sys.stdout.write(data.get("tls.crt", "") + "\n#ca.crt\n" + data.get("ca.crt", ""))
        return 0
    if name == UNSEAL_SECRET and UNSEAL_TEMPLATE_MARK in template:
        sys.stdout.write("present\n" + data.get("key", ""))
        return 0
    return _error("the fake kubectl gives Secret content only to the templates of meho-host")


def _patch(state: State, words: list[str], stdin: str) -> int:
    name = words[2]
    merge_from_stdin = "--type=merge" in words and "--patch-file=/dev/stdin" in words
    if words[1] != "secret" or not merge_from_stdin:
        return _error("the fake kubectl takes only a merge patch of a Secret on standard input")
    if "patch" in state["fail"]:
        return _error(f"Internal error occurred: the patch {stdin.strip()} was not saved")
    secret = state["secrets"].get(name)
    if secret is None:
        return _error(f'secrets "{name}" not found')
    for key, value in json.loads(stdin)["data"].items():
        secret["data"][key] = base64.b64decode(value, validate=True).decode()
    print(f"secret/{name} patched")
    return 0


def _delete(state: State, words: list[str]) -> int:
    name = words[2]
    if "delete" in state["fail"]:
        return _error("Internal error occurred: the Secret was not deleted")
    if name not in state["secrets"]:
        return 0 if "--ignore-not-found" in words else _error(f'secrets "{name}" not found')
    del state["secrets"][name]
    print(f'secret "{name}" deleted')
    return 0


def _create(state: State, words: list[str], stdin: str) -> int:
    if words[1:] != ["-f", "-"]:
        return _error("the fake kubectl creates only from standard input")
    if "create" in state["fail"]:
        return _error(f"Internal error occurred: the object {stdin.strip()} was not saved")
    manifest = json.loads(stdin)
    name = manifest["metadata"]["name"]
    if manifest["kind"] != "Secret" or name in state["secrets"]:
        return _error(f'secrets "{name}" already exists')
    data = dict(manifest.get("stringData", {}))
    for key, value in manifest.get("data", {}).items():
        data[key] = base64.b64decode(value, validate=True).decode()
    state["secrets"][name] = {
        "type": manifest.get("type", "Opaque"),
        "labels": manifest["metadata"].get("labels", {}),
        "data": data,
    }
    print(f"secret/{name} created")
    return 0


def _rollout(state: State, words: list[str]) -> int:
    if words[1:] != ["restart", "statefulset/meho-vault"]:
        return _error(f"the fake kubectl does not know: rollout {' '.join(words[1:])}")
    if "restart" in state["fail"]:
        return _error("Internal error occurred: the StatefulSet was not changed")
    bao = state["openbao"]
    bao["old_pod"] = {"uid": bao["uid"], "sealed": bao["sealed"], "calls": bao["terminating_calls"]}
    bao["restarts"] += 1
    bao["uid"] = f"uid-of-pod-{bao['restarts'] + 1}"
    bao["sealed"] = True
    bao["starting"] = bao["boot_status_calls"]
    print("statefulset.apps/meho-vault restarted")
    return 0


def _unseal_helper(state: State) -> None:
    bao = state["openbao"]
    secret = state["secrets"].get(UNSEAL_SECRET)
    if bao["sealed"] and secret and "".join(secret["data"].get("key", "").split()) == bao["key"]:
        bao["sealed"] = False


def _bao(state: State, command: list[str], interactive: bool, stdin: str) -> int:
    bao = state["openbao"]
    old = _old_pod(state)
    if old is not None and command == ["bao", "status"]:
        return 2 if old["sealed"] else 0
    if not bao["answers"] or bao["starting"] > 0:
        bao["starting"] = max(0, bao["starting"] - 1)
        print("Error checking seal status: connection refused", file=sys.stderr)
        return 1
    if command == ["bao", "status"]:
        _unseal_helper(state)
        print(f"Sealed          {str(bao['sealed']).lower()}")
        return 2 if bao["sealed"] else 0
    if command == ["bao", "write", "sys/unseal", "key=-"] and interactive:
        if stdin == bao["key"]:
            bao["sealed"] = False
            print("Key        Value\nsealed     false")
            return 0
        print(
            "Error writing data to sys/unseal: Code: 500. Errors:\n"
            "* failed to decrypt keys from storage: cipher: message authentication failed",
            file=sys.stderr,
        )
        return 2
    return _error(f"the fake bao does not know: {' '.join(command)}")


def _exec(state: State, words: list[str], stdin: str) -> int:
    separator = words.index("--")
    head, command = words[1:separator], words[separator + 1 :]
    if POD not in head or "-c" not in head or head[head.index("-c") + 1] != "bao":
        return _error(f"the fake kubectl does not know: exec {' '.join(head)}")
    return _bao(state, command, "-i" in head, stdin)


def main(argv: list[str]) -> int:
    state: State = json.loads(STATE.read_text())
    words = _words(argv)
    if state["down"]:
        print("The connection to the server was refused", file=sys.stderr)
        return 1
    verb = words[0] if words else ""
    reads_stdin = verb in ("patch", "create") or (verb == "exec" and "-i" in words)
    stdin = sys.stdin.read() if reads_stdin else ""
    if state["openbao"]["key"] in stdin:
        state["key_inputs"].append(" ".join(words))
    match verb:
        case "get":
            rc = _get(state, words)
        case "patch":
            rc = _patch(state, words, stdin)
        case "delete":
            rc = _delete(state, words)
        case "create":
            rc = _create(state, words, stdin)
        case "rollout":
            rc = _rollout(state, words)
        case "exec":
            rc = _exec(state, words, stdin)
        case _:
            rc = _error(f"the fake kubectl does not know: {' '.join(words)}")
    STATE.write_text(json.dumps(state, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
