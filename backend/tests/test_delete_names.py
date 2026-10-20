# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared delete word list (#4632).

:mod:`meho_backplane.delete_names` is the one place that knows which vendor
action names mean "delete". The parser, the grant checks, the flight
recorder and the stored-delete review all read it, so these tests pin the
rule itself: prefix at a word boundary, the last path part of an ingested
op id, and nothing for dotted typed op ids.
"""

from __future__ import annotations

import pytest

from meho_backplane.delete_names import (
    DESTRUCTIVE_ACTION_PREFIXES,
    destructive_action_name_in_op_id,
    is_destructive_action_name,
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
