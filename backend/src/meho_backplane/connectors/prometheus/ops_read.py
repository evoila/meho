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

import math
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError

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


def _numeric_sample_value(raw: Any) -> float | None:
    """Parse one Prometheus sample-value string to a finite float, else ``None``.

    The Prometheus HTTP API encodes every sample value as a JSON string
    (``"1"``, ``"3.14"``, ``"1.5e9"``, and the non-finite sentinels ``"NaN"``
    / ``"+Inf"`` / ``"-Inf"``). A finite float parses; a non-finite sentinel
    or anything unparseable derives to ``None`` so the augmented payload stays
    strict-JSON-serialisable (JSON has no NaN/Inf literal) and a threshold
    sensor asserting the derived field lands in ``unknown`` (fail-safe) rather
    than on a bogus number.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sample_pair_value(sample: Any) -> float | None:
    """Numeric value of a Prometheus ``[timestamp, "value"]`` sample pair."""
    if isinstance(sample, list) and len(sample) == 2:
        return _numeric_sample_value(sample[1])
    return None


def _augment_vector(result: list[Any]) -> float | None:
    """Add ``value_num`` to each instant-vector sample dict in place.

    Returns the first sample's derived value for the envelope alias.
    """
    for entry in result:
        if isinstance(entry, dict):
            entry["value_num"] = _sample_pair_value(entry.get("value"))
    if result and isinstance(result[0], dict):
        first: float | None = result[0].get("value_num")
        return first
    return None


def _augment_matrix(result: list[Any]) -> float | None:
    """Add ``values_num`` (parallel to ``values``) to each series dict in place.

    Returns the first series' first sample value for the envelope alias.
    """
    for entry in result:
        if isinstance(entry, dict):
            values = entry.get("values")
            entry["values_num"] = (
                [_sample_pair_value(sample) for sample in values]
                if isinstance(values, list)
                else []
            )
    if result and isinstance(result[0], dict):
        nums = result[0].get("values_num")
        return nums[0] if nums else None
    return None


def _augment_numeric_samples(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach derived numeric siblings to a ``query`` / ``query_range`` envelope.

    Every raw sample value the Prometheus API returns is a JSON string, which
    the checks threshold comparator maps to ``unknown`` (it requires a real
    number by contract, #2504). This adds numeric siblings a sensor can assert
    directly -- the #2772/#2808 ``days_to_expiry`` shape -- while leaving the
    raw ``value`` / ``values`` fields byte-identical (wire fidelity):

    * ``vector`` result -> ``value_num`` (float|null) on each sample dict.
    * ``matrix`` result -> ``values_num`` (list[float|null], parallel to
      ``values``) on each series dict.
    * ``first_sample_value`` (float|null) on the envelope -- the numeric of the
      first series' first sample, mirroring #2808's top-level leaf alias, so a
      sensor asserts ``$.first_sample_value`` directly. ``scalar`` / ``string``
      results (whose ``result`` is a bare ``[ts, "val"]`` pair, not a list of
      dicts) surface only through this envelope alias.

    Mutates *payload* in place -- it is the freshly parsed body ``_api_get``
    owns per call -- and returns it. Bodies that are not one of the four PromQL
    result types (errors, unexpected shapes) get ``first_sample_value: null``
    and are otherwise untouched.
    """
    first: float | None = None
    data = payload.get("data")
    if isinstance(data, dict):
        result_type = data.get("resultType")
        result = data.get("result")
        if result_type == "vector" and isinstance(result, list):
            first = _augment_vector(result)
        elif result_type == "matrix" and isinstance(result, list):
            first = _augment_matrix(result)
        elif result_type in ("scalar", "string"):
            first = _sample_pair_value(result)
    payload["first_sample_value"] = first
    return payload


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
        except (httpx.HTTPError, OSError, VaultClientError, CredentialsReadError):
            return None
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        active = data.get("activeTargets") if isinstance(data, dict) else None
        return len(active) if isinstance(active, list) else None

    async def _best_effort_firing_alerts(self, target: Any, operator: Operator) -> int | None:
        """Count firing alerts from ``/api/v1/alerts`` -- ``None`` on any error."""
        try:
            payload = await self._api_get(target, "/api/v1/alerts", operator=operator)
        except (httpx.HTTPError, OSError, VaultClientError, CredentialsReadError):
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
        except (httpx.HTTPError, OSError, VaultClientError, CredentialsReadError):
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
        """``prometheus.query`` -- ``GET /api/v1/query``.

        The raw envelope is augmented with derived numeric sample fields
        (``value_num`` per vector sample, ``first_sample_value`` on the
        envelope) so threshold sensors can assert observed values; the raw
        ``value`` strings stay byte-identical. See
        :func:`_augment_numeric_samples`.
        """
        query: dict[str, Any] = {"query": params["query"]}
        for key in ("time", "timeout"):
            value = params.get(key)
            if value is not None:
                query[key] = value
        payload = await self._api_get(target, "/api/v1/query", operator=operator, params=query)
        return _augment_numeric_samples(payload)

    async def query_range(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``prometheus.query_range`` -- ``GET /api/v1/query_range``.

        The raw matrix envelope is augmented with derived numeric sample
        fields (``values_num`` parallel to each series' ``values``,
        ``first_sample_value`` on the envelope) so threshold sensors can
        assert observed values; the raw ``values`` pairs stay byte-identical.
        See :func:`_augment_numeric_samples`.
        """
        query: dict[str, Any] = {
            "query": params["query"],
            "start": params["start"],
            "end": params["end"],
            "step": params["step"],
        }
        timeout = params.get("timeout")
        if timeout is not None:
            query["timeout"] = timeout
        payload = await self._api_get(
            target, "/api/v1/query_range", operator=operator, params=query
        )
        return _augment_numeric_samples(payload)

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
