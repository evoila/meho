# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The extra settings and the default port a connector declares, in the target form.

A connector class can declare ``summary``, ``default_port`` and ``target_fields``
(:mod:`meho_backplane.connectors.target_form`). When a person picks a product,
the console target form shows what the class declares:

* the one-line summary under the product;
* one input for each extra setting, with its label, help text and a
  "required" mark;
* the declared port, filled in when the person has not typed one.

The values go into the target's ``extras``. A product whose classes declare
nothing shows the form as before. When the classes of one product declare
different settings, the form asks which class the target uses and stores the
answer as ``preferred_impl_id``.

This module only reads the declarations. It reads no secret and writes
nothing: the target is saved by ``create_target`` and ``update_target`` like
before. The Keycloak realm keeps its own input
(:mod:`meho_backplane.ui.routes.connectors.forms_realm`), so a declared field
named ``managed_realm`` is not shown for a Keycloak target.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from meho_backplane.api.v1.connectors_target_form import _classes_for
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.target_form import TargetField
from meho_backplane.ui.routes.connectors.forms_realm import is_keycloak

__all__ = [
    "EXTRA_PREFIX",
    "DeclaredForm",
    "check_declared",
    "declared_view",
    "extra_values_from",
    "load_declared",
    "port_after_product_change",
]

#: Every declared setting is posted as ``extra__<key>``.
EXTRA_PREFIX = "extra__"

_REQUIRED = "This setting is required."


@dataclass(frozen=True)
class DeclaredForm:
    """What the connector classes of one product declare for the form."""

    classes: tuple[type[Connector], ...]
    chosen: type[Connector]
    fields: tuple[TargetField, ...]
    #: True when the classes of the product declare different settings.
    needs_choice: bool


def _signature(cls: type[Connector]) -> tuple[object, object]:
    return (cls.default_port, cls.target_fields)


def _fields_of(cls: type[Connector], product: str) -> tuple[TargetField, ...]:
    return tuple(
        f
        for f in (cls.target_fields or ())
        if not (f.key == "managed_realm" and is_keycloak(product))
    )


async def load_declared(
    operator: Operator, product: str, impl_id: str | None = None
) -> DeclaredForm | None:
    """The declarations for *product*, or ``None`` when no class declares anything."""
    wanted = product.strip().lower()
    if not wanted:
        return None
    classes = tuple(await _classes_for(operator, wanted))
    if not classes:
        return None
    needs_choice = len({_signature(c) for c in classes}) > 1
    chosen = next((c for c in classes if needs_choice and c.impl_id == impl_id), classes[0])
    fields = _fields_of(chosen, wanted)
    if not (needs_choice or fields or chosen.summary or chosen.default_port):
        return None
    return DeclaredForm(classes=classes, chosen=chosen, fields=fields, needs_choice=needs_choice)


def extra_values_from(items: Mapping[str, str]) -> dict[str, str]:
    """The posted ``extra__<key>`` values, by field name."""
    return {k: v for k, v in items.items() if k.startswith(EXTRA_PREFIX)}


def _raw_value(
    field: TargetField, values: Mapping[str, object], stored: Mapping[str, Any]
) -> object:
    name = EXTRA_PREFIX + field.key
    if name in values:
        return values[name]
    if field.key in stored:
        return stored[field.key]
    return field.default


def declared_view(
    declared: DeclaredForm,
    *,
    values: Mapping[str, object],
    stored: Mapping[str, Any] | None = None,
    errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The template context of the declared part of the form.

    A typed value wins over a stored value, and a stored value over the
    declared default.
    """
    stored = stored or {}
    errors = errors or {}
    fields = []
    for f in declared.fields:
        name = EXTRA_PREFIX + f.key
        raw = _raw_value(f, values, stored)
        fields.append(
            {
                "name": name,
                "label": f.label,
                "help": f.help,
                "required": f.required,
                "kind": f.kind,
                "choices": list(f.choices),
                "value": "" if raw is None else str(raw),
                "checked": str(raw).strip().lower() == "true",
                "error": errors.get(name),
            }
        )
    return {
        "summary": declared.chosen.summary,
        "default_port": declared.chosen.default_port,
        "fields": fields,
        "impl_choices": (
            [{"impl_id": c.impl_id, "version": c.version} for c in declared.classes]
            if declared.needs_choice
            else None
        ),
        "chosen_impl_id": declared.chosen.impl_id,
        "impl_error": errors.get("preferred_impl_id"),
    }


def _read_field(f: TargetField, raw: str | None) -> tuple[Any, str | None]:
    """The value to store for one field, or ``None`` and an error text."""
    text = (raw or "").strip()
    if f.kind == "boolean":
        return text.lower() == "true", None
    if not text:
        return None, (_REQUIRED if f.required else None)
    if f.kind == "number":
        try:
            return int(text), None
        except ValueError:
            return None, "Enter a whole number."
    if f.kind == "choice" and text not in f.choices:
        return None, "Pick one of the listed values."
    return text, None


def check_declared(
    declared: DeclaredForm, posted: Mapping[str, str], impl_choice: str | None
) -> tuple[dict[str, Any], str | None, dict[str, str]]:
    """Read the declared settings from the posted form.

    Returns ``(extras, preferred_impl_id, errors)``. ``extras`` holds the fields
    that have a value (an empty optional field is left out); ``errors`` is keyed
    by the form field name.
    """
    extras: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in declared.fields:
        name = EXTRA_PREFIX + f.key
        value, error = _read_field(f, posted.get(name))
        if error is not None:
            errors[name] = error
        elif value is not None:
            extras[f.key] = value
    impl: str | None = None
    if declared.needs_choice:
        if impl_choice in {c.impl_id for c in declared.classes}:
            impl = impl_choice
        else:
            errors["preferred_impl_id"] = "Pick one of the listed connectors."
    return extras, impl, errors


def port_after_product_change(
    typed: str | None, previous_default: str | None, new: int | None
) -> str:
    """The port to show after the product (or class) changed.

    The declared port replaces an empty port and a port that was only the
    earlier declared one. A port the person typed stays.
    """
    text = (typed or "").strip()
    if text and text != (previous_default or "").strip():
        return text
    return "" if new is None else str(new)
