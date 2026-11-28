# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only the known places skip the block check, each under its rule, and only through two helpers.

A run that has changed a target ends in step with the secret store:

* clean-up: the removal of an account, user or token that the same run just
  made on a target and could not store, the removal of the script that a
  VPN user create staged on the firewall, and the end of MEHO's own Vault
  login. It runs through
  :func:`~meho_backplane.auth.principal_block_run.cleanup_without_block_check`.
* finish: after a change that the run cannot undo, the step that brings the
  target and the secret store in step. It runs through
  :func:`~meho_backplane.auth.principal_block_run.finish_without_block_check`.

* Each helper is used in its known places, and nowhere else. A new place
  must be added to the list here, with its rule, so a reviewer sees it.
* Only the known calls pass ``script_removal_is_cleanup`` to the pfSense
  playback, each named by its module and its function, so the removal of a
  staged script runs with no block check only there.
* The modules that send Vault requests for a call never start a worker
  thread without the check (:func:`~meho_backplane.auth.send_check.to_thread_checked`).
* Only the block check reads the mark.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

#: The places that skip the block check on purpose, and the rule of each.
_UNCHECKED_PLACES = {
    "auth/vault.py": "cleanup",
    "connectors/harbor/ops_robot_vault.py": "cleanup",
    "connectors/pfsense/ops_openvpn_user.py": "cleanup",
    # The removal of the staged script of a VPN user create.
    "connectors/pfsense/ops_write.py": "cleanup",
    "connectors/vault/ops_token_store.py": "cleanup",
    "connectors/rke2/ops_write.py": "finish",
    "connectors/vault/userpass_password.py": "finish",
}

#: Each call that passes ``script_removal_is_cleanup`` to the pfSense playback:
#: (module, the function that makes the call, the function it calls) -> rule.
_SCRIPT_REMOVAL_CLEANUP_CALLS = {
    # The removal of the script that a VPN user create staged on the firewall.
    ("connectors/pfsense/ops_openvpn_user.py", "_play_create", "_apply_playback"): "cleanup",
}

_HELPER_OF_RULE = {
    "cleanup": "cleanup_without_block_check",
    "finish": "finish_without_block_check",
}

#: The modules that send Vault requests while a call runs.
_VAULT_SENDERS = [
    "auth/vault.py",
    "connectors/_shared/vault_creds.py",
    "connectors/harbor/ops_robot_vault.py",
    "connectors/pfsense/ops_openvpn_user.py",
    "connectors/rke2/ops_write.py",
    *sorted(
        str(p.relative_to(_SRC))
        for folder in ("connectors/vault", "connectors/secret")
        for p in (_SRC / folder).glob("*.py")
    ),
]


def _modules() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _called_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_only_the_known_places_skip_the_block_check_each_under_its_rule() -> None:
    found: dict[str, str] = {}
    for path in _modules():
        called = _called_names(path)
        for rule, helper in _HELPER_OF_RULE.items():
            if helper in called:
                name = str(path.relative_to(_SRC))
                assert name not in found, f"{name} uses both helpers"
                found[name] = rule
    assert found == _UNCHECKED_PLACES


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else "?"


def _passes_the_flag(call: ast.Call) -> bool:
    for keyword in call.keywords:
        value = keyword.value
        is_false = isinstance(value, ast.Constant) and value.value is False
        if keyword.arg == "script_removal_is_cleanup" and not is_false:
            return True
        # ``**kwargs`` into the playback may carry the flag too.
        if keyword.arg is None and _call_name(call) == "_apply_playback":
            return True
    return False


def _script_removal_cleanup_calls(path: Path) -> list[tuple[str, str, str]]:
    module = str(path.relative_to(_SRC))
    calls: list[tuple[str, str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call) and _passes_the_flag(node):
            calls.append((module, function, _call_name(node)))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return calls


def test_only_the_known_calls_make_the_script_removal_a_cleanup() -> None:
    found = [call for path in _modules() for call in _script_removal_cleanup_calls(path)]
    assert sorted(found) == sorted(_SCRIPT_REMOVAL_CLEANUP_CALLS)
    assert set(_SCRIPT_REMOVAL_CLEANUP_CALLS.values()) == {"cleanup"}


def test_vault_senders_start_no_worker_thread_without_the_check() -> None:
    plain = {
        name
        for name in _VAULT_SENDERS
        if {"to_thread", "run_in_executor"} & _called_names(_SRC / name)
    }
    assert plain == set()


def test_only_the_block_check_reads_the_cleanup_mark() -> None:
    readers = {str(p.relative_to(_SRC)) for p in _modules() if "in_cleanup" in _called_names(p)}
    assert readers == {"auth/principal_block.py", "auth/send_check.py"}
