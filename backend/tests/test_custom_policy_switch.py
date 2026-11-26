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
  reads the four switches directly. For ``CUSTOM_POLICY`` the search reads
  whole files, and the settings may hold only the definition of the setting.
* A file that reads a settings field by a name built while the code runs is
  on a fixed list, because no search can see which field it reads.
"""

from __future__ import annotations

import ast
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
    ("value", "expected"),
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
def test_custom_policy_functions_read_each_value_the_same_way(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """Both functions read every value of ``CUSTOM_POLICY`` as the settings do."""
    if value is not None:
        monkeypatch.setenv("CUSTOM_POLICY", value)
    get_settings.cache_clear()
    assert custom_policy_on_in_environment() is expected
    assert custom_policy_on(get_settings()) is expected


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

#: A read of ``CUSTOM_POLICY``, searched in the whole file: the environment
#: name or the setting name as a whole string (also split over lines, in a
#: constant, in ``getattr`` or in ``model_dump()[...]``), or the setting read
#: on any name.
_CUSTOM_POLICY_READ = re.compile(
    r"[\"']CUSTOM_POLICY[\"']|[\"']custom_policy[\"']|\.custom_policy\b"
)

#: The one file that reads ``CUSTOM_POLICY`` freely.
_CUSTOM_POLICY_HOME = _SRC / "policy" / "switches.py"

#: The texts a file may hold that :data:`_CUSTOM_POLICY_READ` finds, each
#: with how many times it must be there.
_SETTINGS_DEFINITION = re.compile(
    r"custom_policy=parse_bool_env\(\s*os\.environ\.get\(\s*\"CUSTOM_POLICY\",?\s*\),?\s*\)"
)
_READY_FIELD = re.compile(r"\"custom_policy\":")
_CUSTOM_POLICY_ALLOWED: dict[Path, tuple[tuple[re.Pattern[str], int], ...]] = {
    # The definition of the setting.
    _SRC / "settings.py": ((_SETTINGS_DEFINITION, 1),),
    # The field name of ``/ready``: the dict key and the docstring example.
    _SRC / "features.py": ((_READY_FIELD, 2),),
}

#: The files that read a settings field by a name built while the code runs
#: (``getattr`` or ``model_dump`` on the settings object). A search cannot
#: see which field they read, so a reviewer checks each file on this list.
_READS_SETTINGS_BY_NAME = {
    # Every setting that changes what an agent sees, for ``GET /version``.
    "version.py",
    # The setting that an operation parameter needs.
    "operations/switch_params.py",
    # The dashboard fields of the settings.
    "checks/dashboard_file.py",
}


def _blank(text: str, pattern: re.Pattern[str]) -> tuple[str, int]:
    """Replace each match with spaces and keep the line breaks.

    Returns the new text and the number of matches.
    """
    blanked, count = pattern.subn(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    return blanked, count


def _custom_policy_reads(
    text: str, allowed: tuple[tuple[re.Pattern[str], int], ...] = ()
) -> list[str]:
    """The reads of ``CUSTOM_POLICY`` in ``text``, as ``line <n>`` texts.

    The ``allowed`` texts are taken out first. A count of allowed texts other
    than the one given is reported too.
    """
    found = []
    for pattern, expected in allowed:
        text, count = _blank(text, pattern)
        if count != expected:
            found.append(f"{count} times {pattern.pattern!r}, not {expected}")
    found.extend(
        f"line {text.count(chr(10), 0, match.start()) + 1}"
        for match in _CUSTOM_POLICY_READ.finditer(text)
    )
    return found


def _settings_names(tree: ast.AST) -> set[str]:
    """Names in the module that hold the settings object.

    A parameter typed ``Settings`` and a name set from ``get_settings()``.
    """
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.arg)
            and node.annotation is not None
            and re.search(r"\bSettings\b", ast.unparse(node.annotation))
        ):
            names.add(node.arg)
        elif isinstance(node, ast.Assign | ast.AnnAssign) and _is_get_settings(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _is_get_settings(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "get_settings"


def _is_settings(node: ast.expr, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return "settings" in node.id.lower() or node.id in names
    if isinstance(node, ast.Attribute):
        return "settings" in node.attr.lower()
    return _is_get_settings(node)


def _reads_settings_by_name(source: str) -> list[int]:
    """Lines that call ``getattr`` or ``model_dump`` on the settings object."""
    tree = ast.parse(source)
    names = _settings_names(tree)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and node.args:
            receiver: ast.expr = node.args[0]
        elif isinstance(func, ast.Attribute) and func.attr.startswith("model_dump"):
            receiver = func.value
        else:
            continue
        if _is_settings(receiver, names):
            lines.append(node.lineno)
    return sorted(lines)


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
    assert _custom_policy_reads(line)


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        # (a) the environment name on the line after the call.
        ('on = parse_bool_env(\n    os.environ.get(\n        "CUSTOM_POLICY",\n    ),\n)', [3]),
        # (b) a test for the name in the environment.
        ('if "CUSTOM_POLICY" in os.environ:', [1]),
        ("if 'CUSTOM_POLICY' in os.environ:", [1]),
        # (c) the name in a constant.
        ('_NAME = "CUSTOM_POLICY"\non = os.environ.get(_NAME)', [1]),
        # (d) the setting by its name, or on another name for the settings.
        ('on = getattr(settings, "custom_policy")', [1]),
        ("on = settings.model_dump()['custom_policy']", [1]),
        ("on = cfg.custom_policy", [1]),
        ("on = (\n    cfg\n    .custom_policy\n)", [3]),
    ],
)
def test_custom_policy_search_finds_each_form(text: str, lines: list[int]) -> None:
    assert not any(_DIRECT_READ.search(line) for line in text.splitlines())
    assert _custom_policy_reads(text) == [f"line {n}" for n in lines]


@pytest.mark.parametrize(
    "line",
    [
        '"custom_policy": custom_policy_on(settings),',
        "The CUSTOM_POLICY setting turns on all three policy parts.",
    ],
)
def test_direct_read_pattern_skips_calls_and_texts(line: str) -> None:
    assert _DIRECT_READ.search(line) is None


@pytest.mark.parametrize(
    "text",
    [
        "The CUSTOM_POLICY setting turns on all three policy parts.",
        '"""The ``CUSTOM_POLICY`` switch."""',
        "on = custom_policy_on(settings)",
        "on = custom_policy_on_in_environment()",
        "from meho_backplane.policy.switches import custom_policy_on",
        'name = "CUSTOM_POLICY_LIMIT"',
    ],
)
def test_custom_policy_search_skips_calls_and_texts(text: str) -> None:
    assert _custom_policy_reads(text) == []


def test_custom_policy_search_allows_only_the_given_texts() -> None:
    settings_allowed = _CUSTOM_POLICY_ALLOWED[_SRC / "settings.py"]
    definition = 'custom_policy=parse_bool_env(\n    os.environ.get("CUSTOM_POLICY"),\n),'
    second_read = 'on = os.environ.get(\n    "CUSTOM_POLICY",\n)'
    assert _custom_policy_reads(definition, settings_allowed) == []
    assert _custom_policy_reads(f"{definition}\n{second_read}", settings_allowed) == ["line 5"]
    assert _custom_policy_reads(f"{definition}\nx = self.custom_policy", settings_allowed) == [
        "line 4"
    ]
    assert _custom_policy_reads(second_read, settings_allowed) == [
        f"0 times {_SETTINGS_DEFINITION.pattern!r}, not 1",
        "line 2",
    ]

    ready_allowed = _CUSTOM_POLICY_ALLOWED[_SRC / "features.py"]
    ready = '"custom_policy": custom_policy_on(settings),\n{"custom_policy": <bool>}'
    assert _custom_policy_reads(ready, ready_allowed) == []
    assert _custom_policy_reads(f'{ready}\nd["custom_policy"]', ready_allowed) == ["line 3"]


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("x = getattr(settings, name)", [1]),
        ("x = getattr(get_settings(), name, False)", [1]),
        ("x = getattr(self.settings, name)", [1]),
        ("x = settings.model_dump(include={name})", [1]),
        ("x = get_settings().model_dump_json()", [1]),
        ("def f(cfg: Settings) -> None:\n    return getattr(cfg, name)", [2]),
        ("def f(cfg: Settings | None) -> None:\n    return cfg.model_dump()", [2]),
        ("cfg = get_settings()\nx = getattr(cfg, name)", [2]),
    ],
)
def test_settings_by_name_search_finds_each_form(source: str, lines: list[int]) -> None:
    assert _reads_settings_by_name(source) == lines


@pytest.mark.parametrize(
    "source",
    [
        "x = getattr(entry, key)",
        "def f(entry: SettingsTarget) -> None:\n    return getattr(entry, key)",
        "x = body.model_dump(mode='json')",
        "x = settings.custom_policy_on",
    ],
)
def test_settings_by_name_search_skips_other_objects(source: str) -> None:
    assert _reads_settings_by_name(source) == []


def test_no_code_reads_the_switches_directly() -> None:
    found = [
        f"{path.relative_to(_SRC)}:{number}"
        for path in sorted(_SRC.rglob("*.py"))
        if path not in _ALLOWED
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _DIRECT_READ.search(line)
    ]
    found += [
        f"{path.relative_to(_SRC)}: {read}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _CUSTOM_POLICY_HOME
        for read in _custom_policy_reads(
            path.read_text(encoding="utf-8"), _CUSTOM_POLICY_ALLOWED.get(path, ())
        )
    ]
    assert found == [], "ask meho_backplane.policy.switches instead"


def test_files_that_read_settings_by_a_built_name_are_known() -> None:
    found = {
        path.relative_to(_SRC).as_posix()
        for path in sorted(_SRC.rglob("*.py"))
        if _reads_settings_by_name(path.read_text(encoding="utf-8"))
    }
    assert found == _READS_SETTINGS_BY_NAME, (
        "a new file reads a settings field by a built name: check that it does not "
        "read CUSTOM_POLICY, then add it to _READS_SETTINGS_BY_NAME"
    )
