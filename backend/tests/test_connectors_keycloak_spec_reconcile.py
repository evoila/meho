# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""keycloak real-spec reconcile lane (#2988) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the keycloak connector
dispatches is served by the pinned ``keycloak-26.3`` spec on the shelf
(``keycloak-admin-openapi.json`` — the vendor's Admin REST API OpenAPI
3.0.3 document from Maven Central
``org.keycloak:keycloak-api-docs-dist:26.3.3``, Apache-2.0, provenance
in the shelf's ``MANIFEST.md``; version matches the lab's deployed
Keycloak 26.3.3). Parse-only set compare — no DB, no embeddings, no
containers — so it lives in the required unit sweep per the
lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when
the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror): #2988 hoisted every
request path out of inline f-strings (previously spread across
``connector.py`` / ``ops_read.py`` / ``ops_write.py`` /
``secret_endpoint.py``) into the ``_*_PATH`` template constants of
:mod:`~meho_backplane.connectors.keycloak._paths`, which every handler
now fills via ``fill_path`` — so these constants ARE the dispatched
surface. Template placeholders carry the pinned spec's own parameter
names (``{realm}``, ``{client-uuid}``, ``{user-id}``, ``{role-name}``),
making the reconcile byte-for-byte against
:func:`~meho_backplane.operations.ingest.parse_openapi`'s op_ids. The
HTTP method cannot be introspected from a bare path string, so
:data:`_METHODS_BY_CONSTANT` declares it per constant (several
templates dispatch under two methods — list/create, get/update) and a
guard test pins the constant-name set; the method claims come from the
dispatch seams: ``_get_json`` / ``_get_admin_json`` / ``_get_admin_list``
are GET-only, every ``_write_admin`` call site names its verb
explicitly, and the token mint is a ``client.post``.

Sweep note (f-strings / concatenations): after the #2988 hoist the
keycloak package contains no other composed request path (verified by
grep for ``/admin`` / ``/realms`` string literals). The
``probe_method = f"GET {realm_path}"`` string in ``fingerprint`` is
display metadata derived from the same filled constant, never
dispatched (the vcf-operations lane's display-metadata precedent).

Two hand-coded paths are dispatched but deliberately **excluded** from
the reconcile — the #2970 decision-table class "surface lives outside
the pinned spec", evidenced rather than repointed (the pinned spec's
only top-level path family is ``/admin/realms``; verified 2026-08-18,
first armed run):

* ``POST:/realms/{realm}/protocol/openid-connect/token`` — the OIDC
  token endpoint the admin-token mint POSTs. Specified by OAuth 2.0
  (RFC 6749 §3.2) / per-realm OIDC discovery
  (``/realms/{realm}/.well-known/openid-configuration``), not by the
  Admin REST OpenAPI document. No served alternative exists.
* ``GET:/admin/serverinfo`` — the admin console's server-info endpoint
  (best-effort version read; the connector already treats any failure
  as non-fatal ``version=None``). Stable in practice but not modeled by
  the published Admin REST OpenAPI document. No served alternative
  exists.

Both exclusions are pinned by value below, and an armed test asserts
they stay **unserved** — the day a newer pinned spec starts serving
one, that test fails and the constant must be promoted into
:data:`_METHODS_BY_CONSTANT`. All 18 reconciled op_ids were served on
the first armed run; no path repoints were needed. Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from meho_backplane.connectors.keycloak import _paths
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "keycloak-26.3"
_SPEC_FILE = "keycloak-admin-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: HTTP method(s) per hand-coded path constant reconciled against the
#: pinned spec. Declared (not introspected) because a module-level path
#: string carries no method; the guard test below pins this mapping's
#: key set against the live constants so the two can never drift apart
#: silently. Constants serving both a collection read and a create (or
#: a get and an update) declare both verbs.
_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "_ADMIN_REALMS_PATH": ("POST",),
    "_ADMIN_REALM_PATH": ("GET", "PUT"),
    "_CLIENTS_PATH": ("GET", "POST"),
    "_CLIENT_PATH": ("GET", "PUT"),
    "_CLIENT_PROTOCOL_MAPPERS_PATH": ("POST",),
    "_CLIENT_SCOPES_PATH": ("GET", "POST"),
    "_USERS_PATH": ("GET", "POST"),
    "_USER_ROLE_MAPPINGS_PATH": ("GET",),
    "_USER_ROLE_MAPPINGS_REALM_PATH": ("POST",),
    "_USER_RESET_PASSWORD_PATH": ("PUT",),
    "_ROLES_PATH": ("GET",),
    "_ROLE_PATH": ("GET",),
    "_ROLE_USERS_PATH": ("GET",),
}

#: Dispatched constants deliberately outside the pinned Admin REST
#: spec's scope (module docstring: OIDC protocol endpoint; unmodeled
#: serverinfo). Method per constant, mirroring the dispatch seams.
_EXCLUDED_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "_TOKEN_PATH": ("POST",),
    "_SERVERINFO_PATH": ("GET",),
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` constants of ``keycloak._paths``, by constant name."""
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _op_ids(methods_by_constant: dict[str, tuple[str, ...]]) -> set[str]:
    """``METHOD:/path`` op_ids for *methods_by_constant* from the live constants."""
    constants = _path_constants()
    return {
        f"{method}:{constants[name]}"
        for name, methods in methods_by_constant.items()
        for method in methods
    }


def test_path_constants_are_all_discovered_and_method_mapped() -> None:
    """Guard: the introspection finds every path constant, each with a method.

    Pinning the exact constant-name set (the #2944 guard shape) means a
    renamed or dropped ``_*_PATH`` constant — or a new one that skips
    the ``_PATH`` suffix convention or lacks a method mapping — can't
    silently shrink the reconciled set to a vacuous pass.
    """
    assert sorted(_path_constants()) == sorted(
        [*_METHODS_BY_CONSTANT, *_EXCLUDED_METHODS_BY_CONSTANT]
    )


def test_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration wiring is
    proven on every sweep — the nsx-lane manifest precedent. A new
    hand-coded path, a renamed constant, or a changed method must update
    this manifest consciously, and the lane's declared set moves with
    it. Template segments carry the vendor's own parameter names
    (reconciled against the pinned spec in the armed test below).
    """
    assert sorted(_op_ids(_METHODS_BY_CONSTANT)) == [
        "GET:/admin/realms/{realm}",
        "GET:/admin/realms/{realm}/client-scopes",
        "GET:/admin/realms/{realm}/clients",
        "GET:/admin/realms/{realm}/clients/{client-uuid}",
        "GET:/admin/realms/{realm}/roles",
        "GET:/admin/realms/{realm}/roles/{role-name}",
        "GET:/admin/realms/{realm}/roles/{role-name}/users",
        "GET:/admin/realms/{realm}/users",
        "GET:/admin/realms/{realm}/users/{user-id}/role-mappings",
        "POST:/admin/realms",
        "POST:/admin/realms/{realm}/client-scopes",
        "POST:/admin/realms/{realm}/clients",
        "POST:/admin/realms/{realm}/clients/{client-uuid}/protocol-mappers/models",
        "POST:/admin/realms/{realm}/users",
        "POST:/admin/realms/{realm}/users/{user-id}/role-mappings/realm",
        "PUT:/admin/realms/{realm}",
        "PUT:/admin/realms/{realm}/clients/{client-uuid}",
        "PUT:/admin/realms/{realm}/users/{user-id}/reset-password",
    ]
    assert sorted(_op_ids(_EXCLUDED_METHODS_BY_CONSTANT)) == [
        "GET:/admin/serverinfo",
        "POST:/realms/{realm}/protocol/openid-connect/token",
    ]


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every reconciled hand-coded op_id is served by the pinned keycloak-26.3 spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_op_ids(_METHODS_BY_CONSTANT), served, spec_label=_SPEC_LABEL)


def test_excluded_op_ids_stay_unserved_by_the_pinned_spec() -> None:
    """The evidenced exclusions remain genuinely outside the pinned spec.

    Keeps the exclusion list honest: the day a newer pinned spec version
    starts serving one of these surfaces, this fails and the constant
    must move from ``_EXCLUDED_METHODS_BY_CONSTANT`` into
    ``_METHODS_BY_CONSTANT`` (reconciled) instead of staying silently
    excluded.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    wrongly_served = _op_ids(_EXCLUDED_METHODS_BY_CONSTANT) & served
    assert not wrongly_served, (
        f"excluded op_id(s) are now served by the pinned {_SPEC_LABEL}: "
        f"{sorted(wrongly_served)} — promote them into _METHODS_BY_CONSTANT "
        "(the exclusion's premise no longer holds)."
    )
