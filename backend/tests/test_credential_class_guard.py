# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every operation that stores or returns a secret value has a credential class.

The rule that a credential call always waits for a person reads the class
of the operation. An operation that stores or returns a secret value but
has no credential class would run without that rule. This guard goes over
every registered operation (from the registry, not a hand list) and finds
such an operation by what it declares:

* a tag that says it reads or writes a secret or a credential
  (:data:`SECRET_TAGS`, for example ``secret-write`` on ``vault.kv.generate``);
* a parameter with a secret-shaped name, on an operation that writes
  (class ``write`` or ``other``): the call stores the value it is given;
* a text field with a secret-shaped name in its answer, unless the
  operation declared the field as plain or the field holds a time
  (a name that ends in ``_at`` or ``_set``).

The secret-shaped names are the ones the redaction uses
(:func:`~meho_backplane.broadcast.events.is_secret_param_name`). Such an
operation must have the class ``credential_read``, ``credential_write`` or
``credential_mint``. A new operation is checked with no new test line.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Final

import pytest

from meho_backplane.broadcast.events import is_secret_param_name, resolve_op_class
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._secret_values import plain_fields_of
from meho_backplane.operations.service_grants import CREDENTIAL_CLASSES, credential_call_waits
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import _register_all

#: Tags that say an operation reads or writes a secret or a credential.
SECRET_TAGS: Final[frozenset[str]] = frozenset(
    {"secret-read", "secret-write", "credential-read", "credential-write", "credential-mint"}
)

#: Classes of an operation whose parameters go to the target as a change.
_WRITE_CLASSES: Final[frozenset[str]] = frozenset({"write", "other"})

#: JSON types that cannot hold a secret value.
_NO_SECRET_TYPES: Final[frozenset[str]] = frozenset({"boolean", "integer", "number", "null"})

#: Last words of a field that holds a time about a secret, not the secret.
_TIME_WORDS: Final[frozenset[str]] = frozenset({"at", "set"})


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, list):
        return {str(item) for item in declared}
    return {str(declared)} if declared is not None else set()


def _secret_fields(schema: Any, *, text_only: bool) -> list[str]:
    """Secret-shaped property names anywhere in *schema*.

    With *text_only*, only a field that can hold text counts: an object
    is walked into, not counted.
    """
    found: list[str] = []
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                types = _types(sub) if isinstance(sub, Mapping) else set()
                if text_only:
                    counts = not types or "string" in types
                else:
                    counts = not types or not types <= _NO_SECRET_TYPES
                if is_secret_param_name(str(name)) and counts:
                    found.append(str(name))
        for value in schema.values():
            found.extend(_secret_fields(value, text_only=text_only))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_secret_fields(item, text_only=text_only))
    return found


def secret_signs(row: EndpointDescriptor) -> list[str]:
    """What *row* declares that shows it stores or returns a secret value."""
    signs = [f"tag {tag}" for tag in row.tags or [] if tag in SECRET_TAGS]
    if resolve_op_class(row.op_id, row.op_class) in _WRITE_CLASSES:
        params = _secret_fields(row.parameter_schema or {}, text_only=False)
        signs += [f"parameter {name}" for name in sorted(set(params))]
    plain = plain_fields_of(row.op_id)
    answer = {
        name
        for name in _secret_fields(row.response_schema or {}, text_only=True)
        if name not in plain and name.rsplit("_", 1)[-1] not in _TIME_WORDS
    }
    signs += [f"answer field {name}" for name in sorted(answer)]
    return signs


def unclassified_secret_ops(rows: Iterable[EndpointDescriptor]) -> list[tuple[str, list[str]]]:
    """The rows that store or return a secret value and have no credential class."""
    missing: list[tuple[str, list[str]]] = []
    for row in rows:
        if resolve_op_class(row.op_id, row.op_class) in CREDENTIAL_CLASSES:
            continue
        signs = secret_signs(row)
        if signs:
            missing.append((row.op_id, signs))
    return sorted(missing)


def _made_up(
    op_id: str,
    *,
    tags: list[str] | None = None,
    params: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="acme",
        version="1.0",
        impl_id="acme",
        op_id=op_id,
        source_kind="typed",
        tags=tags or ["write"],
        parameter_schema={"type": "object", "properties": params or {}},
        response_schema={"type": "object", "properties": answer or {}},
        safety_level="caution",
        requires_approval=True,
        op_class=None,
    )


@pytest.mark.asyncio
async def test_every_registered_op_that_stores_or_returns_a_secret_has_a_credential_class() -> None:
    rows = await _register_all()
    assert len(rows) >= 100
    assert "vault.kv.generate" in {row.op_id for row in rows}

    assert unclassified_secret_ops(rows) == [], (
        "These operations store or return a secret value but have no credential class. "
        "Declare credential_read, credential_write or credential_mint for them."
    )


@pytest.mark.parametrize(
    "row",
    [
        _made_up("acme.password.generate", tags=["write", "secret-write"]),
        _made_up("acme.robot.create", answer={"api_key": {"type": "string"}}),
        _made_up("acme.user.set", params={"password": {"type": "string"}}),
    ],
    ids=["tag", "answer-field", "parameter"],
)
def test_a_made_up_secret_op_without_a_credential_class_fails_the_guard(
    row: EndpointDescriptor,
) -> None:
    assert [op_id for op_id, _ in unclassified_secret_ops([row])] == [row.op_id]


def test_the_guard_passes_a_made_up_op_with_a_credential_class_or_no_secret() -> None:
    classified = _made_up("acme.password.generate", tags=["write", "secret-write"])
    classified.op_class = "credential_write"
    rows = [
        classified,
        _made_up("acme.user.update", params={"display_name": {"type": "string"}}),
        _made_up(
            "acme.user.password_age",
            answer={
                "password_last_set": {"type": ["string", "null"]},
                "password_expires_at": {"type": "string"},
                "password_expired": {"type": "boolean"},
            },
        ),
    ]
    assert unclassified_secret_ops(rows) == []


@pytest.mark.asyncio
async def test_the_operations_that_can_send_any_credential_get_the_credential_rule() -> None:
    """A command in a container, a raw Proxmox write and every k8s.apply.

    They have no credential class, because their approval card must show the
    call. The rule holds for them all the same.
    """
    registered = {row.op_id for row in await _register_all()}
    assert {"k8s.exec", "proxmox.api.write", "k8s.apply"} <= registered

    assert credential_call_waits("k8s.exec")
    assert credential_call_waits("proxmox.api.write")
    assert credential_call_waits("k8s.apply")
