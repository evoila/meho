# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the credential card of a target shows, and how its form is read.

The card on the target page lets a person store the credential of one target
in the fields its connector declares (``credential_ways``). This module holds
the parts that do not render anything:

* :func:`plan_credential_card`: which of the card states applies (a form, "no
  credential needed", "command line for now", Google Secret Manager, ...) and
  the Vault mount and path the credential is stored at. The path is the
  target's own ``secret_ref``. The person never types it.
* :func:`read_stored_status`: whether a credential is stored there, with its
  version and time. It runs ``vault.kv.versions``, which returns no value.
* :func:`read_credential_form`: turns the submitted form into the fields of
  the chosen way. It never puts a submitted value into an error text.

The value is typed in the web console and goes only to ``vault.kv.put``. This
module never logs a value and never returns one in an error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from meho_backplane.api.v1.connectors_target_form import _classes_for
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.credential_backend import split_credential_ref
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.target_form import CredentialWay
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.vault.routes import _DEFAULT_MOUNT, _OP_VERSIONS, _dispatch

__all__ = [
    "CredentialForm",
    "CredentialPlan",
    "StoredStatus",
    "plan_credential_card",
    "read_credential_form",
    "read_stored_status",
]

#: The longest value of one field. A kubeconfig or a private key is a few
#: kilobytes; this leaves room and still stops a huge paste at the form.
MAX_VALUE_LENGTH: Final[int] = 262_144

#: Form field names are ``field__<way key>__<field name>``, so two ways can
#: have a field with the same name.
FIELD_PREFIX: Final[str] = "field__"


@dataclass(frozen=True)
class CredentialPlan:
    """Which card state applies.

    ``state`` is one of ``form`` (a form is shown), ``no_credential`` (the
    connector needs none), ``cli`` (the connector has not declared its
    fields), ``gsm`` (Google Secret Manager), ``other_store`` (a store the
    console cannot write to) or ``no_path`` (the target has no secret path).
    """

    state: str
    ways: tuple[CredentialWay, ...] = ()
    #: Vault mount and path of the credential (state ``form`` only).
    mount: str = ""
    path: str = ""
    #: The name of the secret to create by hand (state ``gsm``) or the store kind.
    store_name: str = ""


@dataclass(frozen=True)
class StoredStatus:
    """Whether a credential is stored. It never holds a value."""

    #: ``stored``, ``none``, ``error`` or ``skipped`` (the caller may not read).
    state: str
    version: int | None = None
    changed_at: str | None = None
    #: The answer of a failed read, for the shared error text.
    envelope: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class CredentialForm:
    """The submitted form, read. ``errors`` never holds a submitted value."""

    way: CredentialWay | None
    data: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _declared_ways(
    classes: Sequence[type[Connector]], preferred_impl_id: str | None
) -> tuple[CredentialWay, ...] | None:
    """The ways of the one class that applies, or ``None`` when none declares them.

    Several classes of a product can serve one target. When they declare
    different ways, the target's preferred implementation decides. When that
    does not settle it, the connector counts as not declared: a wrong form is
    worse than no form.
    """
    declared = [c for c in classes if c.credential_ways is not None]
    if not declared:
        return None
    if preferred_impl_id:
        chosen = [c for c in declared if c.impl_id == preferred_impl_id]
        if chosen:
            declared = chosen
    first = declared[0].credential_ways
    if all(c.credential_ways == first for c in declared):
        return first
    return None


async def plan_credential_card(operator: Operator, target: TargetORM) -> CredentialPlan:
    """Decide the state of the credential card for *target*."""
    classes = await _classes_for(operator, target.product.strip().lower())
    ways = _declared_ways(classes, target.preferred_impl_id)
    if ways is None:
        return CredentialPlan(state="cli")
    if not ways:
        return CredentialPlan(state="no_credential")
    secret_ref = (target.secret_ref or "").strip()
    if not secret_ref:
        return CredentialPlan(state="no_path", ways=ways)
    kind, store_ref = split_credential_ref(
        secret_ref, default_backend=get_settings().credential_backend
    )
    if kind == "gsm":
        return CredentialPlan(state="gsm", ways=ways, store_name=store_ref)
    if kind != "vault":
        return CredentialPlan(state="other_store", ways=ways, store_name=kind)
    return CredentialPlan(
        state="form",
        ways=ways,
        mount=_DEFAULT_MOUNT,
        path=store_ref.strip().strip("/"),
    )


def _stored_from_versions(result: Mapping[str, Any]) -> StoredStatus:
    """Read ``current_version`` and its time out of a ``vault.kv.versions`` result."""
    current = result.get("current_version")
    versions = result.get("versions")
    if not isinstance(current, int) or isinstance(current, bool) or not isinstance(versions, dict):
        return StoredStatus(state="none")
    meta = versions.get(str(current))
    if not isinstance(meta, dict):
        return StoredStatus(state="none")
    if meta.get("destroyed") or meta.get("deletion_time"):
        return StoredStatus(state="none")
    created = meta.get("created_time")
    return StoredStatus(
        state="stored",
        version=current,
        changed_at=created if isinstance(created, str) else None,
    )


async def read_stored_status(
    operator: Operator, connector_id: str | None, mount: str, path: str
) -> StoredStatus:
    """Run ``vault.kv.versions`` and say whether a credential is stored.

    A path with no secret is "none". Any other failure is "error": the card
    then says it cannot tell, never "not stored".
    """
    if connector_id is None:
        return StoredStatus(state="error", error="no_vault_connector")
    envelope = await _dispatch(operator, connector_id, _OP_VERSIONS, "", mount, path)
    if envelope.get("status") == "ok":
        return _stored_from_versions(envelope.get("result") or {})
    extras = envelope.get("extras") or {}
    if extras.get("exception_class") == "InvalidPath":
        return StoredStatus(state="none")
    return StoredStatus(state="error", envelope=envelope)


def read_credential_form(
    form: Mapping[str, Any], ways: tuple[CredentialWay, ...]
) -> CredentialForm:
    """Read the submitted form for the chosen way.

    Only the fields of the chosen way are read. An empty required field, a
    value that is not text (a file upload) and a value over the length limit
    give a short error that names the field, never the value.
    """
    way_key = form.get("way")
    way = next((w for w in ways if w.key == way_key), None)
    if way is None:
        return CredentialForm(way=None, errors={"way": "Choose how MEHO signs in to this system."})
    data: dict[str, str] = {}
    errors: dict[str, str] = {}
    for item in way.fields:
        raw = form.get(f"{FIELD_PREFIX}{way.key}__{item.name}")
        if raw is not None and not isinstance(raw, str):
            errors[item.name] = "Send this field as text."
        elif raw is None or raw == "":
            if item.required:
                errors[item.name] = "This field is required."
        elif len(raw) > MAX_VALUE_LENGTH:
            errors[item.name] = f"This field is longer than {MAX_VALUE_LENGTH} characters."
        else:
            data[item.name] = raw
    if not data and not errors:
        errors["_form"] = "Fill in at least one field."
    return CredentialForm(way=way, data=data, errors=errors)
