# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The policy file (``policy: v1``): the strict parser, the floors, the plan and the apply.

* The parser: a repeated key, a YAML anchor and an unknown key are each a
  mistake; every mistake names the path. A stored entry that is not a map
  is refused.
* The floors (:func:`~meho_backplane.policy.validate.check_document`): each
  message, an unknown category and an unknown level.
* The plan and the apply (:mod:`meho_backplane.policy.service`): the diff
  with counts, the whole-file replace, "No change.", the check that the
  stored policy did not change after the plan, and the last 20 versions.
* A bad stored document makes a read fail with the document and the entry
  named.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PolicyRevision, TenantPolicy
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy import validate as validate_module
from meho_backplane.policy.classification import (
    OpCoordinates,
    Taxonomy,
    classify,
    taxonomy_from_documents,
)
from meho_backplane.policy.document import (
    canonical_sha256,
    document_of,
    file_sha256,
    parse_policy_file,
)
from meho_backplane.policy.service import (
    KEEP_REVISIONS,
    NO_CHANGE,
    POLICY_CHANGED,
    PolicyApplyError,
    apply_policy,
    build_plan,
    export_policy,
    list_history,
)
from meho_backplane.policy.store import load_classification
from meho_backplane.policy.validate import KnownOp, check_document
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006039")
_FIX = {"product": "polfix", "version": "1.x", "impl_id": "polfix"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _parse(text: str) -> list[str]:
    return parse_policy_file(text.encode()).mistakes


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_a_valid_file_parses() -> None:
    parsed = parse_policy_file(
        b"policy: v1\n"
        b"description: Small team\n"
        b"taxonomy:\n  categories: [network, storage]\n"
        b"classifications:\n"
        b"  - match: {connector: 'k8s-*', op: 'k8s.deployment.*'}\n"
        b"    category: network\n"
        b"    labels: {team: platform}\n"
        b"    always_wait: true\n"
    )
    assert parsed.mistakes == []
    assert parsed.model is not None
    assert document_of(parsed.model) == {
        "description": "Small team",
        "taxonomy": {"categories": ["network", "storage"]},
        "classifications": [
            {
                "match": {"connector": "k8s-*", "op": "k8s.deployment.*"},
                "category": "network",
                "labels": {"team": "platform"},
                "always_wait": True,
            }
        ],
    }


def test_a_repeated_key_is_a_mistake() -> None:
    mistakes = _parse("policy: v1\nclassifications: []\nclassifications: []\n")
    assert mistakes == ["Line 3: the key `classifications` appears twice in the same block."]


def test_an_anchor_is_a_mistake() -> None:
    mistakes = _parse(
        "policy: v1\nclassifications:\n  - &a {match: {op: x}, category: network}\n  - *a\n"
    )
    assert len(mistakes) == 1
    assert "anchors (`&`) and aliases (`*`) are not accepted" in mistakes[0]


@pytest.mark.parametrize(
    ("text", "sentence"),
    [
        ("policy: v1\nroles: []\n", "`roles` is not a known key."),
        (
            "policy: v1\nclassifications:\n  - match: {op: x}\n    colour: red\n",
            "`classifications[0].colour` is not a known key.",
        ),
        (
            "policy: v1\nclassifications:\n  - match: {host: x}\n    category: network\n",
            "`classifications[0].match.host` is not a known key.",
        ),
        ("classifications: []\n", "`policy` is required."),
        ("policy: v2\n", "`policy` is not a file format this MEHO reads; use `v1`."),
        (
            "policy: v1\nclassifications:\n  - match: {}\n    category: network\n",
            "`classifications[0].match`: needs at least one of connector, group or op.",
        ),
        (
            "policy: v1\nclassifications:\n  - match: {op: x}\n",
            "`classifications[0]`: sets nothing: give category, labels, risk or always_wait.",
        ),
        (
            "policy: v1\nclassifications:\n  - match: {op: x}\n    always_wait: 'yes'\n",
            "`classifications[0].always_wait` must be true or false.",
        ),
        (
            "policy: v1\ntaxonomy: {categories: [Network]}\n",
            "`taxonomy.categories`: holds a name that is not a category name",
        ),
    ],
)
def test_each_mistake_is_one_sentence_with_the_path(text: str, sentence: str) -> None:
    mistakes = _parse(text)
    assert any(m.startswith(sentence) for m in mistakes), mistakes


@pytest.mark.parametrize(
    ("labels", "sentence"),
    [
        ("{team: " + "x" * 256 + "}", "The value of label 'team' must have 1 to 255 characters."),
        ("{" + "k" * 64 + ": ops}", "must have 1 to 63 characters."),
        (
            "{" + ", ".join(f"k{i}: v" for i in range(33)) + "}",
            "At most 32 labels are allowed.",
        ),
    ],
)
def test_labels_are_checked_like_the_single_edit(labels: str, sentence: str) -> None:
    mistakes = _parse(
        "policy: v1\nclassifications:\n"
        "  - {match: {op: x}, category: network}\n"
        f"  - {{match: {{op: y}}, labels: {labels}}}\n"
    )
    assert len(mistakes) == 1, mistakes
    assert mistakes[0].startswith("`classifications[1].labels`"), mistakes
    assert sentence in mistakes[0], mistakes


def test_an_entry_that_is_not_a_map_is_refused() -> None:
    mistakes = _parse("policy: v1\nclassifications:\n  - just text\n")
    assert mistakes == ["`classifications[0]` must be a mapping of named keys."]


def test_a_repeated_match_is_a_mistake() -> None:
    mistakes = _parse(
        "policy: v1\nclassifications:\n"
        "  - {match: {op: x}, category: network}\n"
        "  - {match: {op: x}, always_wait: true}\n"
    )
    assert mistakes == [
        "`classifications[1].match` repeats `classifications[0]`; list each match once."
    ]


def test_the_settings_file_still_refuses_a_policy_key() -> None:
    from meho_backplane.instance_settings.schema import parse_settings_file

    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants: []\npolicy: v1\n"
    )
    assert "`policy` is not a known key." in parsed.mistakes


def test_same_meaning_has_the_same_sha() -> None:
    one = parse_policy_file(
        b"policy: v1\nclassifications:\n  - {match: {op: x}, category: network}\n"
    )
    two = parse_policy_file(
        b"# a comment\npolicy: 'v1'\ndescription: ''\nclassifications:\n"
        b"  - category: network\n    match:\n      op: x\n"
    )
    assert one.model is not None and two.model is not None
    assert canonical_sha256(document_of(one.model)) == canonical_sha256(document_of(two.model))
    assert canonical_sha256(None) == canonical_sha256({"classifications": []})


# ---------------------------------------------------------------------------
# The floors
# ---------------------------------------------------------------------------


def _op(op_id: str, level: str = "safe", **flags: bool) -> KnownOp:
    return KnownOp(
        coords=OpCoordinates("polfix-1.x", "compute", op_id),
        safety_level=level,
        shows_secret=flags.get("secret", False),
        caller_address=flags.get("address", False),
    )


def _check(entry: dict[str, Any], ops: list[KnownOp], scope: Any = "tenant") -> list[str]:
    document = {"classifications": [entry]}
    return check_document(document, scope=scope, install_document=None, ops=ops)


@pytest.mark.parametrize("loosen", [{"risk": "safe"}, {"always_wait": False}])
def test_a_secret_op_stays_waiting(loosen: dict[str, Any]) -> None:
    mistakes = _check({"match": {"op": "polfix.*"}, **loosen}, [_op("polfix.pw.read", secret=True)])
    assert mistakes == [
        "`classifications[0]` would make `polfix.pw.read` safe or not waiting. "
        "`polfix.pw.read` shows a secret, so it stays at least caution and always waits."
    ]


def test_a_caller_address_op_stays_waiting() -> None:
    mistakes = _check(
        {"match": {"op": "polfix.*"}, "always_wait": False}, [_op("polfix.fetch", address=True)]
    )
    assert mistakes == [
        "`classifications[0]`: `polfix.fetch` connects to an address the caller gives, so it "
        "stays at least caution and always waits."
    ]


def test_a_file_cannot_lower_a_level() -> None:
    mistakes = _check(
        {"match": {"op": "polfix.*"}, "risk": "caution"}, [_op("polfix.vm.delete", "dangerous")]
    )
    assert mistakes == [
        "`classifications[0]` would set `polfix.vm.delete` to caution, below its level in the "
        "code (dangerous). A file can raise a risk level, not lower it. To lower it, open a "
        "pull request that changes the code."
    ]


def test_a_file_can_raise_a_level_and_add_a_wait() -> None:
    ops = [_op("polfix.vm.list"), _op("polfix.pw.read", "caution", secret=True)]
    assert (
        _check({"match": {"op": "polfix.*"}, "risk": "dangerous", "always_wait": True}, ops) == []
    )


def test_unknown_category_and_level_are_one_sentence_each() -> None:
    mistakes = _check({"match": {"op": "x"}, "category": "cars", "risk": "huge"}, [])
    assert mistakes == [
        (
            "`classifications[0].risk` `huge` is not a risk level; use safe, caution, dangerous "
            "or destructive."
        ),
        (
            "`classifications[0].category` `cars` is not in the taxonomy (network, storage, "
            "compute, identity, secrets, other)."
        ),
    ]


def test_a_tenant_file_cannot_set_the_default_category() -> None:
    document = {"taxonomy": {"default": "network"}, "classifications": []}
    mistakes = check_document(document, scope="tenant", install_document=None, ops=[])
    assert mistakes == [
        "`taxonomy.default` belongs to the install's policy file; a tenant's file can only add "
        "categories."
    ]


def test_an_install_default_alone_is_used() -> None:
    document = {"taxonomy": {"default": "network"}, "classifications": []}
    assert check_document(document, scope="install", install_document=None, ops=[]) == []
    taxonomy = taxonomy_from_documents(document, None)
    assert taxonomy.default == "network"
    unmatched = classify(OpCoordinates("polfix-1.x", None, "polfix.vm.list"), [], taxonomy)
    assert unmatched.category == "network"


def test_install_categories_without_their_default_are_a_mistake() -> None:
    document = {"taxonomy": {"categories": ["network"]}, "classifications": []}
    mistakes = check_document(document, scope="install", install_document=None, ops=[])
    assert mistakes == [
        (
            "`taxonomy.default` `other` is not in the categories (network). "
            "Set `taxonomy.default` to one of them."
        )
    ]
    fixed = {"taxonomy": {"categories": ["network"], "default": "network"}}
    assert taxonomy_from_documents(fixed, None) == Taxonomy(("network",), "network")
    assert check_document(fixed, scope="install", install_document=None, ops=[]) == []


def test_a_conflict_in_the_file_is_a_mistake() -> None:
    document = {
        "classifications": [
            {"match": {"op": "polfix.vm.*"}, "category": "network"},
            {"match": {"op": "polfix.vm.?ist"}, "category": "storage"},
        ]
    }
    mistakes = check_document(
        document, scope="tenant", install_document=None, ops=[_op("polfix.vm.list")]
    )
    assert len(mistakes) == 1 and "make one of them more specific" in mistakes[0]


# ---------------------------------------------------------------------------
# Plan, apply, export, history (database)
# ---------------------------------------------------------------------------


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _register(embedding: AsyncMock, op_id: str, **extra: Any) -> None:
    await register_typed_operation(
        **_FIX,
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema=extra.pop("parameter_schema", {"type": "object", "properties": {}}),
        group_key="compute",
        when_to_use="Fixture group.",
        embedding_service=embedding,
        **extra,
    )


_FILE_A = (
    "policy: v1\n"
    "taxonomy: {categories: [team-net]}\n"
    "classifications:\n"
    "  - {match: {connector: polfix-1.x}, category: compute}\n"
    "  - {match: {op: polfix.vm.delete}, always_wait: true}\n"
)
_FILE_B = (
    "policy: v1\n"
    "classifications:\n"
    "  - {match: {connector: polfix-1.x}, category: network}\n"
    "  - {match: {op: polfix.vm.*}, labels: {team: ops}}\n"
)


async def _apply(content: str, *, based_on: str | None = None, scope: Any = "tenant") -> Any:
    async with get_sessionmaker()() as session, session.begin():
        if based_on is None:
            plan = await build_plan(
                session, scope=scope, tenant_id=_TENANT, content=content.encode()
            )
            based_on = plan.based_on
        return await apply_policy(
            session,
            scope=scope,
            tenant_id=_TENANT,
            content=content.encode(),
            expected_sha256=file_sha256(content.encode()),
            based_on=based_on,
            imported_by="admin-a",
            approved_by="admin-b",
        )


async def _stored() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id == _TENANT))
        ).scalar_one_or_none()
    return None if row is None else row.document


@pytest.mark.asyncio
async def test_the_plan_counts_matches_and_changes(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    await _register(embedding, "polfix.vm.delete", safety_level="dangerous", requires_approval=True)
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=_FILE_A.encode()
        )
    assert plan.mistakes == []
    assert plan.diff is not None
    added = plan.diff["classifications"]["added"]
    assert [(a["matches"], a["changes"]) for a in added] == [(2, 2), (1, 1)]
    assert plan.diff["taxonomy"]["added"] == ["team-net"]
    assert plan.diff["operations_changed"] == 2
    assert await _stored() is None


@pytest.mark.asyncio
async def test_the_import_replaces_the_whole_file(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    await _register(embedding, "polfix.vm.delete", safety_level="dangerous", requires_approval=True)
    first = await _apply(_FILE_A)
    assert first["applied"] is True
    second = await _apply(_FILE_B)
    assert second["applied"] is True
    assert second["summary"]["classifications_removed"] == 1
    stored = await _stored()
    assert stored == {
        "classifications": [
            {"match": {"connector": "polfix-1.x"}, "category": "network"},
            {"match": {"op": "polfix.vm.*"}, "labels": {"team": "ops"}},
        ]
    }

    async with get_sessionmaker()() as session:
        history = await list_history(session, scope="tenant", tenant_id=_TENANT, limit=20)
    assert [h["file"] for h in history] == [_FILE_B, _FILE_A]
    async with get_sessionmaker()() as session:
        other = uuid.UUID("00000000-0000-0000-0000-000000006040")
        assert await list_history(session, scope="tenant", tenant_id=other, limit=20) == []
        assert await list_history(session, scope="install", tenant_id=None, limit=20) == []
    assert history[0]["imported_by_sub"] == "admin-a"
    assert history[0]["approved_by_sub"] == "admin-b"
    removed = history[0]["diff"]["classifications"]["removed"]
    assert [r["entry"]["match"] for r in removed] == [{"op": "polfix.vm.delete"}]


@pytest.mark.asyncio
async def test_the_same_meaning_twice_is_no_change(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    await _apply(_FILE_B)
    reordered = _FILE_B.replace("policy: v1\n", "# same rules\npolicy: v1\ndescription: ''\n")
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=reordered.encode()
        )
    assert plan.no_change is True
    result = await _apply(reordered)
    assert result == {
        "scope": "tenant",
        "applied": False,
        "message": NO_CHANGE,
        "revision_id": None,
    }
    async with get_sessionmaker()() as session:
        count = len((await session.execute(select(PolicyRevision))).scalars().all())
    assert count == 1


@pytest.mark.asyncio
async def test_reordered_rules_and_categories_are_no_change(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    await _register(embedding, "polfix.vm.delete", safety_level="dangerous", requires_approval=True)
    first = (
        "policy: v1\n"
        "taxonomy: {categories: [team-net, team-db]}\n"
        "classifications:\n"
        "  - {match: {connector: polfix-1.x}, category: network}\n"
        "  - {match: {op: polfix.vm.delete}, always_wait: true}\n"
    )
    reordered = (
        "policy: v1\n"
        "taxonomy: {categories: [team-db, team-net]}\n"
        "classifications:\n"
        "  - {match: {op: polfix.vm.delete}, always_wait: true}\n"
        "  - {match: {connector: polfix-1.x}, category: network}\n"
    )
    assert (await _apply(first))["applied"] is True
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=reordered.encode()
        )
    assert plan.mistakes == []
    assert plan.no_change is True
    assert (await _apply(reordered))["applied"] is False
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(PolicyRevision))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_a_policy_changed_after_the_plan_is_not_applied(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=_FILE_A.encode()
        )
    await _apply(_FILE_B)
    with pytest.raises(PolicyApplyError, match=re.escape(POLICY_CHANGED)):
        await _apply(_FILE_A, based_on=plan.based_on)
    assert (await _stored() or {})["classifications"][1]["labels"] == {"team": "ops"}


@pytest.mark.asyncio
async def test_a_file_that_does_not_match_its_sha_is_refused() -> None:
    async with get_sessionmaker()() as session, session.begin():
        with pytest.raises(PolicyApplyError, match="does not match its SHA-256"):
            await apply_policy(
                session,
                scope="tenant",
                tenant_id=_TENANT,
                content=_FILE_B.encode(),
                expected_sha256="0" * 64,
                based_on=canonical_sha256(None),
                imported_by="a",
                approved_by="b",
            )


@pytest.mark.asyncio
async def test_only_the_last_versions_are_kept(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    for index in range(KEEP_REVISIONS + 2):
        await _apply(f"policy: v1\ndescription: version {index}\n")
    async with get_sessionmaker()() as session:
        history = await list_history(session, scope="tenant", tenant_id=_TENANT, limit=50)
    assert len(history) == KEEP_REVISIONS
    assert history[0]["file"].endswith(f"version {KEEP_REVISIONS + 1}\n")


@pytest.mark.asyncio
async def test_the_floors_run_against_the_stored_operations(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(
        embedding,
        "polfix.fetch",
        parameter_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "x-meho-caller-address": True}},
        },
    )
    await _register(embedding, "polfix.pw.read")
    monkeypatch.setattr(
        validate_module, "has_credential_read_floor", lambda op_id: op_id == "polfix.pw.read"
    )
    content = b"policy: v1\nclassifications:\n  - {match: {op: 'polfix.*'}, always_wait: false}\n"
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=content)
    assert len(plan.mistakes) == 2
    assert "`polfix.fetch` connects to an address the caller gives" in plan.mistakes[0]
    assert "`polfix.pw.read` shows a secret" in plan.mistakes[1]
    assert plan.diff is None


@pytest.mark.asyncio
async def test_export_writes_a_file_that_imports_again(embedding: AsyncMock) -> None:
    await _register(embedding, "polfix.vm.list")
    await _apply(_FILE_A)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=text.encode())
    assert text.startswith("policy: v1\n")
    assert plan.mistakes == []
    assert plan.no_change is True


@pytest.mark.asyncio
async def test_a_bad_stored_document_names_the_document_and_the_entry() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={"classifications": [{"match": {"op": "x"}, "risk": "huge"}]},
                set_by_sub="fixture",
            )
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError) as caught:
            await load_classification(session, _TENANT)
    message = str(caught.value)
    assert f"The stored policy of tenant {_TENANT} is not valid" in message
    assert "tenant:classifications[0]" in message

    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=_FILE_B.encode()
        )
    assert plan.mistakes == []
    assert any("is not valid" in w and "The import replaces it." in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_a_stored_entry_that_is_not_a_map_is_not_valid() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT, document={"classifications": ["network"]}, set_by_sub="fixture"
            )
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError, match=r"classifications\[0\]: an entry must be a map"):
            await load_classification(session, _TENANT)
