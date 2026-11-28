# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard: every integration fixture starts its containers through ``start_test_container``.

The CI logs of this repository are public. ``start_test_container``
(``tests/_container_ready.py``) gives a container that did not start a
fixed message: the image without its registry part and the kind of
error. This guard reads every module under ``tests/integration/`` and
fails when one of them

* calls ``<container>.start()``, ``<image>.build()`` or ``<network>.create()``
  itself, outside ``start_test_container`` and ``run_docker_step``, or
* puts a Docker client error into a text (an f-string, ``str``,
  ``repr``, ``format`` or ``%``) in an ``except`` block.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).resolve().parent / "integration"

# Error classes of the Docker client (``docker.errors``) and of testcontainers.
_DOCKER_ERRORS = frozenset(
    {
        "APIError",
        "BuildError",
        "ContainerError",
        "DockerException",
        "ImageNotFound",
        "NotFound",
    }
)


# Calls that ask the Docker daemon for a container, an image or a network.
_DOCKER_STEPS = frozenset({"start", "build", "create"})
_HELPERS = frozenset({"start_test_container", "run_docker_step"})


def direct_starts(source: str) -> list[int]:
    """Line numbers of ``<x>.start()``, ``<x>.build()`` or ``<x>.create()`` outside the helpers."""
    tree = ast.parse(source)
    in_helper: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in _HELPERS:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                in_helper.update(id(n) for n in ast.walk(arg))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DOCKER_STEPS
        and not node.args
        and not node.keywords
        and id(node) not in in_helper
    ]


def docker_errors_in_text(source: str) -> list[int]:
    """Line numbers where an ``except`` block puts a Docker client error into a text."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        starts_container = bool(direct_starts(ast.unparse(ast.Module(node.body, []))))
        for handler in node.handlers:
            if handler.name is None:
                continue
            if not (starts_container or _catches_a_docker_error(handler)):
                continue
            hits.extend(_formatted_uses(handler, handler.name))
    return hits


def _catches_a_docker_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    for node in ast.walk(handler.type):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(name, str) and (name in _DOCKER_ERRORS or "docker" in name.lower()):
            return True
    return False


def _formatted_uses(handler: ast.ExceptHandler, name: str) -> list[int]:
    def is_error(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    hits: list[int] = []
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.FormattedValue) and is_error(node.value):
                hits.append(node.lineno)
            elif isinstance(node, ast.Call) and any(is_error(arg) for arg in node.args):
                func = node.func
                if (isinstance(func, ast.Name) and func.id in {"str", "repr"}) or (
                    isinstance(func, ast.Attribute) and func.attr == "format"
                ):
                    hits.append(node.lineno)
            elif (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mod)
                and any(is_error(n) for n in ast.walk(node.right))
            ):
                hits.append(node.lineno)
    return hits


def _modules() -> list[Path]:
    return sorted(_INTEGRATION.rglob("*.py"))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(_INTEGRATION)))
def test_integration_module_starts_containers_only_through_the_helper(path: Path) -> None:
    source = path.read_text()
    assert direct_starts(source) == [], (
        f"{path.name} starts a container itself; use start_test_container "
        "from tests._container_ready"
    )
    assert docker_errors_in_text(source) == [], (
        f"{path.name} puts a Docker client error into a text; use start_test_container "
        "from tests._container_ready"
    )


def test_the_guard_reads_the_integration_modules() -> None:
    assert len(_modules()) > 20


# The shape a fixture had before it used the helper: the guard must catch it.
_OWN_START = """
def vault():
    container = DockerContainer(image)
    try:
        container.start()
    except Exception as exc:
        reason = f"vault container failed to start ({type(exc).__name__}): {exc}"
        pytest.skip(reason)
"""

_OWN_API_ERROR = """
def pg():
    try:
        pg = start_until_ready(start, probe, what="pg")
    except _DockerAPIError as exc:
        msg = str(exc).lower()
        if "429" in msg:
            pytest.skip("rate limited")
        raise
"""


def test_the_guard_finds_a_fixture_that_starts_its_container_itself() -> None:
    assert direct_starts(_OWN_START) == [5]
    assert docker_errors_in_text(_OWN_START) == [7]


def test_the_guard_finds_a_docker_error_put_into_a_text() -> None:
    assert direct_starts(_OWN_API_ERROR) == []
    assert docker_errors_in_text(_OWN_API_ERROR) == [6]


@pytest.mark.parametrize(
    "text",
    [
        "repr(exc)",
        '"{}".format(exc)',
        '"%s" % exc',
        '"%s %s" % (1, exc)',
    ],
)
def test_the_guard_finds_each_way_to_put_the_error_into_a_text(text: str) -> None:
    source = f"try:\n    x()\nexcept docker.errors.APIError as exc:\n    y = {text}\n"
    assert docker_errors_in_text(source) == [4]


def test_the_guard_finds_an_image_build_and_a_network_outside_the_helper() -> None:
    source = "image.build()\nnetwork = Network().create()\n"
    assert direct_starts(source) == [1, 2]


def test_the_guard_lets_the_helpers_run_docker_steps() -> None:
    source = (
        'run_docker_step(lambda: DockerImage(path=p).build(), what="x", failed="y")\n'
        'start_test_container(make=lambda: Network().create(), image="i", what="x")\n'
    )
    assert direct_starts(source) == []


def test_the_guard_lets_other_errors_and_calls_with_arguments_pass() -> None:
    source = (
        "try:\n"
        "    from testcontainers.k3s import K3SContainer\n"
        "except ImportError as exc:\n"
        '    pytest.skip(f"testcontainers.k3s unavailable: {exc}")\n'
        'page = browser.start(max_age="0")\n'
        "runtime.start(definition)\n"
    )
    assert direct_starts(source) == []
    assert docker_errors_in_text(source) == []
