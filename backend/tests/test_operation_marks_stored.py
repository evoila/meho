# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every "hard to undo" and "changes rights" mark in code reaches the stored row.

A built-in operation says in code whether a call is hard to undo
(``hard_to_undo``) and whether it changes rights (``changes_rights``). Each
connector passes the two values on in its own ``register_typed_operation``
call, and a start writes them into ``endpoint_descriptor``. A register call
that leaves the two lines out writes no mark, and the approval table
(``tests/snapshots/approval_table.json``) does not hold the two fields. This
test fails then.

The test reads the marks from the definitions in code, not from a copied
list, so a new connector is covered with no change here. The test reads
every module of every built-in connector package (the packages in
``meho_backplane.connectors``). A definition belongs to the package of the
registrar that registers it, or else to its built-in connector package.
The test finds definitions in three forms:

* an op record: a dataclass or named tuple with ``op_id`` and a handler
  field, or a dict with ``op_id`` and ``handler`` (a spec dict), held in a
  module variable, also inside nested tuples, lists and dicts. A record
  without a mark field has no mark;
* a call with an ``op_id=`` keyword, or with the op id in the place of the
  ``op_id`` parameter of the function it names: a register call with a
  plain op id (its marks are the plain values it writes, or none), or any
  other call that writes a mark as a plain value (a call that makes a
  record). A plain value can also come from a local variable of the
  function that is set once to a plain value, or from a dict that the call
  unpacks with ``**`` (a local dict, or a dict of the module);
* a ``for`` loop that unpacks spec tuples of a module variable into names
  such as ``op_id`` and ``hard_to_undo``.

The test fails when:

* a stored row has no definition that the test read, also a row with no mark;
* two definitions of one operation give it different marks, for example a
  call that makes a record and the record it makes;
* a stored row holds other marks than its definition;
* a definition has no stored row, also when its package queues no
  registrar;
* a register function keeps a mark in a local variable or a local dict
  that no register call of the function passes on;
* a spec dict has a key that its register call does not read;
* a ``safe`` row has a mark.

A definition that names its connector (``product``, ``version`` and
``impl_id``) holds only for the row of that connector, so two versions of
one connector can give one op id different marks. A record that does not
name its connector holds for the connector of the register call that uses
it: during the start the test looks for the record in the variables of
the code that runs the register call.
"""

from __future__ import annotations

import ast
import contextvars
import functools
import importlib
import inspect
import pkgutil
import sys
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import update

import meho_backplane.connectors
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import typed_register
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    clear_typed_op_snapshot,
)
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

#: ``(hard_to_undo, changes_rights)``. No mark is ``(None, False)``.
Mark = tuple[str | None, bool]
#: ``(product, version, impl_id)``.
Connector = tuple[str, str, str]

_NO_MARK: Mark = (None, False)
_MARK_FIELDS = ("hard_to_undo", "changes_rights")
_CONNECTOR_FIELDS = ("product", "version", "impl_id")
_HANDLER_FIELDS = ("handler", "handler_attr", "handler_name")
_REGISTER_CALLS = frozenset({"register_typed_operation", "register_composite_operation"})
_NESTING_LIMIT = 6

#: The package of the registrar that runs now.
_RUNNING_OWNER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "marks_test_running_owner", default=None
)


@dataclass(frozen=True)
class _Key:
    """Where a definition holds: the registrar package, the connector (or any) and the op id."""

    owner: str
    connector: Connector | None
    op_id: str


@dataclass
class _Definitions:
    """The marks that code gives each key, with where each one was read."""

    marks: dict[_Key, dict[Mark, set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    #: ``owner -> the spec dicts of that owner, with where each was read``.
    spec_dicts: dict[str, list[tuple[dict[str, Any], str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    #: ``owner -> the dict keys that the register calls of that owner read``.
    keys_read: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: Local marks that no register call passes on.
    problems: list[str] = field(default_factory=list)

    def add(self, key: _Key, mark: Mark, where: str) -> None:
        self.marks[key][mark].add(where)


def _owner_of_registrar(registrar: Callable[..., Any]) -> str:
    module = sys.modules[registrar.__module__]
    if hasattr(module, "__path__"):
        return module.__name__
    return module.__name__.rpartition(".")[0]


def _owners() -> list[str]:
    return sorted({_owner_of_registrar(registrar) for registrar in _TYPED_OP_REGISTRARS})


def _built_in_packages() -> list[str]:
    """Every built-in connector package, also one that queues no registrar."""
    root = meho_backplane.connectors
    return sorted(
        f"{root.__name__}.{info.name}" for info in pkgutil.iter_modules(root.__path__) if info.ispkg
    )


def _owner_of_module(name: str, owners: list[str]) -> str | None:
    """The longest package in *owners* that holds module *name*."""
    holding = [owner for owner in owners if name == owner or name.startswith(f"{owner}.")]
    return max(holding, key=len) if holding else None


def _owner_modules(owners: list[str]) -> list[tuple[str, ModuleType]]:
    """Every module of every registrar package, with the package it belongs to."""
    modules: dict[str, ModuleType] = {}
    for name in owners:
        package = importlib.import_module(name)
        modules[name] = package
        for info in pkgutil.walk_packages(package.__path__, f"{name}."):
            modules[info.name] = importlib.import_module(info.name)
    found: list[tuple[str, ModuleType]] = []
    for name, module in sorted(modules.items()):
        owner = _owner_of_module(name, owners)
        assert owner is not None
        found.append((owner, module))
    return found


@dataclass
class _Tracked:
    """What the start stored: each row's registrar package, and the connectors of each record."""

    owners: dict[tuple[Connector, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    #: ``id(record) -> (record, the connectors of the register calls that used it)``.
    records: dict[int, tuple[Any, set[Connector]]] = field(default_factory=dict)

    def clear(self) -> None:
        self.owners.clear()
        self.records.clear()

    def note_record(self, connector: Connector, op_id: str) -> None:
        """Note the record that the running register call uses, if the test sees one.

        The nearest frame that holds a record with this op id is the code
        that runs the register call, for example a loop over the records.
        """
        frame = inspect.currentframe()
        while frame is not None:
            for value in list(frame.f_locals.values()):
                found = _record(value)
                if found is not None and found[0]["op_id"] == op_id:
                    self.records.setdefault(id(value), (value, set()))[1].add(connector)
                    return
            frame = frame.f_back

    def connectors_of(self, record: Any) -> set[Connector]:
        return self.records.get(id(record), (None, set()))[1]


@pytest.fixture(autouse=True)
def _a_real_start(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tracked]:
    """Run every registrar for real, with every operation on, as a start does.

    The suite replays the rows of its first registration pass; that is off
    here, so each start runs the register calls. The policy operations
    register only while ``POLICY_CLASSIFICATION`` is on, so it is on. The
    test notes which registrar writes each row, so a row is matched to the
    definitions of its own registrar package.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.delenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", raising=False)
    get_settings.cache_clear()
    clear_typed_op_snapshot()

    tracked = _Tracked()
    write = typed_register._register_in_session

    @functools.wraps(write)
    async def noted_write(*args: Any, **kwargs: Any) -> None:
        owner = _RUNNING_OWNER.get()
        if owner is not None:
            connector = (kwargs["product"], kwargs["version"], kwargs["impl_id"])
            tracked.owners[(connector, kwargs["op_id"])].add(owner)
            tracked.note_record(connector, kwargs["op_id"])
        await write(*args, **kwargs)

    def noted(registrar: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        owner = _owner_of_registrar(registrar)

        @functools.wraps(registrar)
        async def run(**kwargs: Any) -> None:
            token = _RUNNING_OWNER.set(owner)
            try:
                await registrar(**kwargs)
            finally:
                _RUNNING_OWNER.reset(token)

        return run

    monkeypatch.setattr(typed_register, "_register_in_session", noted_write)
    monkeypatch.setattr(
        typed_register,
        "_TYPED_OP_REGISTRARS",
        [noted(registrar) for registrar in _TYPED_OP_REGISTRARS],
    )
    yield tracked
    clear_typed_op_snapshot()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Reading the definitions
# ---------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _connector_of(values: dict[str, Any]) -> Connector | None:
    parts = [_text(values.get(name)) for name in _CONNECTOR_FIELDS]
    if all(parts):
        return (str(parts[0]), str(parts[1]), str(parts[2]))
    return None


def _record(value: Any) -> tuple[dict[str, Any], bool] | None:
    """The fields of an op record, and whether it is a spec dict; ``None`` for no record."""
    if isinstance(value, dict):
        if isinstance(value.get("op_id"), str) and "handler" in value:
            return value, True
        return None
    if isinstance(value, type):
        return None
    if is_dataclass(value):
        names = [item.name for item in fields(value)]
    elif isinstance(value, tuple) and hasattr(value, "_fields"):
        names = list(value._fields)
    else:
        return None
    if "op_id" not in names or not any(name in names for name in _HANDLER_FIELDS):
        return None
    values = {name: getattr(value, name) for name in names}
    if not isinstance(values["op_id"], str):
        return None
    return values, False


def _mark_of(values: dict[str, Any]) -> Mark:
    return _text(values.get("hard_to_undo")), bool(values.get("changes_rights"))


def _records(value: Any, depth: int = 0) -> Iterator[tuple[dict[str, Any], bool, Any]]:
    """Every op record in *value* (its fields, whether it is a spec dict, the record itself)."""
    found = _record(value)
    if found is not None:
        yield (*found, value)
        return
    if depth >= _NESTING_LIMIT:
        return
    if isinstance(value, dict):
        items: Any = value.values()
    elif isinstance(value, (tuple, list, set, frozenset)):
        items = value
    else:
        return
    for item in items:
        yield from _records(item, depth + 1)


def _assigned_names(tree: ast.Module) -> list[str]:
    """The names that the module itself sets at its top level (not the names it imports)."""
    names: list[str] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        names.extend(target.id for target in targets if isinstance(target, ast.Name))
    return names


@dataclass
class _Scope:
    """The plain values that code in a module or in one function can read by name."""

    module: ModuleType
    #: Local names that are set once, to a plain value.
    constants: dict[str, Any] = field(default_factory=dict)
    #: Names that are set to a dict literal with text keys.
    dicts: dict[str, dict[str, ast.expr]] = field(default_factory=dict)
    #: Local names that are set in another way; they hide a module name.
    shadowed: set[str] = field(default_factory=set)
    #: ``(local name, mark field, value)``: the marks that the function itself keeps.
    marks: list[tuple[str, str, Any]] = field(default_factory=list)


def _text_dict(node: ast.expr) -> dict[str, ast.expr] | None:
    if not isinstance(node, ast.Dict):
        return None
    keys = [
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    ]
    if len(keys) != len(node.keys):
        return None
    return dict(zip(keys, node.values, strict=True))


def _module_scope(tree: ast.Module, module: ModuleType) -> _Scope:
    scope = _Scope(module)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            literal = _text_dict(node.value)
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and literal is not None:
                scope.dicts[targets[0].id] = literal
    return scope


def _function_scope(function: ast.FunctionDef | ast.AsyncFunctionDef, outer: _Scope) -> _Scope:
    """The local names of *function* that are set once to a plain value or a dict literal."""
    simple: dict[int, ast.expr] = {}
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                simple[id(targets[0])] = node.value
    bound: dict[str, list[ast.expr | None]] = defaultdict(list)
    arguments = function.args
    for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
        bound[arg.arg].append(None)
    for arg in (arguments.vararg, arguments.kwarg):
        if arg is not None:
            bound[arg.arg].append(None)
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound[node.id].append(simple.get(id(node)))
    scope = _Scope(outer.module, dicts=dict(outer.dicts))
    for name, values in bound.items():
        scope.dicts.pop(name, None)
        value = values[0] if len(values) == 1 else None
        literal = _text_dict(value) if value is not None else None
        if isinstance(value, ast.Constant):
            scope.constants[name] = value.value
            if name in _MARK_FIELDS:
                scope.marks.append((name, name, value.value))
        elif literal is not None:
            scope.dicts[name] = literal
            scope.marks.extend(
                (name, key, item.value)
                for key, item in literal.items()
                if key in _MARK_FIELDS and isinstance(item, ast.Constant)
            )
        else:
            scope.shadowed.add(name)
    return scope


def _resolve(node: ast.expr, scope: _Scope) -> Any:
    """The value of a plain value, a local plain value or a module-level name, or ``None``."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope.constants:
            return scope.constants[node.id]
        if node.id not in scope.shadowed:
            return getattr(scope.module, node.id, None)
    return None


def _plain(node: ast.expr, scope: _Scope) -> tuple[bool, Any]:
    """``(True, value)`` for a plain value or a local name set to one, else ``(False, None)``."""
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, ast.Name) and node.id in scope.constants:
        return True, scope.constants[node.id]
    return False, None


def _unpacked(node: ast.expr, scope: _Scope) -> dict[str, ast.expr] | None:
    """The keywords that ``**node`` passes, or ``None`` when the test cannot read them."""
    if not isinstance(node, ast.Name) or node.id in scope.constants:
        return None
    if node.id in scope.dicts:
        return scope.dicts[node.id]
    value = None if node.id in scope.shadowed else getattr(scope.module, node.id, None)
    if not isinstance(value, dict):
        return None
    return {key: ast.Constant(item) for key, item in value.items() if isinstance(key, str)}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _positional_op_id(node: ast.Call, module: ModuleType) -> ast.expr | None:
    """The argument that a call passes as ``op_id`` by position, or ``None``.

    The test reads the signature of the function that the call names in its
    module, so a helper such as ``_make_op("x.delete", ..., hard_to_undo=...)``
    counts as a call with ``op_id=``.
    """
    if not node.args or not isinstance(node.func, ast.Name):
        return None
    function = getattr(module, node.func.id, None)
    if not callable(function):
        return None
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return None
    positional = [
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if "op_id" not in positional:
        return None
    index = positional.index("op_id")
    if index >= len(node.args) or isinstance(node.args[index], ast.Starred):
        return None
    if any(isinstance(arg, ast.Starred) for arg in node.args[:index]):
        return None
    return node.args[index]


def _call_keywords(node: ast.Call, scope: _Scope) -> dict[str, ast.expr] | None:
    """The keywords of a call, also those of the dicts it unpacks with ``**``.

    ``None`` for a register call that unpacks a dict the test cannot read:
    its marks may come in through that dict.
    """
    keywords: dict[str, ast.expr] = {}
    for keyword in node.keywords:
        if keyword.arg is not None:
            keywords[keyword.arg] = keyword.value
            continue
        unpacked = _unpacked(keyword.value, scope)
        if unpacked is None and _call_name(node) in _REGISTER_CALLS:
            return None
        keywords.update(unpacked or {})
    return keywords


def _call_definition(node: ast.Call, scope: _Scope) -> tuple[dict[str, Any], Mark] | None:
    """The definition in a call with ``op_id``, or ``None`` when the call defines nothing.

    The op id is the ``op_id=`` keyword, or the argument in the place of the
    ``op_id`` parameter of the function the call names.

    A register call with a plain op id defines the op: its marks are the
    plain values it writes, or none. A register call that takes a mark from
    a variable only passes a definition on. Any other call with ``op_id=``
    (a record, a log line, an error) defines a mark only when it writes the
    mark as a plain value. A keyword that comes in through ``**`` counts the
    same as a keyword written in the call.
    """
    keywords = _call_keywords(node, scope)
    if keywords is None:
        return None
    op_id_node = keywords.get("op_id") or _positional_op_id(node, scope.module)
    op_id = _resolve(op_id_node, scope) if op_id_node is not None else None
    if not isinstance(op_id, str):
        return None
    marks = {name: _plain(keywords[name], scope) for name in _MARK_FIELDS if name in keywords}
    if not all(readable for readable, _ in marks.values()):
        return None
    if not marks and _call_name(node) not in _REGISTER_CALLS:
        return None
    values = {name: value for name, (_, value) in marks.items()}
    values.update(
        {name: _resolve(keywords[name], scope) for name in _CONNECTOR_FIELDS if name in keywords}
    )
    values["op_id"] = op_id
    return values, _mark_of(values)


def _passes(node: ast.Call, name: str, mark: str) -> bool:
    """Whether a call passes the local *name* on as *mark* or in a ``**`` dict."""
    return any(
        isinstance(keyword.value, ast.Name)
        and keyword.value.id == name
        and keyword.arg in (None, mark)
        for keyword in node.keywords
    )


def _local_mark_lines(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: _Scope,
    calls: list[tuple[ast.Call, dict[str, Any]]],
) -> Iterator[str]:
    """A line for each local mark of *function* that none of its register *calls* passes on."""
    for name, mark, value in scope.marks:
        if any(_passes(call, name, mark) for call, _ in calls):
            continue
        for call, values in calls:
            yield (
                f"{values['op_id']}: its register function {function.name} "
                f"({scope.module.__name__}:{function.lineno}) keeps {mark}={value!r} in "
                f"the local {name!r}, but its register call (line {call.lineno}) passes "
                f"{mark}={dict(zip(_MARK_FIELDS, _mark_of(values), strict=True))[mark]!r}"
            )


def _loop_definitions(node: ast.For, module: ModuleType) -> Iterator[dict[str, Any]]:
    """The spec tuples a ``for`` loop unpacks into ``op_id`` and the mark names."""
    if not isinstance(node.target, ast.Tuple) or not isinstance(node.iter, ast.Name):
        return
    names = [item.id if isinstance(item, ast.Name) else None for item in node.target.elts]
    if "op_id" not in names or not any(name in names for name in _MARK_FIELDS):
        return
    specs = getattr(module, node.iter.id, None)
    if not isinstance(specs, (tuple, list)):
        return
    for spec in specs:
        values = {name: value for name, value in zip(names, spec, strict=True) if name}
        yield values


def _keys_read(function: ast.AST) -> set[str]:
    """The dict keys that *function* reads.

    These are the plain keys of ``x["key"]`` and ``x.get("key")``, and, for a
    register call that unpacks a dict with ``**``, the names of the
    parameters of that register function.
    """
    keys: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) in _REGISTER_CALLS
            and any(keyword.arg is None for keyword in node.keywords)
        ):
            register = getattr(typed_register, str(_call_name(node)))
            keys |= set(inspect.signature(register).parameters)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                keys.add(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def _register_functions(tree: ast.Module) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(inner, ast.Call) and _call_name(inner) in _REGISTER_CALLS
            for inner in ast.walk(node)
        ):
            yield node


def _function_scopes(
    tree: ast.Module, outer: _Scope
) -> tuple[dict[int, _Scope], list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, _Scope]]]:
    """The scope of each call (by node id), and each function with its scope."""
    scopes: dict[int, _Scope] = {}
    functions: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, _Scope]] = []
    # ast.walk visits an outer function before the functions inside it, so
    # the innermost function sets the scope of its calls last.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = _function_scope(node, outer)
            functions.append((node, scope))
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    scopes[id(inner)] = scope
    return scopes, functions


def _read_records(
    owner: str, module: ModuleType, names: Iterable[str], tracked: _Tracked, found: _Definitions
) -> dict[tuple[str, str], set[Connector]]:
    """Read the records in module variables.

    Returns the connectors that the start used each record for, by
    ``(variable name, op id)``.
    """
    used: dict[tuple[str, str], set[Connector]] = defaultdict(set)
    for name in names:
        where = f"{module.__name__}.{name}"
        for values, is_spec_dict, record in _records(getattr(module, name, None)):
            used[(name, values["op_id"])] |= tracked.connectors_of(record)
            own = _connector_of(values)
            connectors = [own] if own else sorted(tracked.connectors_of(record)) or [None]
            for connector in connectors:
                found.add(_Key(owner, connector, values["op_id"]), _mark_of(values), where)
            if is_spec_dict:
                found.spec_dicts[owner].append((values, where))
    return used


def _variable_of_calls(tree: ast.Module) -> dict[int, str]:
    """The module variable that each call at the top level of the module helps to set."""
    variables: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                for inner in ast.walk(node.value):
                    variables[id(inner)] = targets[0].id
    return variables


def _read_module(owner: str, module: ModuleType, tracked: _Tracked, found: _Definitions) -> None:
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError):
        return
    used = _read_records(owner, module, _assigned_names(tree), tracked, found)
    variables = _variable_of_calls(tree)
    module_scope = _module_scope(tree, module)
    scopes, functions = _function_scopes(tree, module_scope)
    register_calls: dict[int, list[tuple[ast.Call, dict[str, Any]]]] = defaultdict(list)
    for node in ast.walk(tree):
        where = f"{module.__name__}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Call):
            scope = scopes.get(id(node), module_scope)
            call = _call_definition(node, scope)
            if call is None:
                continue
            values, mark = call
            # A call that makes a record in a module variable holds for the
            # connectors of that record.
            made = used.get((variables.get(id(node), ""), values["op_id"]), set())
            own = _connector_of(values)
            for connector in [own] if own else sorted(made) or [None]:
                found.add(_Key(owner, connector, values["op_id"]), mark, where)
            if _call_name(node) in _REGISTER_CALLS:
                register_calls[id(scope)].append((node, values))
        elif isinstance(node, ast.For):
            for values in _loop_definitions(node, module):
                key = _Key(owner, _connector_of(values), values["op_id"])
                found.add(key, _mark_of(values), where)
    for function, scope in functions:
        found.problems.extend(_local_mark_lines(function, scope, register_calls[id(scope)]))
    for function in _register_functions(tree):
        found.keys_read[owner] |= _keys_read(function)


def _definitions(tracked: _Tracked) -> _Definitions:
    found = _Definitions()
    for owner, module in _owner_modules(sorted({*_owners(), *_built_in_packages()})):
        _read_module(owner, module, tracked, found)
    return found


# ---------------------------------------------------------------------------
# Comparing them with the stored rows
# ---------------------------------------------------------------------------


def _connector_of_row(row: EndpointDescriptor) -> Connector:
    return (row.product, row.version, row.impl_id)


def _name(row: EndpointDescriptor) -> str:
    return f"{row.op_id} ({'/'.join(_connector_of_row(row))})"


def _marks_for(row: EndpointDescriptor, owner: str, found: _Definitions) -> dict[Mark, set[str]]:
    marks: dict[Mark, set[str]] = defaultdict(set)
    for key in (_Key(owner, None, row.op_id), _Key(owner, _connector_of_row(row), row.op_id)):
        for mark, places in found.marks.get(key, {}).items():
            marks[mark] |= places
    return marks


def _row_lines(
    rows: list[EndpointDescriptor], tracked: _Tracked, found: _Definitions
) -> tuple[list[str], int]:
    lines: list[str] = []
    marked = 0
    for row in sorted(rows, key=lambda item: (item.op_id, _connector_of_row(item))):
        stored = (row.hard_to_undo, row.changes_rights)
        if row.safety_level == "safe" and (row.hard_to_undo or row.changes_rights):
            lines.append(
                f"{_name(row)}: its risk level is safe, but it is stored with a mark {stored}"
            )
        owners = tracked.owners.get((_connector_of_row(row), row.op_id), set())
        if len(owners) != 1:
            lines.append(f"{_name(row)}: written by the registrars of {sorted(owners)}, not by one")
            continue
        marks = _marks_for(row, next(iter(owners)), found)
        if not marks:
            lines.append(
                f"{_name(row)}: stored {stored}, but the test reads no definition of it in "
                f"{next(iter(owners))}. Give the op a record with op_id, a handler and its "
                "marks in a module variable, or write op_id= and the marks as plain values "
                "in its register call"
            )
            continue
        if len(marks) > 1:
            detail = "; ".join(
                f"{mark} in {sorted(places)}" for mark, places in sorted(marks.items(), key=repr)
            )
            lines.append(
                f"{_name(row)}: the code gives it different marks: {detail}. If the marks are "
                "for different connectors, name product, version and impl_id in each definition"
            )
            continue
        (expected,) = marks
        marked += expected != _NO_MARK
        if stored != expected:
            lines.append(f"{_name(row)}: code {expected}, stored {stored}")
    return lines, marked


def _definition_lines(
    rows: list[EndpointDescriptor], tracked: _Tracked, found: _Definitions
) -> list[str]:
    stored: set[_Key] = set()
    for row in rows:
        for owner in tracked.owners.get((_connector_of_row(row), row.op_id), set()):
            stored.add(_Key(owner, None, row.op_id))
            stored.add(_Key(owner, _connector_of_row(row), row.op_id))
    queued = set(_owners())
    return [
        f"{key.op_id}: defined in {sorted(set().union(*places.values()))}, but {key.owner} "
        + ("stores no row of it" if key.owner in queued else "queues no registrar, so no row")
        + (f" for {'/'.join(key.connector)}" if key.connector else "")
        for key, places in sorted(found.marks.items(), key=repr)
        if key not in stored
    ]


def _spec_dict_lines(found: _Definitions) -> list[str]:
    lines: list[str] = []
    for owner, specs in sorted(found.spec_dicts.items()):
        read = found.keys_read[owner]
        for spec, where in specs:
            for key in sorted(set(spec) - read):
                lines.append(
                    f"{spec['op_id']}: its spec dict in {where} has the key {key!r}, "
                    f"which no register call of {owner} reads"
                )
    return lines


async def _built_in_rows() -> list[EndpointDescriptor]:
    return [row for row in await register_all_descriptors() if row.tenant_id is None]


def _assert_rows_match_code(rows: list[EndpointDescriptor], tracked: _Tracked) -> None:
    found = _definitions(tracked)
    lines, marked = _row_lines(rows, tracked, found)
    # The test reads its marks from code. If that reading broke, few ops would
    # carry one, and a dropped mark would go unseen.
    assert marked > 100, f"only {marked} built-in ops carry a mark in code"
    lines += _definition_lines(rows, tracked, found)
    lines += _spec_dict_lines(found)
    lines += found.problems
    assert not lines, (
        f"{len(lines)} problem(s) between the marks in code and the stored operation rows. "
        "Check that each register call passes hard_to_undo and changes_rights on:\n"
        + "\n".join(lines)
    )


@pytest.mark.asyncio
async def test_each_start_stores_the_marks_of_the_code(_a_real_start: _Tracked) -> None:
    # A fresh database.
    _assert_rows_match_code(await _built_in_rows(), _a_real_start)
    # An upgraded install: its rows are from before the two columns and hold
    # neither value. One start writes the values of the code.
    async with get_sessionmaker()() as session:
        emptied = await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.tenant_id.is_(None))
            .values(hard_to_undo=None, changes_rights=None)
        )
        await session.commit()
    assert emptied.rowcount > 500
    _a_real_start.clear()
    _assert_rows_match_code(await _built_in_rows(), _a_real_start)
