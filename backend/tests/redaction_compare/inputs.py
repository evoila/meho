# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fixed set of made-up inputs for the secret-hiding compare.

Each input is built from a form (``password=...``, a URL with a login, a
JSON text, a command line, ...), a value style (plain, quoted, with a space,
empty) and a container (bare, in a dict, in a list, in a nested result).
Every planted part has an id and a flag. The id starts with ``s`` for a
secret value and with ``p`` for a plain word that must stay visible. The
ids are built from the form, the style, the label and the container, so
adding a form does not rename the others.

Secret values are made at run time from a seeded random generator, so no
secret-shaped literal stands in this file. The same code gives the same
inputs on every run.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

SEED = 6765
SECRET_LABELS = ("password", "token", "api_key", "client_secret", "secret_key", "passwd")
PLAIN_LABELS = ("region", "note", "colour", "hostname", "shape", "owner_name")
STYLES = ("plain", "single", "double", "space", "empty")

_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PATH_ALL = (
    "redact",
    "scrub_content",
    "scrub_broadcast_params",
    "redact_error_payload",
    "default_view",
)


@dataclass(frozen=True)
class Part:
    """One planted part: *pid* starts with ``s`` (secret) or ``p`` (plain)."""

    pid: str
    value: str


@dataclass(frozen=True)
class CompareInput:
    """One made-up input, the parts planted in it and the paths it runs through."""

    id: str
    payload: Any
    parts: tuple[Part, ...]
    paths: tuple[str, ...]


def _word(rng: random.Random, size: int = 12) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(size))


class _Builder:
    """Hands out the parts of one input and renders a value in a style."""

    def __init__(self, input_id: str, secret: bool) -> None:
        self.rng = random.Random(f"{SEED}:{input_id}")
        self.secret = secret
        self.parts: list[Part] = []

    def plain(self) -> str:
        value = f"plain{len(self.parts)}{_word(self.rng, 5).lower()}"
        self.parts.append(Part(f"p{len(self.parts)}", value))
        return value

    def label(self, text: str) -> str:
        """Record *text* as a plain part, spelled exactly as it goes into the input."""
        self.parts.append(Part(f"p{len(self.parts)}", text))
        return text

    def value(self, style: str) -> str:
        """The value text in *style*; secret parts when the input is a secret one."""
        if style == "empty":
            return ""
        words = [_word(self.rng) for _ in range(2 if style == "space" else 1)]
        for word in words:
            tag = "s" if self.secret else "p"
            self.parts.append(Part(f"{tag}{len(self.parts)}", word))
        return " ".join(words)


def _quote(text: str, style: str) -> str:
    if style == "single":
        return f"'{text}'"
    if style == "double" or (style == "space" and text):
        return f'"{text}"'
    return text


TextForm = Callable[[_Builder, str, str], str]


def _assign(b: _Builder, label: str, style: str) -> str:
    return f"host={b.plain()} {b.label(label)}={_quote(b.value(style), style)} port=443"


def _flag(b: _Builder, label: str, style: str) -> str:
    return f"tool --{b.label(label)} {_quote(b.value(style), style)} --name {b.plain()}"


def _flag_eq(b: _Builder, label: str, style: str) -> str:
    return f"tool --{b.label(label)}={_quote(b.value(style), style)} --name {b.plain()}"


def _yaml_line(b: _Builder, label: str, style: str) -> str:
    return f"name: {b.plain()}\n{b.label(label)}: {_quote(b.value(style), style)}\nport: 443\n"


def _env(b: _Builder, label: str, style: str) -> str:
    return f"export {b.label(label.upper())}={_quote(b.value(style), style)}\necho {b.plain()}\n"


def _url(b: _Builder, label: str, style: str) -> str:
    login = (
        f"{b.plain()}:{b.value(style)}@" if b.secret else ""
    )  # a login in a URL is always a credential
    return f"https://{login}{b.plain()}.example/path?{b.label(label)}=1"


def _connection(b: _Builder, label: str, style: str) -> str:
    host, user = b.plain(), b.plain()
    return f"Server={host};User Id={user};{b.label(label.title())}={b.value(style)};Port=1433;"


def _header(b: _Builder, label: str, style: str) -> str:
    return f"X-{b.label(label)}: {b.value(style)}\nAccept: {b.plain()}"


def _json_text(b: _Builder, label: str, style: str) -> str:
    return json.dumps({"name": b.plain(), b.label(label): b.value(style), "port": 443})


def _xml(b: _Builder, label: str, style: str) -> str:
    return f"<conf><name>{b.plain()}</name><{b.label(label)}>{b.value(style)}</{label}></conf>"


# form name -> (builder, styles it supports)
TEXT_FORMS: dict[str, tuple[TextForm, tuple[str, ...]]] = {
    "assign": (_assign, STYLES),
    "flag": (_flag, STYLES),
    "flag_eq": (_flag_eq, STYLES),
    "yaml_line": (_yaml_line, STYLES),
    "env": (_env, STYLES),
    "url": (_url, ("plain", "empty")),
    "connection": (_connection, ("plain", "space", "empty")),
    "header": (_header, ("plain", "space")),
    "json_text": (_json_text, ("plain", "space", "empty")),
    "xml": (_xml, ("plain", "space", "empty")),
}

CONTAINERS: dict[str, Callable[[str], Any]] = {
    "bare": lambda text: text,
    "dict": lambda text: {"text": text},
    "list": lambda text: ["first", text],
    "nested": lambda text: {"stdout": text, "params": {"command": "echo ok"}},
    "json_field": lambda text: json.dumps({"log": text}),
}


def _text_inputs(labels: tuple[str, ...], secret: bool) -> Iterator[CompareInput]:
    for form, (build, styles) in TEXT_FORMS.items():
        for style in styles:
            for label in labels:
                for container, wrap in CONTAINERS.items():
                    input_id = f"{form}.{style}.{label}.{container}"
                    b = _Builder(input_id, secret)
                    payload = wrap(build(b, label, style))
                    yield CompareInput(input_id, payload, tuple(b.parts), PATH_ALL)


def _shape_dict(b: _Builder, label: str, style: str) -> Any:
    return {"name": b.plain(), b.label(label): b.value(style)}


def _shape_rows(b: _Builder, label: str, style: str) -> Any:
    return [{"name": b.plain(), b.label(label): b.value(style)}, {"name": b.plain(), "size": 3}]


def _shape_deep(b: _Builder, label: str, style: str) -> Any:
    auth = {b.label(label): b.value(style)}
    return {"spec": {"items": [{"id": b.plain(), "auth": auth}]}}


def _shape_params(b: _Builder, label: str, style: str) -> Any:
    return {"params": {"command": b.plain(), b.label(label): b.value(style)}}


_SHAPES: dict[str, Callable[[_Builder, str, str], Any]] = {
    "dict": _shape_dict,
    "rows": _shape_rows,
    "deep": _shape_deep,
    "params": _shape_params,
}


def _structured_inputs(labels: tuple[str, ...], secret: bool) -> Iterator[CompareInput]:
    for style in ("plain", "space", "empty"):
        for label in labels:
            for shape, build in _SHAPES.items():
                input_id = f"struct.{shape}.{style}.{label}"
                b = _Builder(input_id, secret)
                payload = build(b, label, style)
                yield CompareInput(input_id, payload, tuple(b.parts), PATH_ALL)


#: Command forms whose secret does not depend on a label: ``-p``, ``-u user:value``, ``sshpass -p``.
_UNLABELLED = ("p_flag", "user_pw", "script_sshpass")


def _command_inputs(labels: tuple[str, ...], secret: bool) -> Iterator[CompareInput]:
    shell_forms: dict[str, Callable[[_Builder, str, str], str]] = {
        "p_flag": lambda b, lab, st: f"mysql -u {b.plain()} -p{b.value(st)} {b.plain()}",
        "long_flag": lambda b, lab, st: (
            f"tool --{b.label(lab)} {_quote(b.value(st), st)} --name {b.plain()}"
        ),
        "long_eq": lambda b, lab, st: (
            f"tool --{b.label(lab)}={_quote(b.value(st), st)} --name {b.plain()}"
        ),
        "env_prefix": lambda b, lab, st: (
            f"{b.label(lab.upper())}={_quote(b.value(st), st)} run {b.plain()}"
        ),
        "sh_c": lambda b, lab, st: (
            f"sh -c '{b.label(lab.upper())}={_quote(b.value(st), st)}; echo {b.plain()}'"
        ),
        "user_pw": lambda b, lab, st: (
            f"curl -u {b.plain()}:{b.value(st)} https://{b.plain()}.example/"
        ),
    }
    script_forms: dict[str, Callable[[_Builder, str, str], str]] = {
        "script_export": lambda b, lab, st: (
            f"set -e\nexport {b.label(lab.upper())}={_quote(b.value(st), st)}\necho {b.plain()}\n"
        ),
        "script_sshpass": lambda b, lab, st: (
            f"sshpass -p {_quote(b.value(st), st)} ssh {b.plain()}\nmkdir -p /tmp/{b.plain()}\n"
        ),
        "script_loop": lambda b, lab, st: (
            f"for h in {b.plain()}; do\n  tool --{b.label(lab)} {_quote(b.value(st), st)} $h\n"
            "done\n"
        ),
    }
    for forms, path in ((shell_forms, "hide_command_text"), (script_forms, "hide_shell_script")):
        for form, build in forms.items():
            if form in _UNLABELLED and not secret:
                continue  # these forms always carry a secret, so they have no plain twin
            for style in STYLES:
                for label in labels if form not in _UNLABELLED else ("any",):
                    input_id = f"cmd.{form}.{style}.{label}"
                    b = _Builder(input_id, secret)
                    text = build(b, label, style)
                    yield CompareInput(input_id + ".bare", text, tuple(b.parts), (*PATH_ALL, path))
                    nested = {"params": {"command": text}, "stdout": "ok"}
                    yield CompareInput(input_id + ".nested", nested, tuple(b.parts), PATH_ALL)


def secret_inputs() -> list[CompareInput]:
    """Inputs with planted secret values and plain words."""
    labels = SECRET_LABELS
    inputs = [*_text_inputs(labels, True), *_structured_inputs(labels, True)]
    inputs.extend(_command_inputs(labels, True))
    return sorted(inputs, key=lambda item: item.id)


def plain_inputs() -> list[CompareInput]:
    """The same forms with plain labels and plain values: nothing to hide."""
    labels = PLAIN_LABELS
    inputs = [*_text_inputs(labels, False), *_structured_inputs(labels, False)]
    inputs.extend(_command_inputs(labels, False))
    return sorted(inputs, key=lambda item: item.id)
