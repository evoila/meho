# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The restart of the HCX application service, and its status read.

``hcx.manager.restart`` stops, then starts the HCX application service
(``POST /components/appengine?action=stop``, then ``?action=start``, on the
appliance management API, port 9443), so a vCenter, SSO or NSX change takes
effect. Each call waits until the vendor is done, up to 300 s. HCX needs
several minutes more before its main API answers again. HCX gives no job id
for a restart; the caller follows it with ``hcx.manager.service.status``
(``GET /components/appengine/status``), then ``hcx.about``.

Before the stop, the restart reads the service state: a service that is
starting or stopping means a restart already runs, so it returns that and
starts nothing. A service that is stopped (for example after a failed
start) cannot answer on the main API and moves no migration, so the restart
only starts it. Otherwise it asks the main HCX API (port 443, the
connector's own login) for migrations that have not ended
(``POST /hybridity/api/migrations?action=query``) and refuses while one
runs. ``skip_migration_check`` does not skip that check: it only lets the
restart go on when the main API refused MEHO's login, the state of a new
Manager whose single sign-on does not work before its first restart. A
check that answers and shows a running migration refuses the restart in
every case. Both checks are a help, not a lock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.hcx.appliance_api import (
    ApplianceApi,
    HcxApplianceError,
    HcxManagerSetupError,
    appliance_session,
)
from meho_backplane.connectors.hcx.manager_setup import (
    _APPENGINE_PATH,
    _APPENGINE_STATUS_PATH,
    _VCENTER_PATH,
    _configs,
    _error_text,
    _step,
)
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import rows_of

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "MIGRATION_QUERY_CALL",
    "hcx_manager_restart_impl",
    "hcx_manager_service_status_impl",
]

_MIGRATIONS_PATH: Final = "/hybridity/api/migrations"

#: The one main-API call (port 443): the migration check before a restart.
MIGRATION_QUERY_CALL: Final = ("POST", _MIGRATIONS_PATH)

#: The read that follows a restart.
_RESTART_STATUS_READ: Final = "hcx.manager.service.status"

#: The stop and the start of the application service wait until they are done.
_SERVICE_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)

#: Why the migration check could not run: a vendor, login or credential error.
_CHECK_ERRORS: Final = (
    httpx.HTTPError,
    RuntimeError,
    ValueError,
    CredentialsReadError,
    VaultClientError,
)

#: The next step when the main API refused the login of the migration check.
_LOGIN_REMEDIATION: Final = (
    "Fix the HCX login of the target and run again. Only on a new HCX Manager whose "
    "single sign-on does not work before its first restart, set skip_migration_check: "
    "true; it applies only to this refused login."
)

#: Why the migration check was skipped on a stopped service.
_STOPPED_REASON: Final = (
    "the application service is stopped: the main API cannot answer and no migration moves"
)

_RUNNING: Final = "RUNNING"
_STOPPED: Final = "STOPPED"
_IN_PROGRESS: Final[frozenset[str]] = frozenset({"STARTING", "STOPPING"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_running(row: dict[str, Any]) -> bool:
    """A migration row that has not ended: ``progress.isDone`` false, not failed or cancelled."""
    progress = row.get("progress")
    progress = progress if isinstance(progress, dict) else {}
    if progress.get("isCancelled") is True or progress.get("didFail") is True:
        return False
    return progress.get("isDone") is False


async def _migration_check(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    api: ApplianceApi,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Refuse when a migration runs.

    Skipped when no vCenter is registered (then no migration can run), or
    when the main API refused MEHO's login and ``skip_migration_check`` is
    set: the state of a new Manager whose single sign-on does not work
    before its first restart. Every other failure of the check refuses the
    restart, and so does a check that shows a running migration.
    """
    if not _configs(await api.get_or_none(_VCENTER_PATH)):
        return {"state": "skipped", "reason": "no vCenter is registered, so no migration can run"}
    try:
        payload = await connector._post_json(
            target,
            _MIGRATIONS_PATH,
            operator=operator,
            params={"action": "query"},
            json={"filter": {"skipDrafts": True}},
        )
    except ConnectorAuthError as exc:
        if params.get("skip_migration_check"):
            return {
                "state": "skipped",
                "reason": (
                    "the main API refused the HCX login (single sign-on does not work "
                    "yet) and skip_migration_check was set"
                ),
            }
        raise HcxManagerSetupError(
            "MEHO could not check for running migrations: the main API refused the HCX login",
            remediation=_LOGIN_REMEDIATION,
        ) from exc
    except _CHECK_ERRORS as exc:
        raise HcxManagerSetupError(
            f"MEHO could not check for running migrations: {type(exc).__name__}",
            remediation="Make the main API of the HCX Manager answer, then run again.",
        ) from exc
    rows = rows_of(payload)
    if not rows and isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = [row for row in payload["rows"] if isinstance(row, dict)]
    running = [row for row in rows if _is_running(row)]
    if running:
        ids = ", ".join(str(row.get("migrationId") or row.get("id")) for row in running)
        raise HcxManagerSetupError(
            f"{len(running)} migrations still run on this HCX Manager ({ids[:500]}); "
            "a restart now would break them",
            remediation="Wait until hcx.migration.list shows them ended, then restart.",
        )
    return {"state": "passed", "reason": "no migration runs"}


def _service_state(answer: Any) -> str | None:
    state = answer.get("result") if isinstance(answer, dict) else None
    return str(state).upper() if state else None


def _restart_answer(
    status: str, service_state: str | None, check: dict[str, Any], steps: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "status": status,
        "job_id": None,
        "service_state": service_state,
        "migration_check": check,
        "steps": steps,
        "status_read": _RESTART_STATUS_READ,
        "checked_at": _now(),
    }


async def _service_action(api: ApplianceApi, action: str) -> tuple[str | None, str | None]:
    """Stop or start the application service: ``(state, error)``; raises for no answer."""
    try:
        answer = await api.call(
            "POST", _APPENGINE_PATH, params={"action": action}, timeout=_SERVICE_TIMEOUT
        )
    except HcxApplianceError as exc:
        return None, _error_text(exc)
    if isinstance(answer, dict) and str(answer.get("operationStatus") or "").upper() == "FAILED":
        return _service_state(answer), f"HCX answered operationStatus FAILED for {action}"
    return _service_state(answer), None


async def hcx_manager_restart_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.restart`` -- stop, then start the HCX application service.

    Returns ``already_running`` when the service is already starting or
    stopping, ``accepted`` when the start was taken (HCX needs several
    minutes before it is usable), and ``failed`` or ``no_answer`` with the
    steps when the stop or the start failed. Refuses while migrations run.
    A stopped service (after a failed start) is only started: its main API
    cannot answer the migration check, and nothing moves while it is down.
    """
    async with appliance_session(connector, operator, target) as api:
        state = _service_state(await api.get(_APPENGINE_STATUS_PATH))
        if state in _IN_PROGRESS:
            return _restart_answer("already_running", state, {"state": "not_run"}, [])
        if state == _STOPPED:
            check: dict[str, Any] = {"state": "skipped", "reason": _STOPPED_REASON}
        else:
            check = await _migration_check(connector, operator, target, api, params)
        steps: list[dict[str, Any]] = []
        try:
            if state == _STOPPED:
                steps.append(_step("stop", "skipped", None))
            else:
                state, error = await _service_action(api, "stop")
                if error is not None:
                    steps += [_step("stop", "failed", error), _step("start", "not_run")]
                    return _restart_answer("failed", state, check, steps)
                steps.append(_step("stop", "done"))
            state, error = await _service_action(api, "start")
        except httpx.TransportError as exc:
            if steps:
                steps.append(_step("start", "unknown", _error_text(exc)))
            else:
                steps += [_step("stop", "unknown", _error_text(exc)), _step("start", "not_run")]
            return _restart_answer("no_answer", None, check, steps)
        if error is not None:
            steps.append(_step("start", "failed", error))
            return _restart_answer("failed", state, check, steps)
        steps.append(_step("start", "done"))
    return _restart_answer("accepted", state, check, steps)


def _is_not_reachable(exc: httpx.TransportError) -> bool:
    """True when the Manager did not answer; an address MEHO refused is not that."""
    if isinstance(exc, SsrfBlockedError):
        return False
    return "CERTIFICATE_VERIFY_FAILED" not in str(exc)


async def hcx_manager_service_status_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.service.status`` -- ``GET /components/appengine/status``.

    When the Manager does not answer, returns ``state: not_reachable`` with
    ``checked_at``; it never says the service failed.
    """
    del params
    checked_at = _now()
    try:
        async with appliance_session(connector, operator, target) as api:
            answer = await api.get(_APPENGINE_STATUS_PATH)
    except httpx.TransportError as exc:
        if not _is_not_reachable(exc):
            raise
        return {
            "service": "appengine",
            "state": "not_reachable",
            "reachable": False,
            "running": False,
            "operation_status": None,
            "checked_at": checked_at,
        }
    state = _service_state(answer)
    return {
        "service": "appengine",
        "state": state,
        "reachable": True,
        "running": state == _RUNNING,
        "operation_status": answer.get("operationStatus") if isinstance(answer, dict) else None,
        "checked_at": checked_at,
    }
