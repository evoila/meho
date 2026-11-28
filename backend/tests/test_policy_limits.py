# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``limits`` part of the policy file, and the other reserved keys.

* The parser: ``limits.personal_agent_up_to`` takes ``safe``, ``caution`` or
  ``dangerous``; ``destructive`` and other keys are mistakes.
  ``approvers``, ``role_bindings``, ``risk_levels``, ``custom_roles`` and
  ``baseline`` are refused with one sentence, whatever their value; the
  parser keeps ``agents`` for the checks (``test_policy_agent_rules.py``).
* The checks: a tenant's file that holds the key ``limits`` is refused; the
  install's file that holds it is refused while the switch is off.
* The import with the switch on: an install file with a limit is stored,
  exported and imported again with "No change."; the diff shows the limit.
* The reader :func:`~meho_backplane.policy.limits.personal_agent_limit`:
  ``safe`` with the switch off, without a limit, or with a stored value
  that is not a mapping; the stored level with the switch on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy.document import (
    document_of,
    file_sha256,
    parse_policy_file,
)
from meho_backplane.policy.limits import (
    LIMITS_NEED_CUSTOM_POLICY,
    limits_diff,
    personal_agent_limit,
)
from meho_backplane.policy.service import (
    NO_CHANGE,
    apply_policy,
    build_plan,
    builtin_install_document,
    export_policy,
)
from meho_backplane.policy.validate import check_document
from meho_backplane.settings import get_settings

_NOT_READ = "This version of MEHO does not read `{}` in a policy file; remove it."


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _install_file(level: str) -> str:
    return f"policy: v1\nclassifications: []\nlimits:\n  personal_agent_up_to: {level}\n"


def _document(text: str) -> dict[str, Any]:
    parsed = parse_policy_file(text.encode())
    assert parsed.model is not None, parsed.mistakes
    return document_of(parsed.model)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["safe", "caution", "dangerous"])
def test_each_limit_level_parses(level: str) -> None:
    assert _document(_install_file(level))["limits"] == {"personal_agent_up_to": level}


@pytest.mark.parametrize(
    ("text", "sentence"),
    [
        (
            _install_file("destructive"),
            "`limits.personal_agent_up_to` is not a level a limit can have; use safe, "
            "caution or dangerous. A destructive operation always waits.",
        ),
        (
            "policy: v1\nlimits: {service_agent_up_to: safe}\n",
            "`limits.service_agent_up_to` is not a known key.",
        ),
    ],
)
def test_a_wrong_limit_is_one_sentence_with_the_path(text: str, sentence: str) -> None:
    assert parse_policy_file(text.encode()).mistakes == [sentence]


@pytest.mark.parametrize(
    "key", ["approvers", "role_bindings", "risk_levels", "custom_roles", "baseline"]
)
@pytest.mark.parametrize(
    "value", ["{example-name: example-value-7}", "[example-value-7]", "example-value-7", "null"]
)
def test_a_reserved_key_is_refused_by_the_parser(key: str, value: str) -> None:
    mistakes = parse_policy_file(f"policy: v1\n{key}: {value}\n".encode()).mistakes
    assert mistakes == [_NOT_READ.format(key)]


@pytest.mark.parametrize(
    "form", ["limits: {}", "limits: null", "limits:", "limits: {personal_agent_up_to: null}"]
)
def test_the_stored_form_keeps_the_key_limits_without_a_value(form: str) -> None:
    assert _document(f"policy: v1\n{form}\n")["limits"] == {}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("custom_policy", [True, False])
def test_a_tenant_file_with_limits_is_refused(custom_policy: bool) -> None:
    mistakes = check_document(
        _document(_install_file("caution")),
        scope="tenant",
        install_document=builtin_install_document(),
        ops=[],
        custom_policy=custom_policy,
    )
    assert mistakes == [
        "`limits` belongs to the install's policy file. Only a platform admin sets the "
        "outer limit; a tenant's file stays inside it."
    ]


def test_limits_need_the_switch() -> None:
    document = _document(_install_file("caution"))
    off = check_document(document, scope="install", install_document=document, ops=[])
    on = check_document(
        document, scope="install", install_document=document, ops=[], custom_policy=True
    )
    assert off == [LIMITS_NEED_CUSTOM_POLICY]
    assert on == []


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "level"),
    [
        (None, "safe"),
        ({}, "safe"),
        ({"limits": {}}, "safe"),
        ({"limits": {"personal_agent_up_to": "dangerous"}}, "dangerous"),
        ({"limits": {"personal_agent_up_to": "destructive"}}, "safe"),
        ({"limits": ["caution"]}, "safe"),
    ],
)
def test_the_reader_says_safe_without_a_limit(document: Any, level: str) -> None:
    assert personal_agent_limit(document, custom_policy=True) == level


def test_with_the_switch_off_the_reader_says_safe() -> None:
    document = {"limits": {"personal_agent_up_to": "dangerous"}}
    assert personal_agent_limit(document) == "safe"
    assert personal_agent_limit(document, custom_policy=False) == "safe"


@pytest.mark.parametrize("stored", [["example"], "example", 7])
@pytest.mark.parametrize("custom_policy", [True, False])
def test_a_stored_document_that_is_not_a_mapping_is_no_limit(
    stored: Any, custom_policy: bool
) -> None:
    assert personal_agent_limit(stored, custom_policy=custom_policy) == "safe"
    assert limits_diff(stored, {}, custom_policy=custom_policy) == {}


def test_with_the_switch_off_the_diff_has_no_limits() -> None:
    stored = {"limits": {"personal_agent_up_to": "dangerous"}}
    assert limits_diff(stored, {}) == {}
    assert limits_diff(stored, {"limits": {"personal_agent_up_to": "safe"}}) == {}


def test_a_stored_limit_that_is_not_a_map_is_no_limit_in_the_diff() -> None:
    on = {"custom_policy": True}
    assert limits_diff({"limits": ["caution"]}, {}, **on) == {}
    assert limits_diff(
        {"limits": ["caution"]}, {"limits": {"personal_agent_up_to": "safe"}}, **on
    ) == {
        "limits": {
            "added": [{"key": "personal_agent_up_to", "value": "safe"}],
            "changed": [],
            "removed": [],
        }
    }


def test_the_shipped_install_policy_has_no_limit() -> None:
    assert "limits" not in builtin_install_document()
    assert personal_agent_limit(builtin_install_document()) == "safe"


# ---------------------------------------------------------------------------
# The import and the export
# ---------------------------------------------------------------------------


async def _apply_install(content: str, *, custom_policy: bool = True) -> dict[str, Any]:
    async with get_sessionmaker()() as session, session.begin():
        plan = await build_plan(
            session,
            scope="install",
            tenant_id=None,
            content=content.encode(),
            custom_policy=custom_policy,
        )
        return await apply_policy(
            session,
            scope="install",
            tenant_id=None,
            content=content.encode(),
            expected_sha256=file_sha256(content.encode()),
            based_on=plan.based_on,
            imported_by="platform-admin",
            approved_by="platform-admin",
            custom_policy=custom_policy,
        )


async def _stored_install() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one_or_none()
    return None if row is None else row.document


@pytest.mark.asyncio
async def test_an_install_limit_is_imported_exported_and_no_change_again() -> None:
    answer = await _apply_install(_install_file("dangerous"))
    assert answer["applied"] is True
    assert answer["summary"]["limits_added"] == 1
    stored = await _stored_install()
    assert stored is not None
    assert personal_agent_limit(stored, custom_policy=True) == "dangerous"

    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="install", tenant_id=None, custom_policy=True)
        plan = await build_plan(
            session, scope="install", tenant_id=None, content=text.encode(), custom_policy=True
        )
    assert "limits:\n  personal_agent_up_to: dangerous\n" in text
    assert plan.mistakes == []
    assert plan.no_change is True
    again = await _apply_install(text)
    assert again["message"] == NO_CHANGE


@pytest.mark.asyncio
async def test_a_lower_limit_is_read_at_once() -> None:
    await _apply_install(_install_file("dangerous"))
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="install",
            tenant_id=None,
            content=_install_file("caution").encode(),
            custom_policy=True,
        )
    assert plan.diff is not None
    assert plan.diff["limits"] == {
        "added": [],
        "changed": [{"key": "personal_agent_up_to", "before": "dangerous", "after": "caution"}],
        "removed": [],
    }
    await _apply_install(_install_file("caution"))
    assert personal_agent_limit(await _stored_install(), custom_policy=True) == "caution"


@pytest.mark.asyncio
async def test_with_the_switch_off_the_import_refuses_limits() -> None:
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session, scope="install", tenant_id=None, content=_install_file("caution").encode()
        )
    assert plan.mistakes == [LIMITS_NEED_CUSTOM_POLICY]
    assert plan.diff is None
    assert await _stored_install() is None


@pytest.mark.asyncio
async def test_a_policy_without_limits_has_the_same_diff_as_before() -> None:
    content = "policy: v1\nclassifications: []\n"
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=content.encode())
    assert plan.mistakes == []
    assert plan.diff is not None
    assert "limits" not in plan.diff
    answer = await _apply_install(content, custom_policy=False)
    assert not any(key.startswith("limits_") for key in answer["summary"])


@pytest.mark.asyncio
async def test_a_removed_limit_shows_in_the_diff() -> None:
    await _apply_install(_install_file("caution"))
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="install",
            tenant_id=None,
            content=b"policy: v1\nclassifications: []\n",
            custom_policy=True,
        )
    assert plan.mistakes == []
    assert plan.diff is not None
    assert plan.diff["limits"]["removed"] == [{"key": "personal_agent_up_to", "value": "caution"}]
