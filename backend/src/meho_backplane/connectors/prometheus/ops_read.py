# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-op handler mixin for :class:`PrometheusConnector`.

Initiative #2228 / Task #2234. The eight typed-op handlers and the three
best-effort fingerprint-augmentation helpers live here on the
:class:`PrometheusReadOps` mixin, keeping
:mod:`~meho_backplane.connectors.prometheus.connector` focused on
transport shaping, auth, the read-only gate, fingerprint, and probe.

The mixin is subclass-/mixin-correct with the dispatcher: the typed-op
registrar persists each handler as
``...prometheus.ops_read.PrometheusReadOps.<method>`` and the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`
walks the concrete connector's MRO to rebind the handler against the live
:class:`PrometheusConnector` instance -- the documented mixin path (see
that function's docstring). Every handler therefore reaches the
concrete connector's gated transport (:meth:`_api_get`) at dispatch time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

if TYPE_CHECKING:
    # These attributes are provided by the concrete PrometheusConnector the
    # mixin is combined with; declared here only so the type checker knows
    # the handlers can reach the gated transport. Never instantiated stand-alone.
    from meho_backplane.connectors.prometheus.connector import Target

    class _ConnectorProto:
        async def _api_get(
            self,
            target: Target,
            logical_path: str,
            *,
            operator: Operator,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]: ...

    _Base = _ConnectorProto
else:
    _Base = object


class PrometheusReadOps(_Base):
    """Typed read-op handlers + fingerprint-augmentation helpers.

    Combined into :class:`PrometheusConnector` via multiple inheritance;
    every method delegates to the concrete connector's gated
    :meth:`_api_get`, so the GET-only + ``/api/v1/`` allowlist applies to
    all of them by construction.
    """

    # ------------------------------------------------------------------
    # Best-effort fingerprint augmentation (each returns None on any error)
    # ------------------------------------------------------------------

    async def _best_effort_active_targets(self, target: Any, operator: Operator) -> int | None:
        """Count ``activeTargets`` from ``/api/v1/targets`` -- ``None`` on any error."""
        try:
            payload = await self._api_get(
                target, "/api/v1/targets", operator=operator, params={"state": "active"}
            )
        except (httpx.HTTPError, OSError, VaultClientError, VaultCredentialsReadError):
            return None
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        active = data.get("activeTargets") if isinstance(data, dict) else None
        return len(active) if isinstance(active, list) else None

    async def _best_effort_firing_alerts(self, target: Any, operator: Operator) -> int | None:
        """Count firing alerts from ``/api/v1/alerts`` -- ``None`` on any error."""
        try:
            payload = await self._api_get(target, "/api/v1/alerts", operator=operator)
        except (httpx.HTTPError, OSError, VaultClientError, VaultCredentialsReadError):
            return None
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        alerts = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(alerts, list):
            return None
        return sum(1 for a in alerts if isinstance(a, dict) and a.get("state") == "firing")

    async def _best_effort_rule_groups(self, target: Any, operator: Operator) -> int | None:
        """Count rule groups from ``/api/v1/rules`` -- ``None`` on any error."""
        try:
            payload = await self._api_get(target, "/api/v1/rules", operator=operator)
        except (httpx.HTTPError, OSError, VaultClientError, VaultCredentialsReadError):
            return None
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        groups = data.get("groups") if isinstance(data, dict) else None
        return len(groups) if isinstance(groups, list) else None

    # ------------------------------------------------------------------
    # Typed op handlers (each a thin, gated GET)
    # ------------------------------------------------------------------

    async def query(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.query`` -- ``GET /api/v1/query``."""
        query: dict[str, Any] = {"query": params["query"]}
        for key in ("time", "timeout"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        return await self._api_get(target, "/api/v1/query", operator=operator, params=query)

    async def query_range(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.query_range`` -- ``GET /api/v1/query_range``."""
        query: dict[str, Any] = {
            "query": params["query"],
            "start": params["start"],
            "end": params["end"],
            "step": params["step"],
        }
        timeout = params.get("timeout")
        if timeout is not None:
            query["timeout"] = timeout
        return await self._api_get(target, "/api/v1/query_range", operator=operator, params=query)

    async def series(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.series`` -- ``GET /api/v1/series``.

        The repeated ``match[]`` selector wire form is produced by passing
        a list value for the ``match[]`` key; httpx serialises it into
        ``match[]=a&match[]=b``.
        """
        query: dict[str, Any] = {"match[]": list(params["match"])}
        for key in ("start", "end"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        return await self._api_get(target, "/api/v1/series", operator=operator, params=query)

    async def labels(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.labels`` -- ``GET /api/v1/labels``."""
        query: dict[str, Any] = {}
        match = params.get("match")
        if match:
            query["match[]"] = list(match)
        for key in ("start", "end"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        return await self._api_get(
            target, "/api/v1/labels", operator=operator, params=query or None
        )

    async def targets(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.targets`` -- ``GET /api/v1/targets``."""
        query: dict[str, Any] = {}
        state = params.get("state")
        if state is not None:
            query["state"] = state
        return await self._api_get(
            target, "/api/v1/targets", operator=operator, params=query or None
        )

    async def rules(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.rules`` -- ``GET /api/v1/rules``."""
        query: dict[str, Any] = {}
        rule_type = params.get("type")
        if rule_type is not None:
            query["type"] = rule_type
        return await self._api_get(target, "/api/v1/rules", operator=operator, params=query or None)

    async def alerts(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.alerts`` -- ``GET /api/v1/alerts``."""
        del params  # declared empty in schema
        return await self._api_get(target, "/api/v1/alerts", operator=operator)

    async def raw_get(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.get`` -- constrained GET passthrough under ``/api/v1/``.

        The path is re-validated by :meth:`_api_get`'s gate, so even
        though the parameter schema pins ``pattern: ^/api/v1/``, a path
        that slips past JSON-Schema (or a caller bypassing validation)
        still cannot leave the read-only surface.
        """
        path = params["path"]
        query = params.get("query")
        return await self._api_get(target, path, operator=operator, params=query if query else None)
