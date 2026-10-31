# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI check for the contracts of every hand-written operation.

An AI picks an operation from its summary, description, group text and
safety level, and fills in the parameters from the field descriptions. This
test checks every hand-written (typed and multi-step) operation that MEHO
registers at start-up, of every connector, against ten rules:

1. ``summary`` -- a one-line summary (at most 120 characters) and a short
   description (at most 300 characters).
2. ``field_descriptions`` -- every parameter field, top level and nested,
   has a description.
3. ``response_schema`` -- the response schema names its fields
   (``properties``, or ``items`` with ``properties``).
4. ``llm_instructions`` -- ``when_to_call``, ``output_shape`` and
   ``next_step``; a change (not ``safe``) also has ``preconditions``.
5. ``examples`` -- at least one entry under ``examples`` in the parameter
   schema, and each one validates against the schema.
6. ``no_ticket_numbers`` -- no ticket number (``#1234``) in the summary,
   description or guidance.
7. ``requires_approval`` -- every operation that is not ``safe`` has
   ``requires_approval: true``.
8. ``group`` -- the operation's group exists and has a text.
9. ``preview_builder`` -- every ``destructive`` operation has a preview
   builder (``register_preview_builder``).
10. ``write_name`` -- a ``safe`` operation has no write word in its id
    (``create``, ``delete``, ``set``, ``start``, ...; the list is
    :data:`meho_backplane.delete_names.WRITE_WORDS`). The real reads whose
    id holds such a word are in :data:`READS_WITH_A_WRITE_WORD`, each with
    a reason.

Today's gaps live in ``tests/data/operation_contract_gaps.yaml``. The test
fails on a gap that is not in that list (a new gap) and on a listed gap that
is gone (remove the entry). So the list only shrinks.

The check reads what MEHO registers at start-up. It calls no vendor system.
A new connector is covered as soon as its registrar runs; nothing needs to
be added here. The checker's own tests, with fake operations, are in
``test_operation_contract_rules.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

GAPS_FILE: Final[Path] = Path(__file__).parent / "data" / "operation_contract_gaps.yaml"

RULES: Final[tuple[str, ...]] = (
    "summary",
    "field_descriptions",
    "response_schema",
    "llm_instructions",
    "examples",
    "no_ticket_numbers",
    "requires_approval",
    "group",
    "preview_builder",
    "write_name",
)

#: Real reads whose id holds a write word, with the reason. The live test
#: fails when an entry names an operation that no longer exists or is no
#: longer ``safe``, so this list stays tidy.
READS_WITH_A_WRITE_WORD: Final[Mapping[str, str]] = {
    "installer.system.depot.sync-status": "reads the state of the depot sync; starts none",
    "mssql.ha.sync-health": "reads the health of the availability-group sync",
    "vcfa.tenant.abx.run.get": "reads the state and log of an ABX action run; runs nothing",
    "vcfa.vra8.abx.run.get": "reads the state and log of an ABX action run; runs nothing",
    "winsrv.system.pending-reboot": "reads whether a reboot is pending; reboots nothing",
}

SUMMARY_MAX: Final[int] = 120
DESCRIPTION_MAX: Final[int] = 300
GUIDANCE_KEYS: Final[tuple[str, ...]] = ("when_to_call", "output_shape", "next_step")
CHANGE_GUIDANCE_KEY: Final[str] = "preconditions"

#: Three or more digits, so "the #1 sign" is not read as a ticket number.
_TICKET_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<![\w&])#\d{3,}\b")
_NESTED_SCHEMA_KEYS: Final[tuple[str, ...]] = ("items", "additionalProperties")
_SCHEMA_LIST_KEYS: Final[tuple[str, ...]] = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_MAP_KEYS: Final[tuple[str, ...]] = ("$defs", "definitions")


@dataclass(frozen=True)
class OperationContract:
    """What one registered operation tells the AI. Built from the database row."""

    connector: str
    op_id: str
    summary: str | None = None
    description: str | None = None
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    response_schema: Mapping[str, Any] | None = None
    llm_instructions: Mapping[str, Any] | None = None
    safety_level: str = "safe"
    requires_approval: bool = False
    group_text: str | None = None
    has_preview_builder: bool = False


# ---------------------------------------------------------------------------
# The ten rules. Each returns a list of problems; an empty list means pass.
# ---------------------------------------------------------------------------


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check_summary(op: OperationContract) -> list[str]:
    problems: list[str] = []
    summary = op.summary or ""
    description = op.description or ""
    if not summary.strip():
        problems.append("summary is empty")
    if "\n" in summary.strip():
        problems.append("summary is more than one line")
    if len(summary) > SUMMARY_MAX:
        problems.append(f"summary has {len(summary)} characters (at most {SUMMARY_MAX})")
    if not description.strip():
        problems.append("description is empty")
    if len(description) > DESCRIPTION_MAX:
        problems.append(
            f"description has {len(description)} characters (at most {DESCRIPTION_MAX}; "
            "put longer guidance in llm_instructions)"
        )
    return problems


def _fields_without_description(schema: object, path: str) -> list[str]:
    """Return the dotted path of every named field with no description."""
    if not isinstance(schema, Mapping):
        return []
    missing: list[str] = []
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, sub in properties.items():
            field_path = f"{path}.{name}" if path else str(name)
            if not (isinstance(sub, Mapping) and _has_text(sub.get("description"))):
                missing.append(field_path)
            missing.extend(_fields_without_description(sub, field_path))
    for key in _NESTED_SCHEMA_KEYS:
        missing.extend(_fields_without_description(schema.get(key), f"{path}[]" if path else path))
    for key in _SCHEMA_LIST_KEYS:
        for sub in schema.get(key) or ():
            missing.extend(_fields_without_description(sub, path))
    for key in _SCHEMA_MAP_KEYS:
        defs = schema.get(key)
        if isinstance(defs, Mapping):
            for name, sub in defs.items():
                missing.extend(_fields_without_description(sub, f"{key}/{name}"))
    return missing


def check_field_descriptions(op: OperationContract) -> list[str]:
    return [
        f"parameter field {path!r} has no description"
        for path in _fields_without_description(op.parameter_schema, "")
    ]


def _names_fields(schema: object) -> bool:
    return isinstance(schema, Mapping) and bool(schema.get("properties"))


def check_response_schema(op: OperationContract) -> list[str]:
    schema = op.response_schema
    if _names_fields(schema):
        return []
    if isinstance(schema, Mapping) and _names_fields(schema.get("items")):
        return []
    return ["response schema names no field (needs properties, or items with properties)"]


def _has_guidance(value: object) -> bool:
    if isinstance(value, list | Mapping):
        return bool(value)
    return _has_text(value)


def check_llm_instructions(op: OperationContract) -> list[str]:
    guidance = op.llm_instructions
    if not isinstance(guidance, Mapping):
        return ["llm_instructions is missing"]
    required = list(GUIDANCE_KEYS)
    if op.safety_level != "safe":
        required.append(CHANGE_GUIDANCE_KEY)
    return [
        f"llm_instructions has no {key!r}"
        for key in required
        if not _has_guidance(guidance.get(key))
    ]


def check_examples(op: OperationContract) -> list[str]:
    examples = op.parameter_schema.get("examples")
    if not isinstance(examples, list) or not examples:
        return ["parameter schema has no entry under 'examples'"]
    problems: list[str] = []
    for index, example in enumerate(examples):
        if not isinstance(example, Mapping):
            problems.append(f"example {index} is not an object")
            continue
        for error in validate_params(dict(op.parameter_schema), dict(example)):
            problems.append(
                f"example {index} does not validate: {error['path']} expects {error['expected']}"
            )
    return problems


def _texts(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _texts(item)


def check_no_ticket_numbers(op: OperationContract) -> list[str]:
    found: set[str] = set()
    for text in _texts([op.summary, op.description, op.llm_instructions]):
        found.update(_TICKET_NUMBER.findall(text))
    return [f"text names ticket {number}" for number in sorted(found)]


def check_requires_approval(op: OperationContract) -> list[str]:
    if op.safety_level != "safe" and not op.requires_approval:
        return [f"a {op.safety_level!r} operation must have requires_approval: true"]
    return []


def check_group(op: OperationContract) -> list[str]:
    if op.group_text is None:
        return ["operation has no group"]
    if not _has_text(op.group_text):
        return ["operation group has no text"]
    return []


def check_preview_builder(op: OperationContract) -> list[str]:
    if op.safety_level == "destructive" and not op.has_preview_builder:
        return ["destructive operation has no preview builder, so it can never wait for approval"]
    return []


def check_write_name(op: OperationContract) -> list[str]:
    if op.safety_level != "safe" or op.op_id in READS_WITH_A_WRITE_WORD:
        return []
    word = write_word_in_op_id(op.op_id)
    if word is None:
        return []
    return [
        f"the id says it writes ({word!r}) but the operation is 'safe'; make it a change, "
        "or add a real read to READS_WITH_A_WRITE_WORD with a reason"
    ]


CHECKS: Final[Mapping[str, Any]] = {
    "summary": check_summary,
    "field_descriptions": check_field_descriptions,
    "response_schema": check_response_schema,
    "llm_instructions": check_llm_instructions,
    "examples": check_examples,
    "no_ticket_numbers": check_no_ticket_numbers,
    "requires_approval": check_requires_approval,
    "group": check_group,
    "preview_builder": check_preview_builder,
    "write_name": check_write_name,
}


def check_operation(op: OperationContract) -> dict[str, list[str]]:
    """Return the problems of *op*, by rule. A rule with no problem is left out."""
    result: dict[str, list[str]] = {}
    for rule in RULES:
        problems = CHECKS[rule](op)
        if problems:
            result[rule] = problems
    return result


# ---------------------------------------------------------------------------
# Gap list
# ---------------------------------------------------------------------------

Gap = tuple[str, str, str]


def load_gaps(path: Path = GAPS_FILE) -> set[Gap]:
    """Read the gap list: ``{connector: {op_id: [rule, ...]}}``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gaps: set[Gap] = set()
    for connector, ops in data.items():
        for op_id, rules in (ops or {}).items():
            for rule in rules or ():
                gaps.add((connector, op_id, rule))
    return gaps


def compare_with_gaps(
    problems: Mapping[Gap, list[str]],
    known: set[Gap],
) -> list[str]:
    """Return one message per new gap and per listed gap that is gone."""
    messages: list[str] = []
    for gap in sorted(set(problems) - known):
        connector, op_id, rule = gap
        detail = "; ".join(problems[gap])
        messages.append(f"NEW GAP {connector} {op_id} rule {rule!r}: {detail}")
    for connector, op_id, rule in sorted(known - set(problems)):
        messages.append(
            f"GONE {connector} {op_id} rule {rule!r}: the gap is fixed or the operation "
            f"no longer exists; remove this entry from {GAPS_FILE.name}"
        )
    return messages


def find_problems(ops: list[OperationContract]) -> dict[Gap, list[str]]:
    found: dict[Gap, list[str]] = {}
    for op in ops:
        for rule, problems in check_operation(op).items():
            found[(op.connector, op.op_id, rule)] = problems
    return found


# ---------------------------------------------------------------------------
# Live check against what MEHO registers at start-up
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _registered_operations() -> list[OperationContract]:
    await run_typed_op_registrars(embedding_service=_stub_embedding_service())
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.tenant_id.is_(None),
                        EndpointDescriptor.source_kind.in_(("typed", "composite")),
                    )
                )
            )
            .scalars()
            .all()
        )
        groups = {g.id: g for g in (await session.execute(select(OperationGroup))).scalars()}
    ops: list[OperationContract] = []
    for row in rows:
        group = groups.get(row.group_id) if row.group_id else None
        ops.append(
            OperationContract(
                connector=row.impl_id,
                op_id=row.op_id,
                summary=row.summary,
                description=row.description,
                parameter_schema=row.parameter_schema or {},
                response_schema=row.response_schema,
                llm_instructions=row.llm_instructions,
                safety_level=row.safety_level,
                requires_approval=row.requires_approval,
                group_text=group.when_to_use if group is not None else None,
                has_preview_builder=row.op_id in _PREVIEW_BUILDERS,
            )
        )
    return ops


@pytest.mark.asyncio
async def test_operation_contracts_match_the_gap_list() -> None:
    """Every hand-written operation meets the ten rules, or its gap is in the list."""
    ops = await _registered_operations()
    assert ops, "no typed or multi-step operation was registered"

    gaps = load_gaps()
    seen_connectors = {op.connector for op in ops}
    unknown = {connector for connector, _, _ in gaps} - seen_connectors
    assert not unknown, f"the gap list names connectors that registered nothing: {sorted(unknown)}"

    messages = compare_with_gaps(find_problems(ops), gaps)
    assert not messages, (
        "operation contracts do not match "
        f"tests/data/{GAPS_FILE.name}. Fix a new gap in the operation; "
        "never add it to the list. Remove an entry once its gap is fixed.\n" + "\n".join(messages)
    )


@pytest.mark.asyncio
async def test_reads_with_a_write_word_are_registered_and_safe() -> None:
    """The allow list names only real, 'safe' operations, so a stale entry is caught."""
    safe_ops = {op.op_id for op in await _registered_operations() if op.safety_level == "safe"}
    stale = sorted(set(READS_WITH_A_WRITE_WORD) - safe_ops)
    assert not stale, f"not registered as a 'safe' operation; remove from the allow list: {stale}"


def test_gap_list_names_only_known_rules() -> None:
    """A typo in a rule name must not hide a gap."""
    assert {rule for _, _, rule in load_gaps()} <= set(RULES)
