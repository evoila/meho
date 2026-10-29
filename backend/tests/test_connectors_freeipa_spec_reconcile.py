# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""freeipa spec-reconcile lane -- hand-coded IPA commands vs FreeIPA's ``API.txt``.

FreeIPA publishes the contract of each release in ``API.txt``: one block per
command, starting with ``command: <name>/<version>``. Release 4.13.4 has 506
commands at API version 2.257. FreeIPA is GPL-3.0 licensed, so the file is
not copied into this repository; it sits on the spec shelf at
``freeipa-4.13.4/API.txt``, and the compare test skips while it is missing.

Every IPA command the connector sends is a module constant in
:mod:`meho_backplane.connectors.freeipa.rpc` whose name ends in
``_COMMAND``. The lane reads them from the live module (never a copy). The
login and JSON paths come from FreeIPA's ``doc/api/jsonrpc_usage.md``, not
from ``API.txt``, so they are pinned in the manifest only.
"""

from __future__ import annotations

from pathlib import Path

from meho_backplane.connectors.freeipa import rpc as _rpc_module
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "freeipa-4.13.4"
_SPEC_FILE = "API.txt"

_COMMAND_PREFIX = "command: "


def _declared_commands() -> set[str]:
    """The IPA commands the connector sends."""
    return {
        value
        for name, value in vars(_rpc_module).items()
        if name.endswith("_COMMAND") and isinstance(value, str)
    }


def _served_commands(api_txt: Path) -> set[str]:
    """Command names in an ``API.txt``, without their ``/<version>`` suffix."""
    return {
        line.removeprefix(_COMMAND_PREFIX).split("/", 1)[0]
        for line in api_txt.read_text(encoding="utf-8").splitlines()
        if line.startswith(_COMMAND_PREFIX)
    }


def test_freeipa_hand_coded_command_manifest_is_pinned() -> None:
    """Pin the swept command set so drift cannot silently shrink the reconcile."""
    assert _declared_commands() == {
        "ping",
        "host_find",
        "host_show",
        "hostgroup_find",
        "hostgroup_show",
        "user_find",
        "user_show",
        "group_find",
        "group_show",
        "role_find",
        "role_show",
        "sudorule_find",
        "sudorule_show",
        "hbacrule_find",
        "hbacrule_show",
        "hbactest",
        "pwpolicy_show",
    }
    assert (_rpc_module.LOGIN_PATH, _rpc_module.JSON_PATH) == (
        "/ipa/session/login_password",
        "/ipa/session/json",
    )


def test_served_commands_parses_api_txt_blocks(tmp_path: Path) -> None:
    api_txt = tmp_path / "API.txt"
    api_txt.write_text(
        "command: ping/1\nargs: 0,1,1\noption: Str('version?')\n"
        "command: host_find/1\nargs: 1,40,4\n"
        "default: ping/1\ncapability: messages 2.52\n",
        encoding="utf-8",
    )
    assert _served_commands(api_txt) == {"ping", "host_find"}


def test_freeipa_commands_are_in_the_pinned_api_txt() -> None:
    """Every hand-coded IPA command is in ``API.txt`` of FreeIPA 4.13.4."""
    label = f"{_SPEC_DIR}/{_SPEC_FILE}"
    served = _served_commands(require_shelf_spec(_SPEC_DIR, _SPEC_FILE))
    assert_op_ids_served(_declared_commands(), served, spec_label=label)
