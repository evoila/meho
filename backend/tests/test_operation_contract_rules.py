# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of the operation contract checker, with small fake operations.

The live check is in :mod:`tests.test_operation_contracts`. These tests prove
each rule fails with a message that names the problem, without a database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from tests._operation_contract_addresses import NOT_CALLER_ADDRESSES, unmarked_address_fields
from tests._operation_contract_placeholders import REAL_TOP_LEVEL_DOMAINS, load_top_level_domains
from tests.test_operation_contracts import (
    DESCRIPTION_MAX,
    GAPS_FILE,
    NOT_OPERATION_IDS,
    READS_WITH_A_WRITE_WORD,
    SUMMARY_MAX,
    OperationContract,
    check_operation,
    compare_with_gaps,
    find_problems,
)

_GOOD_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM id."},
        "spec": {
            "type": "object",
            "description": "New settings.",
            "properties": {"cpu": {"type": "integer", "description": "CPU count."}},
        },
    },
    "required": ["vm"],
    "examples": [{"vm": "<vm-id>", "spec": {"cpu": 2}}],
}


_GUIDANCE: Final[dict[str, str]] = {
    "when_to_call": "To change CPUs.",
    "output_shape": "The VM id.",
    "next_step": "Read the VM back.",
    "preconditions": "The VM is powered off.",
}


def _good_op(**overrides: Any) -> OperationContract:
    values: dict[str, Any] = {
        "connector": "vmware-rest",
        "op_id": "fake.vm.update",
        "summary": "Change a VM's CPU count.",
        "description": "Sets the number of virtual CPUs of one VM.",
        "parameter_schema": _GOOD_PARAMS,
        "response_schema": {"type": "object", "properties": {"vm": {"type": "string"}}},
        "llm_instructions": _GUIDANCE,
        "safety_level": "caution",
        "requires_approval": True,
        "group_text": "VM changes.",
        "has_preview_builder": False,
    }
    values.update(overrides)
    return OperationContract(**values)


def _only_rule(op: OperationContract, rule: str) -> list[str]:
    result = check_operation(op)
    assert set(result) == {rule}, f"expected only rule {rule!r} to fail, got {result}"
    return result[rule]


def test_a_good_operation_passes_every_rule() -> None:
    assert check_operation(_good_op()) == {}


def test_a_list_response_with_item_fields_passes() -> None:
    op = _good_op(
        response_schema={"type": "array", "items": {"properties": {"vm": {"type": "string"}}}}
    )
    assert check_operation(op) == {}


def test_empty_description_fails() -> None:
    assert _only_rule(_good_op(description=""), "summary") == ["description is empty"]


def test_long_or_multi_line_summary_fails() -> None:
    problems = _only_rule(_good_op(summary="a\nb" + "x" * SUMMARY_MAX), "summary")
    assert "summary is more than one line" in problems
    assert any("characters (at most 120)" in p for p in problems)


def test_long_description_fails() -> None:
    problems = _only_rule(_good_op(description="x" * (DESCRIPTION_MAX + 1)), "summary")
    assert "llm_instructions" in problems[0]


def test_nested_field_without_description_fails() -> None:
    params = {
        **_GOOD_PARAMS,
        "properties": {
            **_GOOD_PARAMS["properties"],
            "spec": {
                "type": "object",
                "description": "New settings.",
                "properties": {"cpu": {"type": "integer"}},
            },
        },
    }
    problems = _only_rule(_good_op(parameter_schema=params), "field_descriptions")
    assert problems == ["parameter field 'spec.cpu' has no description"]


def test_field_inside_an_array_item_is_checked() -> None:
    params = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "description": "Tags.",
                "items": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
        "examples": [{"tags": [{"name": "a"}]}],
    }
    problems = _only_rule(_good_op(parameter_schema=params), "field_descriptions")
    assert problems == ["parameter field 'tags[].name' has no description"]


@pytest.mark.parametrize(
    "response_schema",
    [None, {}, {"type": "object"}, {"type": "array", "items": {"type": "object"}}],
)
def test_opaque_response_schema_fails(response_schema: dict[str, Any] | None) -> None:
    _only_rule(_good_op(response_schema=response_schema), "response_schema")


def test_missing_guidance_key_fails() -> None:
    op = _good_op(llm_instructions={"when_to_use": "x", "output_shape": "y", "next_step": "z"})
    problems = _only_rule(op, "llm_instructions")
    assert problems == [
        "llm_instructions has no 'when_to_call'",
        "llm_instructions has no 'preconditions'",
    ]


def test_a_read_needs_no_preconditions() -> None:
    guidance = {"when_to_call": "x", "output_shape": "y", "next_step": "z"}
    op = _good_op(
        op_id="fake.vm.get", safety_level="safe", requires_approval=False, llm_instructions=guidance
    )
    assert check_operation(op) == {}


def test_missing_example_fails() -> None:
    params = {k: v for k, v in _GOOD_PARAMS.items() if k != "examples"}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert problems == ["parameter schema has no entry under 'examples'"]


def test_example_that_does_not_validate_fails() -> None:
    params = {**_GOOD_PARAMS, "examples": [{"spec": {"cpu": "two"}}]}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert len(problems) == 2
    assert all(p.startswith("example 0 does not validate") for p in problems)


@pytest.mark.parametrize(
    "example",
    [
        {"vm": "<vm-id>", "spec": {"cpu": "<cpu-count>"}},
        {"vm": "<one-of-possible_answers>"},
        {"vm": "<vm.id>"},
    ],
)
def test_placeholder_in_an_example_passes_the_fields_own_rules(example: dict[str, Any]) -> None:
    """A ``<name>`` placeholder is not held to the field's type or pattern (the aim of #4111)."""
    schema = {
        **_GOOD_PARAMS,
        "properties": {
            **_GOOD_PARAMS["properties"],
            "vm": {"type": "string", "pattern": "^vm-[0-9]+$", "description": "VM id."},
        },
        "examples": [example],
    }
    assert check_operation(_good_op(parameter_schema=schema)) == {}


@pytest.mark.parametrize(
    "example",
    [
        {"vm": "vm 1"},
        {"vm": "<VM-ID>"},
        {"vm": "<vm-id", "spec": {"cpu": "two"}},
        {"spec": {"cpu": "<cpu-count>"}},
    ],
)
def test_a_value_that_is_no_placeholder_still_fails(example: dict[str, Any]) -> None:
    """Only a lowercase ``<name>`` is a placeholder; a missing required field still fails."""
    schema = {
        **_GOOD_PARAMS,
        "properties": {
            **_GOOD_PARAMS["properties"],
            "vm": {"type": "string", "pattern": "^vm-[0-9]+$", "description": "VM id."},
        },
        "examples": [example],
    }
    problems = _only_rule(_good_op(parameter_schema=schema), "examples")
    assert problems, example
    assert all(p.startswith("example 0 does not validate") for p in problems)


def test_a_placeholder_is_refused_at_runtime() -> None:
    """The placeholder rule is only for the example check: a real call is still refused."""
    schema = {
        **_GOOD_PARAMS,
        "properties": {
            **_GOOD_PARAMS["properties"],
            "vm": {"type": "string", "pattern": "^vm-[0-9]+$", "description": "VM id."},
        },
    }
    assert validate_params(schema, {"vm": "<vm-id>"}) != []
    assert validate_params(schema, {"vm": "vm-1", "spec": {"cpu": "<cpu-count>"}}) != []


def test_ticket_number_in_guidance_fails() -> None:
    guidance = {
        "when_to_call": "Added by #1234.",
        "output_shape": "y",
        "next_step": "z",
        "preconditions": "p",
    }
    problems = _only_rule(_good_op(llm_instructions=guidance), "no_ticket_numbers")
    assert problems == ["text names ticket #1234"]


def test_change_without_approval_fails() -> None:
    problems = _only_rule(_good_op(requires_approval=False), "requires_approval")
    assert problems == ["a 'caution' operation must have requires_approval: true"]


@pytest.mark.parametrize(("group_text", "message"), [(None, "no group"), ("  ", "no text")])
def test_missing_group_or_group_text_fails(group_text: str | None, message: str) -> None:
    problems = _only_rule(_good_op(group_text=group_text), "group")
    assert message in problems[0]


def test_destructive_without_preview_builder_fails() -> None:
    problems = _only_rule(_good_op(safety_level="destructive"), "preview_builder")
    assert "no preview builder" in problems[0]
    assert check_operation(_good_op(safety_level="destructive", has_preview_builder=True)) == {}


def test_safe_operation_with_a_write_word_fails() -> None:
    problems = _only_rule(
        _good_op(op_id="fake.vm.power.get", safety_level="safe", requires_approval=False),
        "write_name",
    )
    assert "'power'" in problems[0]
    assert "READS_WITH_A_WRITE_WORD" in problems[0]


def _safe_op(op_id: str) -> OperationContract:
    return _good_op(op_id=op_id, safety_level="safe", requires_approval=False)


@pytest.mark.parametrize("op_id", ["fake.vm.settings.get", "fake.node.runtime.show"])
def test_a_write_word_inside_a_longer_word_does_not_count(op_id: str) -> None:
    assert check_operation(_safe_op(op_id)) == {}


def test_an_allow_listed_read_passes_the_name_rule() -> None:
    assert check_operation(_safe_op(next(iter(READS_WITH_A_WRITE_WORD)))) == {}


def test_a_change_may_carry_a_write_word() -> None:
    assert check_operation(_good_op(op_id="fake.vm.power.start")) == {}


def test_a_new_gap_and_a_gone_gap_each_name_the_operation_and_rule() -> None:
    problems = {("vmware-rest", "fake.vm.update", "examples"): ["no example"]}
    known = {("nsx-rest", "fake.segment.list", "summary")}
    messages = compare_with_gaps(problems, known)
    assert messages == [
        "NEW GAP vmware-rest fake.vm.update rule 'examples': no example",
        "GONE nsx-rest fake.segment.list rule 'summary': the gap is fixed or the operation "
        f"no longer exists; remove this entry from {GAPS_FILE.name}",
    ]
    assert compare_with_gaps(problems, set(problems)) == []


_UNMARKED_HOST_PARAMS: Final[dict[str, Any]] = {
    **_GOOD_PARAMS,
    "properties": {
        **_GOOD_PARAMS["properties"],
        "host": {"type": "string", "description": "The host to check."},
    },
}

_NESTED_FIELD_NO_DESCRIPTION: Final[dict[str, Any]] = {
    **_GOOD_PARAMS,
    "properties": {"vm": {"type": "object", "description": "VM.", "properties": {"id": {}}}},
    "examples": [{"vm": {"id": "<vm-id>"}}],
}


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"description": ""}, "summary"),
        ({"parameter_schema": _NESTED_FIELD_NO_DESCRIPTION}, "field_descriptions"),
        ({"response_schema": {"type": "object"}}, "response_schema"),
        ({"parameter_schema": {**_GOOD_PARAMS, "examples": []}}, "examples"),
        ({"parameter_schema": {**_GOOD_PARAMS, "examples": [{"vm": 1}]}}, "examples"),
        ({"requires_approval": False}, "requires_approval"),
        ({"safety_level": "destructive"}, "preview_builder"),
        ({"safety_level": "safe", "requires_approval": False}, "write_name"),
        (
            {"parameter_schema": {**_GOOD_PARAMS, "examples": [{"vm": "vm-42"}]}},
            "example_placeholders",
        ),
        (
            {"llm_instructions": {**_GUIDANCE, "next_step": "Call `fake.vm.nope`."}},
            "named_operations_exist",
        ),
        ({"parameter_schema": _UNMARKED_HOST_PARAMS}, "caller_address"),
    ],
)
def test_each_bad_operation_fails_with_a_message_naming_operation_and_rule(
    overrides: dict[str, Any], rule: str
) -> None:
    messages = compare_with_gaps(find_problems([_good_op(**overrides)]), set())
    assert len(messages) == 1
    assert messages[0].startswith(f"NEW GAP vmware-rest fake.vm.update rule {rule!r}: ")


# ---------------------------------------------------------------------------
# Rule 11: example values look made up
# ---------------------------------------------------------------------------


def _with_example(example: dict[str, Any]) -> OperationContract:
    return _good_op(parameter_schema={**_GOOD_PARAMS, "examples": [example]})


def test_a_real_looking_vm_id_fails_and_a_placeholder_passes() -> None:
    problems = _only_rule(_with_example({"vm": "vm-42"}), "example_placeholders")
    assert problems == [
        "example 0: vm holds 'vm-42', which looks real; use a placeholder in angle "
        "brackets, such as <vm-id>"
    ]
    assert check_operation(_with_example({"vm": "<vm-id>"})) == {}


@pytest.mark.parametrize(
    "value",
    [
        "vm-42",
        "host-12",
        "domain-c8",
        "domain-s3",
        "datastore-15",
        "resgroup-8",
        "resgroup-v12",
        "group-v3",
        "dvportgroup-21",
        "datacenter-2",
        "moref=vm-42",
        "10.20.30.40",
        "10.9.0.0/24",
        "https://172.16.0.5:443/api",
        "fe80::1",
        "fd00::10",
        "vc01.not-a-real-host.com",
        "vc01.not-a-real-host.fr",
        "nas.made-up.uk",
        "app.made-up.tech",
        "shop.made-up.xyz",
        "esx-01.local",
        "nas.lan",
        "admin@made-up-domain.net",
        "https://vcenter.made-up.de/ui",
        "release-notes.md",
        "0b6e3e0c-1f2a-4c55-9a51-2f6d7a8b9c01",
        "00000000-0000-4000-8000-000000000001",
        "0B6E3E0C-1F2A-4C55-9A51-2F6D7A8B9C01",
    ],
)
def test_a_real_looking_value_fails(value: str) -> None:
    problems = _only_rule(_with_example({"vm": "<vm-id>", "target": value}), "example_placeholders")
    assert len(problems) == 1, problems
    assert "target holds" in problems[0]


@pytest.mark.parametrize(
    "value",
    [
        "<vm-id>",
        "<vcenter-host.example>",
        "192.0.2.10",
        "198.51.100.0/24",
        "203.0.113.7",
        "2001:db8::1",
        "vc.example.com",
        "example.org",
        "mail.example.net",
        "esx01.test",
        "vc.lab.example",
        "host.invalid",
        "management.azure.com",
        "Microsoft.Storage/storageAccounts",
        "compute.provision.post",
        "sa@example-project.iam.gserviceaccount.com",
        "kubernetes.default.svc.cluster.local",
        "cert-manager.io",
        "vsphere.local\\<group>",
        "00000000-0000-0000-0000-000000000000",
        "config.yaml",
        "web-vm-01",
        "vm-name",
        "datastore1",
        "12:30:00",
        "aa:bb:cc:dd:ee:ff",
        "https://<backup-store>/<folder>/",
    ],
)
def test_a_made_up_value_passes(value: str) -> None:
    assert check_operation(_with_example({"vm": "<vm-id>", "target": value})) == {}


def test_a_nested_real_looking_id_fails_with_its_path() -> None:
    example = {
        "vm": "<vm-id>",
        "spec": {"cpu": 2, "placement": {"cluster": "domain-c8"}},
        "disks": [{"datastore": "datastore-15"}],
    }
    problems = _only_rule(_with_example(example), "example_placeholders")
    assert [p.split(" holds ")[0] for p in problems] == [
        "example 0: spec.placement.cluster",
        "example 0: disks[].datastore",
    ]


def test_every_example_is_checked_not_only_the_first() -> None:
    params = {**_GOOD_PARAMS, "examples": [{"vm": "<vm-id>"}, {"vm": "vm-7"}]}
    problems = _only_rule(_good_op(parameter_schema=params), "example_placeholders")
    assert len(problems) == 1
    assert problems[0].startswith("example 1: vm holds 'vm-7'")


def test_a_version_field_may_look_like_an_address() -> None:
    """``9.0.2.0`` in a field named ``*version*`` is a version, not an IPv4 address."""
    assert check_operation(_with_example({"vm": "<vm-id>", "target_version": "9.0.2.0"})) == {}
    example = {"vm": "<vm-id>", "address": "9.0.2.0"}
    problems = _only_rule(_with_example(example), "example_placeholders")
    assert "'9.0.2.0'" in problems[0]


@pytest.mark.parametrize("field", ["path", "files[].path", "tar_path", "config_file"])
def test_a_path_field_may_hold_a_file_name_that_ends_like_a_domain(field: str) -> None:
    """``release-notes.md`` in a ``*path*`` or ``*file*`` field is a file name, not a host."""
    example: dict[str, Any] = {"vm": "<vm-id>"}
    if "[]" in field:
        example["files"] = [{"path": "docs/release-notes.md"}]
    else:
        example[field] = "docs/release-notes.md"
    assert check_operation(_with_example(example)) == {}
    problems = _only_rule(
        _with_example({"vm": "<vm-id>", "name": "release-notes.md"}), "example_placeholders"
    )
    assert "'release-notes.md'" in problems[0]


@pytest.mark.parametrize(
    "value",
    [
        "\\\\nas01.made-up.com\\backups\\full.bak",
        "//nas01.made-up.local/backups/full.bak",
        "file://nas01.made-up.com/backups/release-notes.md",
        "smb://backup@nas01.made-up.com:445/backups/full.bak",
        "nfs-01.made-up.net:/exports/backups",
    ],
)
def test_a_path_field_with_a_real_looking_host_fails(value: str) -> None:
    """The host of a network share, a file URL or an NFS export is still checked."""
    problems = _only_rule(_with_example({"vm": "<vm-id>", "path": value}), "example_placeholders")
    assert len(problems) == 1, problems
    assert problems[0].startswith("example 0: path holds 'n")
    assert "made-up." in problems[0]


@pytest.mark.parametrize(
    "value",
    [
        "\\\\<backup-server>\\backups\\<file>.bak",
        "\\\\nas.example.com\\backups\\release-notes.md",
        "file://nas.example.com/backups/release-notes.md",
        "file:///var/backups/release-notes.md",
        "nas.example.com:/exports/release-notes.md",
        "C:/Users/<name>/release-notes.md",
    ],
)
def test_a_path_field_with_a_made_up_host_passes(value: str) -> None:
    """A placeholder or example host passes, and the file name part is never a host."""
    assert check_operation(_with_example({"vm": "<vm-id>", "path": value})) == {}


def test_the_top_level_domain_list_is_the_public_one(tmp_path: Path) -> None:
    """The snapshot holds the public domains, lowercase; the reserved names are not in it."""
    assert {"com", "fr", "uk", "tech", "xyz", "io"} <= REAL_TOP_LEVEL_DOMAINS
    assert not {"test", "example", "invalid", "localhost"} & REAL_TOP_LEVEL_DOMAINS
    assert all(domain == domain.lower() for domain in REAL_TOP_LEVEL_DOMAINS)
    snapshot = tmp_path / "tlds.txt"
    snapshot.write_text("# a comment\nCOM\n\n  fr \n", encoding="utf-8")
    assert load_top_level_domains(snapshot) == {"com", "fr"}


# ---------------------------------------------------------------------------
# Rule 12: the guidance names only registered operations
# ---------------------------------------------------------------------------


def _guided(next_step: str, **overrides: Any) -> OperationContract:
    return _good_op(llm_instructions={**_GUIDANCE, "next_step": next_step}, **overrides)


@pytest.mark.parametrize(
    "next_step",
    [
        "Call `fake.vm.nope` next.",
        "Call ``fake.vm.nope`` next.",
        "Use `fake.vm.nope` or `fake.vm.update`.",
    ],
)
def test_guidance_naming_an_unregistered_operation_fails(next_step: str) -> None:
    result = check_operation(_guided(next_step), registered={"fake.vm.update"})
    assert result == {
        "named_operations_exist": [
            "guidance names 'fake.vm.nope', which is no registered operation"
        ]
    }


def test_rule_12_needs_the_registry() -> None:
    """A check of one operation alone has no registry, so rule 12 does not run."""
    assert check_operation(_guided("Call `fake.vm.nope` next.")) == {}


def test_guidance_naming_a_registered_operation_passes() -> None:
    reader = _good_op(op_id="fake.vm.get", safety_level="safe", requires_approval=False)
    writer = _guided("Read it back with `fake.vm.get`, then `fake.vm.update` again.")
    assert find_problems([reader, writer]) == {}


def test_the_registry_spans_every_connector() -> None:
    """Guidance may name an operation of another connector."""
    other = _good_op(
        connector="k8s", op_id="other.node.get", safety_level="safe", requires_approval=False
    )
    op = _guided("When the API is up, call `other.node.get`; never `other.node.lost`.")
    assert find_problems([other, op]) == {
        ("vmware-rest", "fake.vm.update", "named_operations_exist"): [
            "guidance names 'other.node.lost', which is no registered operation"
        ]
    }


def test_a_wrong_first_part_is_caught() -> None:
    """``kubernetes.node.list`` is no operation when only ``k8s`` operations exist."""
    node_list = _good_op(
        connector="k8s", op_id="k8s.node.list", safety_level="safe", requires_approval=False
    )
    op = _guided("When the API is down, `kubernetes.node.list` cannot answer.")
    assert find_problems([node_list, op]) == {
        ("vmware-rest", "fake.vm.update", "named_operations_exist"): [
            "guidance names 'kubernetes.node.list', which is no registered operation"
        ]
    }


@pytest.mark.parametrize(
    "next_step",
    [
        "Run `call_operation` with `op_id` and read `status`.",
        "A wildcard such as `fake.vm.*` is no name.",
        "`Fake.Vm.Nope` has capitals.",
        "A placeholder such as `<op-id>` is no name.",
        "Plain text fake.vm.nope without backticks.",
    ],
)
def test_backticked_words_that_are_no_operation_id_pass(next_step: str) -> None:
    assert find_problems([_guided(next_step)]) == {}


def test_a_listed_non_operation_name_passes_and_an_unlisted_one_fails() -> None:
    name = next(iter(NOT_OPERATION_IDS))
    assert find_problems([_guided(f"Check `{name}`.")]) == {}
    assert find_problems([_guided("Check `status.phase`.")]) != {}


# ---------------------------------------------------------------------------
# Rule 13: an address field carries the caller-address marker
# ---------------------------------------------------------------------------


def _with_field(name: str, schema: dict[str, Any]) -> OperationContract:
    return _good_op(
        parameter_schema={
            **_GOOD_PARAMS,
            "properties": {**_GOOD_PARAMS["properties"], name: {"description": "x", **schema}},
        }
    )


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("url", {"type": "string"}),
        ("uri", {"type": "string"}),
        ("host", {"type": "string"}),
        ("hostname", {"type": "string"}),
        ("address", {"type": "string"}),
        ("server", {"type": "string"}),
        ("resolver", {"type": "string"}),
        ("Host", {"type": "string"}),
        ("callback_url", {"type": "string"}),
        ("jump_host", {"type": "string"}),
        ("portal_address", {"type": "string"}),
        ("to", {"type": "string", "format": "uri"}),
        ("to", {"type": "string", "format": "url"}),
        ("to", {"type": "string", "format": "hostname"}),
        ("to", {"type": "string", "format": "ipv4"}),
        ("to", {"type": "string", "format": "ipv6"}),
    ],
)
def test_an_address_field_without_marker_fails_with_its_name(
    name: str, schema: dict[str, Any]
) -> None:
    problems = _only_rule(_with_field(name, schema), "caller_address")
    assert len(problems) == 1
    assert problems[0].startswith(f"parameter field {name!r} looks like an address")


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("host", {"type": "string", CALLER_ADDRESS_MARKER: True}),
        ("to", {"type": "string", "format": "uri", CALLER_ADDRESS_MARKER: True}),
        ("hostgroup", {"type": "string"}),
        ("url_path", {"type": "string"}),
        ("to", {"type": "string", "format": "date-time"}),
    ],
)
def test_a_marked_or_other_field_passes(name: str, schema: dict[str, Any]) -> None:
    assert check_operation(_with_field(name, schema)) == {}


def test_a_marker_false_does_not_count() -> None:
    op = _with_field("host", {"type": "string", CALLER_ADDRESS_MARKER: False})
    assert _only_rule(op, "caller_address")


def test_a_nested_address_field_fails_with_its_path() -> None:
    nested = {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "object", "properties": {"url": {"type": "string"}}},
            }
        },
    }
    assert unmarked_address_fields("fake.op", nested) == ["targets[].url"]


def test_a_marker_on_a_list_covers_its_items() -> None:
    schema = {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                CALLER_ADDRESS_MARKER: True,
                "items": {"type": "string", "format": "uri"},
            }
        },
    }
    assert unmarked_address_fields("fake.op", schema) == []


def test_an_allow_listed_field_passes_and_every_entry_has_a_reason() -> None:
    assert NOT_CALLER_ADDRESSES
    for (op_id, path), reason in NOT_CALLER_ADDRESSES.items():
        assert reason.strip(), f"{op_id} {path} has no reason"
        schema = {"type": "object", "properties": {path: {"type": "string"}}}
        if not unmarked_address_fields("fake.other.op", schema):
            # A field found by its format, not its name (allowed_ip, format ipv4).
            schema = {"type": "object", "properties": {path: {"type": "string", "format": "ipv4"}}}
        if "." not in path and "[" not in path:
            assert unmarked_address_fields(op_id, schema) == []
            assert unmarked_address_fields("fake.other.op", schema) == [path]
