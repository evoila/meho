# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one switch ``CUSTOM_POLICY`` (#6957).

* With every switch off, the three parts of the policy are off.
* With only ``CUSTOM_POLICY`` on, all three are on.
* With only one old switch on, only that part is on.
* ``GET /ready`` shows ``features.policy.custom_policy``.
* ``policy.apply``, ``policy.classify`` and ``policy.classify_preview``
  register with only ``CUSTOM_POLICY`` on, and not with every switch off.
* ``custom_policy_on`` and ``custom_policy_on_in_environment`` say off with
  every switch off and on with only ``CUSTOM_POLICY`` on, and read each value
  of ``CUSTOM_POLICY`` the same way.
* No code outside :mod:`meho_backplane.policy.switches` and the settings
  reads the four switches directly. The settings file holds only the
  definition of ``CUSTOM_POLICY``, also when a read is split over lines,
  and the search reads the whole settings file.
* Each file that reads a settings field by a name it gets while the code
  runs is on a fixed list.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.policy.apply import (
    POLICY_APPLY_OP_ID,
    register_policy_apply_operation,
)
from meho_backplane.connectors.policy.classify import (
    POLICY_CLASSIFY_OP_ID,
    register_policy_classify_operation,
)
from meho_backplane.connectors.policy.preview import (
    POLICY_CLASSIFY_PREVIEW_OP_ID,
    register_policy_classify_preview_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.features import CATEGORY_WITHOUT_CLASSIFICATION, build_features_block
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.tools._preview_policy_fields import (
    PREVIEW_POLICY_OUTPUT_SCHEMA,
    policy_schema_for_settings,
)
from meho_backplane.policy import bindings, routing, shadow, store
from meho_backplane.policy.routing import category_routing_warnings
from meho_backplane.policy.switches import custom_policy_on, custom_policy_on_in_environment
from meho_backplane.settings import get_settings

_SWITCHES = (
    "CUSTOM_POLICY",
    "POLICY_CLASSIFICATION",
    "POLICY_ENGINE_SHADOW",
    "POLICY_APPROVER_ROUTING",
)

_CATEGORY_ROUTE = [{"match": {"category": "network"}, "approvers": ["Admins"]}]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in _SWITCHES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _turn_on(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, "true")
    get_settings.cache_clear()


def _parts() -> dict[str, bool]:
    """What each helper says, keyed by the old switch it stands for."""
    assert routing.routing_enabled() is bindings.routing_enabled()
    return {
        "POLICY_CLASSIFICATION": store.classification_enabled(),
        "POLICY_ENGINE_SHADOW": shadow.shadow_enabled(),
        "POLICY_APPROVER_ROUTING": routing.routing_enabled(),
    }


def test_every_switch_off_turns_every_part_off() -> None:
    assert get_settings().custom_policy is False
    assert _parts() == dict.fromkeys(_SWITCHES[1:], False)


@pytest.mark.parametrize("value", ["false", "", "no", "on-ish"])
def test_custom_policy_not_true_turns_nothing_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", value)
    get_settings.cache_clear()
    assert get_settings().custom_policy is False
    assert _parts() == dict.fromkeys(_SWITCHES[1:], False)


def test_custom_policy_alone_turns_every_part_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    settings = get_settings()
    assert settings.custom_policy is True
    assert (
        settings.policy_classification,
        settings.policy_engine_shadow,
        settings.policy_approver_routing,
    ) == (False, False, False)
    assert _parts() == dict.fromkeys(_SWITCHES[1:], True)


def test_custom_policy_functions_off_with_every_switch_off() -> None:
    assert custom_policy_on(get_settings()) is False
    assert custom_policy_on_in_environment() is False


def test_custom_policy_functions_on_with_only_custom_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    assert custom_policy_on(get_settings()) is True
    assert custom_policy_on_in_environment() is True


@pytest.mark.parametrize(
    ("value", "on"),
    [
        ("true", True),
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        (" true ", True),
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("enabled", False),
        ("on-ish", False),
    ],
)
def test_custom_policy_functions_read_each_value_like_the_settings(
    monkeypatch: pytest.MonkeyPatch, value: str | None, on: bool
) -> None:
    if value is not None:
        monkeypatch.setenv("CUSTOM_POLICY", value)
    get_settings.cache_clear()
    assert custom_policy_on_in_environment() is on
    assert custom_policy_on(get_settings()) is on


@pytest.mark.parametrize("old_switch", _SWITCHES[1:])
def test_custom_policy_functions_off_with_only_an_old_switch(
    monkeypatch: pytest.MonkeyPatch, old_switch: str
) -> None:
    _turn_on(monkeypatch, old_switch)
    assert custom_policy_on(get_settings()) is False
    assert custom_policy_on_in_environment() is False


@pytest.mark.parametrize("old_switch", _SWITCHES[1:])
def test_one_old_switch_turns_on_only_its_part(
    monkeypatch: pytest.MonkeyPatch, old_switch: str
) -> None:
    _turn_on(monkeypatch, old_switch)
    assert _parts() == {name: name == old_switch for name in _SWITCHES[1:]}


def test_ready_shows_custom_policy_off_by_default() -> None:
    block = build_features_block(get_settings())["policy"]
    assert block["custom_policy"] is False
    assert (block["classification"], block["engine_shadow"], block["approver_routing"]) == (
        False,
        False,
        False,
    )
    assert block["notes"] == []


def test_ready_shows_the_parts_on_with_custom_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    block = build_features_block(get_settings())["policy"]
    assert block["custom_policy"] is True
    assert (block["classification"], block["engine_shadow"], block["approver_routing"]) == (
        True,
        True,
        True,
    )
    assert block["notes"] == []


def test_category_note_stays_for_routing_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only ``POLICY_APPROVER_ROUTING`` on, ``/ready`` shows the category note
    and the import gives one warning.
    """
    _turn_on(monkeypatch, "POLICY_APPROVER_ROUTING")
    assert build_features_block(get_settings())["policy"]["notes"] == [
        CATEGORY_WITHOUT_CLASSIFICATION
    ]
    assert len(category_routing_warnings(_CATEGORY_ROUTE)) == 1


def test_custom_policy_has_no_category_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    assert category_routing_warnings(_CATEGORY_ROUTE) == []


def _preview_tool() -> ToolDefinition:
    return ToolDefinition.model_construct(
        name="preview_operation",
        outputSchema={"type": "object", "properties": {"x": {}, **PREVIEW_POLICY_OUTPUT_SCHEMA}},
    )


def test_preview_fields_follow_custom_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def keys() -> set[str]:
        schema = policy_schema_for_settings(_preview_tool()).outputSchema
        assert schema is not None
        return set(schema["properties"])

    assert keys() == {"x"}
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    assert keys() == {"x", *PREVIEW_POLICY_OUTPUT_SCHEMA}


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _register_policy_ops(embedding: AsyncMock) -> set[str]:
    await register_policy_apply_operation(embedding_service=embedding)
    await register_policy_classify_operation(embedding_service=embedding)
    await register_policy_classify_preview_operation(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.op_id.in_(
                    [POLICY_APPLY_OP_ID, POLICY_CLASSIFY_OP_ID, POLICY_CLASSIFY_PREVIEW_OP_ID]
                )
            )
        )
    return set(rows.scalars())


@pytest.mark.asyncio
async def test_policy_ops_register_with_only_custom_policy(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(monkeypatch, "CUSTOM_POLICY")
    assert await _register_policy_ops(embedding) == {
        POLICY_APPLY_OP_ID,
        POLICY_CLASSIFY_OP_ID,
        POLICY_CLASSIFY_PREVIEW_OP_ID,
    }


@pytest.mark.asyncio
async def test_policy_ops_not_registered_with_every_switch_off(embedding: AsyncMock) -> None:
    assert await _register_policy_ops(embedding) == set()


_SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

#: The only files that may read the four switches directly.
_ALLOWED = {_SRC / "settings.py", _SRC / "policy" / "switches.py"}

_DIRECT_READ = re.compile(
    r"settings\w*(?:\(\))?\.(policy_classification|policy_engine_shadow|policy_approver_routing"
    r"|custom_policy)\b"
    r"|(?:environ(?:\.get\(|\[)|getenv\()\s*[\"'](POLICY_CLASSIFICATION|POLICY_ENGINE_SHADOW|"
    r"POLICY_APPROVER_ROUTING|CUSTOM_POLICY)[\"']"
)


@pytest.mark.parametrize(
    "line",
    [
        '"custom_policy": settings.custom_policy,',
        "if get_settings().custom_policy:",
        'on = os.environ.get("CUSTOM_POLICY")',
        "on = os.environ['CUSTOM_POLICY']",
        'on = os.getenv("CUSTOM_POLICY")',
    ],
)
def test_direct_read_pattern_finds_custom_policy(line: str) -> None:
    assert _DIRECT_READ.search(line)


@pytest.mark.parametrize(
    "line",
    [
        '"custom_policy": custom_policy_on(settings),',
        "The CUSTOM_POLICY setting turns on all three policy parts.",
    ],
)
def test_direct_read_pattern_skips_calls_and_texts(line: str) -> None:
    assert _DIRECT_READ.search(line) is None


#: A read of ``CUSTOM_POLICY`` that the line search above misses: the whole
#: environment name as a string (also in a read split over lines, or kept in a
#: constant), the setting on any name, and the setting name as a string (for
#: ``getattr`` and ``model_dump()[...]``). A sentence that names the switch
#: inside a longer text is no match.
_CUSTOM_POLICY_READ = re.compile(r"([\"'])CUSTOM_POLICY\1|\.custom_policy\b|([\"'])custom_policy\2")

#: The only such reads outside :mod:`meho_backplane.policy.switches`, each
#: allowed once: the definition of the setting, and the field of ``/ready``
#: with the example in the docstring of that function.
_CUSTOM_POLICY_ALLOWED: dict[str, tuple[re.Pattern[str], ...]] = {
    "settings.py": (
        re.compile(r'custom_policy=parse_bool_env\(\s*os\.environ\.get\("CUSTOM_POLICY"\),\s*\)'),
    ),
    "features.py": (
        re.compile(r'"custom_policy": custom_policy_on\(settings\),'),
        re.compile(r'"custom_policy": <bool>,'),
    ),
}


def _custom_policy_reads(text: str, allowed: tuple[re.Pattern[str], ...] = ()) -> list[int]:
    """The line numbers of each read in ``text``, after the first match of each
    allowed text is blanked out.
    """
    for pattern in allowed:
        match = pattern.search(text)
        if match:
            start, end = match.span()
            text = text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]
    return [text.count("\n", 0, m.start()) + 1 for m in _CUSTOM_POLICY_READ.finditer(text)]


@pytest.mark.parametrize(
    "text",
    [
        'on = parse_bool_env(\n    os.environ.get(\n        "CUSTOM_POLICY",\n    )\n)',
        'if "CUSTOM_POLICY" in os.environ:',
        "_NAME = 'CUSTOM_POLICY'\non = os.environ.get(_NAME)",
        'on = getattr(settings, "custom_policy")',
        "on = settings.model_dump()['custom_policy']",
        "on = cfg.custom_policy",
        "on = (\n    cfg\n    .custom_policy\n)",
    ],
)
def test_custom_policy_read_pattern_finds_wider_reads(text: str) -> None:
    assert _custom_policy_reads(text)


@pytest.mark.parametrize(
    "text",
    [
        "on = custom_policy_on(get_settings())",
        "The CUSTOM_POLICY setting turns on all three policy parts.",
        '"Only with CUSTOM_POLICY or POLICY_ENGINE_SHADOW on: the rule"',
        "from meho_backplane.policy.switches import custom_policy_on",
        "on = switches.custom_policy_on_in_environment()",
    ],
)
def test_custom_policy_read_pattern_skips_calls_and_texts(text: str) -> None:
    assert _custom_policy_reads(text) == []


def test_settings_allow_only_the_definition_of_custom_policy() -> None:
    allowed = _CUSTOM_POLICY_ALLOWED["settings.py"]
    definition = 'custom_policy=parse_bool_env(\n    os.environ.get("CUSTOM_POLICY"),\n),'
    assert _custom_policy_reads(definition, allowed) == []
    assert _custom_policy_reads(f"{definition}\n{definition}", allowed) == [5]
    assert _custom_policy_reads(f'{definition}\non = os.getenv("CUSTOM_POLICY")', allowed) == [4]
    assert _custom_policy_reads(f"{definition}\non = self.custom_policy", allowed) == [4]


def _files_searched_for_custom_policy() -> dict[str, str]:
    """The text of each file the search for reads of ``CUSTOM_POLICY`` reads,
    by its path under the package.
    """
    return {
        path.relative_to(_SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_SRC.rglob("*.py"))
        if path != _SRC / "policy" / "switches.py"
    }


def _custom_policy_reads_in(files: dict[str, str]) -> list[str]:
    """``<path>:<line>`` of each read of ``CUSTOM_POLICY`` in *files*."""
    return [
        f"{name}:{number}"
        for name, text in files.items()
        for number in _custom_policy_reads(text, _CUSTOM_POLICY_ALLOWED.get(name, ()))
    ]


def test_the_search_finds_a_second_read_in_settings() -> None:
    """The search reads all of ``settings.py``, not only the definition."""
    files = _files_searched_for_custom_policy()
    assert "settings.py" in files, "the search must read settings.py"
    text = files["settings.py"]
    end = text.count("\n")
    files["settings.py"] = (
        f'{text}\ncustom_policy=parse_bool_env(\n    os.environ.get("CUSTOM_POLICY"),\n)'
        '\non = os.getenv("CUSTOM_POLICY")\n'
    )

    found = [read for read in _custom_policy_reads_in(files) if read.startswith("settings.py:")]

    assert found == [f"settings.py:{end + 3}", f"settings.py:{end + 5}"]


def test_no_code_reads_the_switches_directly() -> None:
    sources = sorted(_SRC.rglob("*.py"))
    found = [
        f"{path.relative_to(_SRC)}:{number}"
        for path in sources
        if path not in _ALLOWED
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _DIRECT_READ.search(line)
    ]
    found += _custom_policy_reads_in(_files_searched_for_custom_policy())
    assert found == [], "ask meho_backplane.policy.switches instead"


#: ``getattr(`` or ``model_dump(`` on the settings object. Code that does this
#: can read a field by a name it gets while it runs, which no search finds.
_SETTINGS_BY_NAME = re.compile(
    r"getattr\(\s*(?:[\w.]*\.)?\w*settings\w*(?:\(\))?\s*,"
    r"|\w*settings\w*(?:\(\))?\s*\.model_dump\("
)

#: The files that read settings fields by name, each checked by a reviewer:
#: ``GET /version`` lists the settings that change what an agent sees, an
#: operation parameter can depend on a switch, and the dashboard file compares
#: its fields with the settings.
_READS_SETTINGS_BY_NAME = {
    "checks/dashboard_file.py",
    "operations/switch_params.py",
    "version.py",
}


@pytest.mark.parametrize(
    "text",
    [
        "getattr(settings, name)",
        "getattr(\n    get_settings(),\n    setting,\n    False,\n)",
        "getattr(self.settings, name)",
        "settings.model_dump(include=fields)",
        "get_settings().model_dump()",
    ],
)
def test_settings_by_name_pattern_finds_reads(text: str) -> None:
    assert _SETTINGS_BY_NAME.search(text)


@pytest.mark.parametrize(
    "text",
    ["getattr(row, field)", "body.model_dump(mode='json')", "settings.custom_policy_on"],
)
def test_settings_by_name_pattern_skips_other_objects(text: str) -> None:
    assert _SETTINGS_BY_NAME.search(text) is None


def test_only_listed_files_read_settings_by_name() -> None:
    found = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if _SETTINGS_BY_NAME.search(path.read_text(encoding="utf-8"))
    }
    assert found == _READS_SETTINGS_BY_NAME, "a reviewer checks each new file and adds it here"
