# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-call guest credential override for the guest-ops channel (#3364).

A guest op may name a *different* secret for the guest OS login than the
vCenter target's own ``secret_ref``, through the optional
``guest_secret_ref`` parameter. One vCenter manages many VMs, and those
VMs can carry different guest logins; without the override every VM
reached through one target had to share the target secret's
``guest_username`` / ``guest_password``, and operators registered a
duplicate vCenter target per guest credential to work around it.

The override is a *reference string only* (the same forms a target
``secret_ref`` accepts: schemeless, ``vault:``, ``gsm:``). The credential
value is read in-process under the operator's identity, through the same
scheme-dispatched backend the connector already uses, and never enters
params, a result, an audit row, a broadcast event, or a log line.

Why the reference is bounded
============================

``guest_secret_ref`` is caller-chosen per call, and the ``safe`` guest
reads auto-execute for a granted agent with no approval intercept. An
unbounded reference would let a caller point guest authentication at
*any* secret the operator's JWT can read and learn from the guest login
outcome whether it holds a valid guest credential -- a credential-validity
oracle over everything the JWT reaches. "Authorized to read" is broader
than "appropriate for this target's guest", so the supplied reference
must fall under a per-target **allowed prefix**:

* by default, the parent namespace of the target's own ``secret_ref``
  (``targets/vc-01`` allows ``targets/<anything>``). The namespace comes
  from the secret's identity only: a ``#field`` selector and a GSM
  ``/versions/<n>`` pin are dropped first, so ``gsm:proj/vc/versions/4``
  allows ``gsm:proj/<anything>``, not just other versions of ``vc``;
* optionally replaced by the per-target ``extras["guest_secret_prefix"]``
  setting, which an operator uses to widen or narrow the namespace.

When the reference falls outside the prefix, or no prefix can be
resolved at all (a target without a ``secret_ref`` and without the
setting, or a single-segment ``secret_ref`` with no parent namespace),
the call is **rejected before any store read** -- no fallback to the
target secret, no guest login attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.credential_backend import (
    parse_credential_scheme,
    split_credential_ref,
)
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_param_secret_data,
    secret_ref_prefix,
    strip_credential_value,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "GUEST_SECRET_PREFIX_EXTRA",
    "GUEST_SECRET_REF_PARAM",
    "GuestSecretRefScopeError",
    "allowed_guest_secret_prefix",
    "check_guest_secret_ref",
    "load_guest_override_credentials",
]

#: The op parameter carrying the override reference.
GUEST_SECRET_REF_PARAM = "guest_secret_ref"

#: Per-target ``extras`` key that replaces the default allowed prefix.
GUEST_SECRET_PREFIX_EXTRA = "guest_secret_prefix"

#: Path segments that would escape or blur the allowed prefix.
_REJECTED_SEGMENTS: frozenset[str] = frozenset({"", ".", ".."})


class GuestSecretRefScopeError(ValueError):
    """``guest_secret_ref`` falls outside the target's allowed prefix.

    Raised **before** any credential-store read, so the message carries
    only the ref cut to its prefix (never the full path, never a value).
    Distinct from a store denial: the operator may well be allowed to read
    the secret, but it is not one this target's guest logins may use.
    """


@dataclass(slots=True)
class _GuestSecretTarget:
    """Adapter presenting the op's ``guest_secret_ref`` to the shared reader.

    Carries the vCenter target's ``name`` / ``host`` for log and error
    attribution while pointing the reader at the override reference --
    the same shape the keycloak ``password_secret_ref`` reader uses.
    """

    name: str
    host: str
    secret_ref: str | None


def _split(ref: str) -> tuple[str, str]:
    """Split *ref* into ``(backend kind, store path)``.

    The deployment-default backend is resolved only for a schemeless ref,
    so an explicit ``vault:`` / ``gsm:`` ref never touches the settings.
    """
    scheme = parse_credential_scheme(ref)
    default_backend = scheme if scheme is not None else get_settings().credential_backend
    return split_credential_ref(ref, default_backend=default_backend)


def _secret_identity(kind: str, store_ref: str) -> str:
    """Return the store path that names the secret itself.

    Drops the ``#field`` selector every backend accepts and, for ``gsm``,
    the ``/versions/<n>`` pin (``proj/secret/versions/4#pw`` becomes
    ``proj/secret``). Neither part changes *which* secret the target uses,
    so neither may change the namespace derived from it.
    """
    path = store_ref.rpartition("#")[0] if "#" in store_ref else store_ref
    if kind == "gsm":
        segments = path.split("/")
        if len(segments) == 4 and segments[2] == "versions":
            path = "/".join(segments[:2])
    return path


def _parent_namespace(store_ref: str) -> str | None:
    """Return the parent namespace of *store_ref* with a trailing slash, or ``None``."""
    head, sep, _tail = store_ref.rpartition("/")
    if not sep or not head:
        return None
    return head + "/"


def allowed_guest_secret_prefix(target: Any) -> tuple[str, str] | None:
    """Return the ``(kind, store prefix)`` a ``guest_secret_ref`` must fall under.

    ``extras["guest_secret_prefix"]`` wins when set to a non-empty string;
    otherwise the parent namespace of the target's own ``secret_ref``,
    taken from the secret's identity (no ``#field`` selector, no GSM
    version pin). ``None`` means no prefix is resolvable -- the caller
    rejects.
    """
    extras = getattr(target, "extras", None)
    setting = extras.get(GUEST_SECRET_PREFIX_EXTRA) if isinstance(extras, dict) else None
    if isinstance(setting, str) and setting.strip():
        kind, store_prefix = _split(setting.strip())
        if not store_prefix:
            return None
        return kind, store_prefix if store_prefix.endswith("/") else store_prefix + "/"

    secret_ref = getattr(target, "secret_ref", None)
    if not isinstance(secret_ref, str) or not secret_ref.strip():
        return None
    kind, store_ref = _split(secret_ref.strip())
    parent = _parent_namespace(_secret_identity(kind, store_ref))
    return None if parent is None else (kind, parent)


def check_guest_secret_ref(guest_secret_ref: str, target: Any) -> None:
    """Reject *guest_secret_ref* unless it falls under the target's allowed prefix.

    Fail-closed: a missing prefix, a backend-kind mismatch, a ``.`` / ``..``
    / empty path segment, or a path outside the prefix all raise
    :class:`GuestSecretRefScopeError`. Nothing is read from any store.
    """
    shown = secret_ref_prefix(guest_secret_ref)
    allowed = allowed_guest_secret_prefix(target)
    target_name = getattr(target, "name", "?")
    if allowed is None:
        raise GuestSecretRefScopeError(
            f"guest_secret_ref {shown!r} rejected: target {target_name!r} has no "
            f"allowed guest-secret prefix (its secret_ref has no parent namespace "
            f"and extras.{GUEST_SECRET_PREFIX_EXTRA} is unset)"
        )
    kind, store_ref = _split(guest_secret_ref)
    allowed_kind, prefix = allowed
    in_scope = (
        kind == allowed_kind
        and store_ref.startswith(prefix)
        and len(store_ref) > len(prefix)
        and not any(part in _REJECTED_SEGMENTS for part in store_ref.split("/"))
    )
    if not in_scope:
        raise GuestSecretRefScopeError(
            f"guest_secret_ref {shown!r} rejected: it is outside the allowed "
            f"guest-secret prefix {allowed_kind}:{prefix!r} of target {target_name!r}"
        )


async def load_guest_override_credentials(
    guest_secret_ref: str,
    target: Any,
    operator: Operator,
    *,
    fields: tuple[str, ...],
) -> dict[str, str]:
    """Read the guest login *fields* from the override reference.

    Order: the prefix check first (no store read on rejection), then the
    read through
    :func:`~meho_backplane.connectors._shared.vault_creds.load_param_secret_data`
    under the operator's identity. A store denial or an unreadable path
    raises ``SecretRefReadError`` naming the ``guest_secret_ref`` param; a
    secret missing one of *fields* raises
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`.
    Neither carries the full path or a value. There is no fallback to the
    target's own secret on any failure.
    """
    ref = guest_secret_ref.strip()
    check_guest_secret_ref(ref, target)
    adapter = _GuestSecretTarget(name=target.name, host=target.host, secret_ref=ref)
    secret_data = await load_param_secret_data(adapter, operator, param=GUEST_SECRET_REF_PARAM)
    credentials: dict[str, str] = {}
    for field_name in fields:
        value = secret_data.get(field_name) if isinstance(secret_data, dict) else None
        if value is None:
            raise VaultCredentialsReadError(
                f"secret at {GUEST_SECRET_REF_PARAM}={secret_ref_prefix(ref)!r} is missing "
                f"the {field_name!r} field required for the guest login"
            )
        credentials[field_name] = strip_credential_value(value)
    return credentials
