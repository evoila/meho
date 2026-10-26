# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared delete and write word lists (#4632).

:mod:`meho_backplane.delete_names` is the one place that knows which vendor
action names mean "delete", which op-id patterns are delete-shaped, and
which words in a typed op id mean "write". The parser, the grant checks,
the flight recorder, the stored-delete review and the contract check in CI
all read it, so these tests pin the rules themselves: prefix at a word
boundary, the last path part of an ingested op id, nothing for dotted typed
op ids, whole words only for the write words, and every delete sign is a
write word.
"""

from __future__ import annotations

import pytest

from meho_backplane.delete_names import (
    DELETE_SHAPED_OP_ID_PATTERNS,
    DESTRUCTIVE_ACTION_PREFIXES,
    WRITE_WORDS,
    destructive_action_name_in_op_id,
    is_destructive_action_name,
    write_word_in_op_id,
)


def test_word_list_is_the_parser_vocabulary() -> None:
    assert DESTRUCTIVE_ACTION_PREFIXES == ("Destroy", "Delete", "Remove", "Unregister")


@pytest.mark.parametrize(
    "value",
    [
        "Destroy_Task",
        "VirtualMachine.Destroy_Task",
        "DeleteDatastoreFile_Task",
        "RemovePortGroup",
        "UnregisterVM",
        "Unregister",
        "Remove",
    ],
)
def test_action_names_that_mean_delete(value: str) -> None:
    assert is_destructive_action_name(value) is True


@pytest.mark.parametrize("value", ["Destroyer_Task", "Reset_Task", "Removed", "destroy", ""])
def test_action_names_that_only_look_similar(value: str) -> None:
    assert is_destructive_action_name(value) is False


@pytest.mark.parametrize(
    ("op_id", "action_name"),
    [
        ("POST:/VirtualMachine/{moId}/Destroy_Task", "Destroy_Task"),
        ("POST:/VirtualApp/{moId}/Destroy_Task", "Destroy_Task"),
        ("POST:/HostNetworkSystem/{moId}/RemovePortGroup", "RemovePortGroup"),
        ("POST:/FileManager/{moId}/DeleteDatastoreFile_Task", "DeleteDatastoreFile_Task"),
        ("POST:/VirtualMachine/{moId}/UnregisterVM", "UnregisterVM"),
    ],
)
def test_ingested_op_id_reports_its_delete_action_name(op_id: str, action_name: str) -> None:
    assert destructive_action_name_in_op_id(op_id) == action_name


@pytest.mark.parametrize(
    "op_id",
    [
        # Lookalikes: the same pair the parser test uses.
        "POST:/VirtualMachine/{moId}/Destroyer_Task",
        "POST:/VirtualMachine/{moId}/Reset_Task",
        # Only the last path part counts.
        "POST:/Destroy/{moId}/Reset_Task",
        "GET:/vcenter/vm/{vm}",
        # A plain HTTP DELETE is the first sign, not this one.
        "DELETE:/vcenter/vm/{vm}",
        # Dotted typed op ids keep their own patterns.
        "vmware.composite.vm.destroy",
        "vault.sys.policy.delete",
        # Not an ingested op id shape.
        "Destroy_Task",
        "POST:Destroy_Task",
        "POST:/",
    ],
)
def test_op_ids_without_a_delete_action_name(op_id: str) -> None:
    assert destructive_action_name_in_op_id(op_id) is None


def test_every_delete_sign_is_a_write_word() -> None:
    """An id with a delete sign is also a write, so the two lists cannot drift apart."""
    for prefix in DESTRUCTIVE_ACTION_PREFIXES:
        assert prefix.lower() in WRITE_WORDS, prefix
    for pattern in DELETE_SHAPED_OP_ID_PATTERNS:
        word = pattern.removeprefix("*.").removesuffix(":*").lower()
        assert word in WRITE_WORDS, pattern


def test_delete_shaped_patterns_are_the_grant_check_defaults() -> None:
    from meho_backplane.settings import _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS

    # The grant check adds the Hetzner Robot vSwitch writes sent as POST
    # (#3973) on top of the shared delete signs.
    grant_defaults = _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    assert grant_defaults == (*DELETE_SHAPED_OP_ID_PATTERNS, "POST:/vswitch/*")
    assert DELETE_SHAPED_OP_ID_PATTERNS == (
        "DELETE:*",
        "*.delete",
        "*.destroy",
        "*.remove",
        "*.purge",
    )


@pytest.mark.parametrize(
    ("op_id", "word"),
    [
        ("rke2.etcd-snapshot.save", "save"),
        ("msad.user.create", "create"),
        ("topology.delete_node", "delete"),
        ("hyperv.power.start", "power"),
        ("vmware.composite.vm.Power.Off", "Power"),
        ("gh.composite.project_item_set_field", "set"),
    ],
)
def test_write_word_in_op_id(op_id: str, word: str) -> None:
    assert write_word_in_op_id(op_id) == word


@pytest.mark.parametrize(
    "op_id",
    ["vm.settings.get", "node.runtime.show", "mssql.ha.health", "rabbitmq.overview", ""],
)
def test_op_ids_without_a_write_word(op_id: str) -> None:
    assert write_word_in_op_id(op_id) is None
