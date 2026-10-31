# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``<ns>.integration.set``: add or change an Orchestrator, IPAM or Git integration.

MEHO does this work inside its own call, in three steps:

1. **test** — ``POST /iaas/api/integrations?validateOnly=true`` with the full
   body. The vendor runs its connection test and answers with a request
   tracker. MEHO reads the tracker until it ends. A failed test stops here;
   nothing is saved.
2. **save** — ``POST /iaas/api/integrations`` (add) or
   ``PATCH /iaas/api/integrations/{id}`` (change), again followed through
   its request tracker.
3. **read** — read the saved integration.

The answer lists the steps that ran, the step that failed with the vendor's
error, and the steps that did not run. MEHO undoes nothing.

The password or token comes from Vault (``password_secret_ref``) and goes
only into the request body; a vendor text that repeats it is replaced.

The shape rules of an IPAM integration (learned on a real system; the
public API description does not say them): the running environment is
``faasProviderEndpointId``, the login goes at the top level
(``privateKeyId`` / ``privateKey``), and the appliance's certificate goes
**inside** ``integrationProperties.certificate``. MEHO refuses the shapes that
fail and puts the certificate where each type reads it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    DEFAULT_WAIT_SECONDS,
    INTEGRATION_PATH,
    INTEGRATIONS_PATH,
    Calls,
    Steps,
    follow_tracker,
    path_with,
    read_secret,
    vendor_text,
)
from meho_backplane.connectors.vmware_shared.vcfa_integrations import (
    GIT_INTEGRATION_TYPES,
    integration_row,
    list_integrations,
)

__all__ = ["IPAM_TEST_NOTE", "integration_set"]

#: The note an IPAM test always adds to the answer.
IPAM_TEST_NOTE: Final[str] = (
    "The vendor's test runs the IPAM provider's validate action. It leaves one "
    "extensibility action run record, also when nothing is saved. That record is not an "
    "integration."
)

_ID_IN_LINK: Final = re.compile(r"/iaas/api/integrations/([^/?#]+)")
_LOGIN_KEYS: Final[frozenset[str]] = frozenset({"privateKey", "privateKeyId"})


def _check_properties(
    params: Mapping[str, Any], integration_type: str, existing: Mapping[str, Any] | None
) -> dict[str, str]:
    """The properties to send: the stored ones (on a change) with the caller's on top.

    Raises :exc:`ValueError` for a shape that fails.
    """
    raw = params.get("integration_properties") or {}
    given = {str(key): str(value) for key, value in raw.items()}
    old = (existing or {}).get("integrationProperties") or {}
    props = {**{str(k): str(v) for k, v in old.items()}, **given}
    for key in given:
        if is_secret_param_name(key) or key in _LOGIN_KEYS:
            raise ValueError(
                f"integration_properties.{key}: a login does not go into the integration "
                "properties. Give user_name and password_secret_ref (the vendor wants the "
                "login at the top level). Nothing was sent."
            )
    if integration_type != "ipam":
        return props
    if "endpointId" in props:
        raise ValueError(
            "integration_properties.endpointId: an IPAM integration takes its running "
            "environment as faasProviderEndpointId (the id of the abx.endpoint integration). "
            "With endpointId the vendor's test fails with 'No suitable regions for "
            "execution'. Nothing was sent."
        )
    missing = [key for key in ("providerId", "faasProviderEndpointId") if not props.get(key)]
    if missing:
        raise ValueError(
            f"an IPAM integration needs integration_properties {', '.join(missing)}: "
            "providerId comes from the IPAM package import, faasProviderEndpointId is the id "
            "of the abx.endpoint integration. Nothing was sent."
        )
    props.setdefault("dcId", "onprem")
    return props


def _check_login(params: Mapping[str, Any], integration_type: str) -> None:
    needs_secret = integration_type == "ipam" or integration_type in GIT_INTEGRATION_TYPES
    if needs_secret and not params.get("password_secret_ref"):
        raise ValueError(
            f"a {integration_type} integration needs password_secret_ref (the password or "
            "token, read from Vault); the vendor's test runs with it. Nothing was sent."
        )
    if integration_type == "ipam" and not params.get("user_name"):
        raise ValueError(
            "an IPAM integration needs user_name: the vendor wants the login at the top "
            "level, not inside the integration properties. Nothing was sent."
        )


def _spec(
    params: Mapping[str, Any], integration_type: str, props: dict[str, str], secret: str | None
) -> dict[str, Any]:
    """The vendor's integration body. The certificate goes where the type reads it."""
    spec: dict[str, Any] = {
        "name": params["name"],
        "integrationType": integration_type,
        "integrationProperties": props,
    }
    if params.get("description"):
        spec["description"] = params["description"]
    if params.get("user_name"):
        spec["privateKeyId"] = params["user_name"]
    if secret is not None:
        spec["privateKey"] = secret
    custom = {str(k): str(v) for k, v in (params.get("custom_properties") or {}).items()}
    if integration_type == "ipam":
        custom.setdefault("isExternal", "true")
    if custom:
        spec["customProperties"] = custom
    certificate = params.get("certificate")
    if certificate and integration_type == "ipam":
        # The IPAM provider reads its certificate from the integration
        # properties; given as certificateInfo, the provider's own test fails.
        props["certificate"] = str(certificate)
    elif certificate:
        spec["certificateInfo"] = {"certificate": str(certificate)}
    return spec


@dataclass
class _Run:
    """The state of one ``integration.set`` call, for the answer."""

    calls: Calls
    params: Mapping[str, Any]
    steps: Steps = field(default_factory=lambda: Steps("test", "save", "read"))
    secrets: tuple[str | None, ...] = ()
    notes: list[str] = field(default_factory=list)
    test: dict[str, Any] | None = None
    save: dict[str, Any] | None = None

    @property
    def wait(self) -> float:
        return float(self.params.get("wait_seconds") or DEFAULT_WAIT_SECONDS)

    def answer(
        self, status: str, guidance: str, integration: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "status": status,
            "integration": integration_row(integration) if integration is not None else None,
            "test": self.test,
            "save": self.save,
            "steps": self.steps.rows(),
            "notes": list(self.notes),
            "guidance": guidance,
        }


async def _existing_for_change(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        row = await calls.iaas("GET", path_with(INTEGRATION_PATH, params["id"], "id"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ValueError(
                f"no integration has the id {params['id']!r}; nothing was sent"
            ) from None
        raise
    row = row if isinstance(row, dict) else {}
    wanted = params.get("integration_type")
    if wanted and wanted != row.get("integrationType"):
        raise ValueError(
            f"integration {params['id']!r} has the type {row.get('integrationType')!r}, not "
            f"{wanted!r}; a type cannot change. Nothing was sent."
        )
    return row


async def integration_set(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<ns>.integration.set``: test, then save, then read the integration.

    A refused input answers ``invalid_request`` with the reason (the op is
    credential-class, so a raised error would hide its text).
    """
    try:
        return await _integration_set(calls, params)
    except ValueError as exc:
        return _Run(calls, params).answer("invalid_request", str(exc))


async def _integration_set(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    existing = await _existing_for_change(calls, params) if params.get("id") else None
    integration_type = str(
        (existing or {}).get("integrationType") or params.get("integration_type") or ""
    )
    if not integration_type:
        raise ValueError("integration_type is required to add an integration; nothing was sent")
    props = _check_properties(params, integration_type, existing)
    _check_login(params, integration_type)
    run = _Run(calls, params)
    if existing is None:
        same = [r for r in await list_integrations(calls) if r.get("name") == params["name"]]
        if same:
            return run.answer(
                "exists",
                f"An integration with this name exists. Call {calls.op('integration.set')} "
                "with its id to change it. Nothing was changed.",
                same[0],
            )
    secret = await read_secret(calls, params, "password")
    run.secrets = (secret,)
    spec = _spec(params, integration_type, props, secret)
    if integration_type == "ipam":
        run.notes.append(IPAM_TEST_NOTE)
    if not await _test(run, spec):
        return run.answer(
            "test_failed" if run.steps.rows()[0]["result"] == "failed" else "unfinished",
            "The test did not pass, so nothing was saved. Read the step error, fix the input "
            "(address, login, certificate) and call again. If the test did not end in time, "
            f"read its tracker with {calls.op('integration.list')} request_tracker_id.",
        )
    return await _save(run, spec)


async def _test(run: _Run, spec: dict[str, Any]) -> bool:
    try:
        sent = await run.calls.iaas("POST", INTEGRATIONS_PATH, json=spec, validateOnly="true")
    except httpx.HTTPStatusError as exc:
        run.steps.failed("test", vendor_text(exc, run.secrets))
        return False
    run.test = await follow_tracker(run.calls, run.steps, "test", sent, run.wait, run.secrets)
    return bool(run.test["status"] == "FINISHED")


async def _save(run: _Run, spec: dict[str, Any]) -> dict[str, Any]:
    calls, params = run.calls, run.params
    try:
        if params.get("id"):
            body = {k: v for k, v in spec.items() if k != "integrationType"}
            path = path_with(INTEGRATION_PATH, params["id"], "id")
            sent = await calls.iaas("PATCH", path, json=body)
        else:
            sent = await calls.iaas("POST", INTEGRATIONS_PATH, json=spec)
    except httpx.HTTPStatusError as exc:
        run.steps.failed("save", vendor_text(exc, run.secrets))
        return run.answer("failed", "The test passed, but the vendor refused the save.")
    run.save = await follow_tracker(calls, run.steps, "save", sent, run.wait, run.secrets)
    if run.save["status"] != "FINISHED":
        return run.answer(
            "failed" if run.save["status"] == "FAILED" else "unfinished",
            "The save did not finish. Read the integrations with "
            f"{calls.op('integration.list')} before you call again.",
        )
    new_id = params.get("id") or next(
        (m.group(1) for res in run.save["resources"] if (m := _ID_IN_LINK.search(res))), None
    )
    row: Mapping[str, Any] | None = None
    try:
        if new_id:
            row = await calls.iaas("GET", path_with(INTEGRATION_PATH, new_id, "id"))
        else:
            row = next(r for r in await list_integrations(calls) if r.get("name") == spec["name"])
        run.steps.done("read")
    except (httpx.HTTPError, StopIteration, ValueError) as exc:
        run.steps.failed("read", type(exc).__name__)
    return run.answer(
        "saved",
        "The integration is saved. The vendor may still collect data through it (for IPAM: "
        "its IP ranges) for a few minutes.",
        row if isinstance(row, Mapping) else None,
    )
