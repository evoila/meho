# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The connector class for the made-up widget API.

The widget API has these paths:

* ``GET /api/v1/version``: ``{"version": "1.4.2"}``.
* ``GET /api/v1/widgets?page=<n>``: one page of widgets,
  ``{"items": [{"id", "name", "color"}, ...], "next_page": <n> | null}``.
* ``GET /api/v1/widgets/<id>``: one widget.
* ``PATCH /api/v1/widgets/<id>`` with ``{"name": ...}``: rename a widget. The
  answer is the changed widget.

Every call sends ``Authorization: Bearer <token>``. The token is the
``token`` field of the target's secret in Vault.

This file imports MEHO only through the connector kit.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from meho_connector_kit import (
    FingerprintResult,
    HttpConnector,
    Operator,
    ProbeResult,
    Target,
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
    synthesise_system_operator,
)

PRODUCT = "example"
VERSION = "1.x"
IMPL_ID = "example-widget"
#: The connector id that agents and operators use: ``<impl_id>-<version>``.
CONNECTOR_ID = f"{IMPL_ID}-{VERSION}"

#: A widget id: letters, digits, ``-`` and ``_``. So an id can never change
#: the path of the call (no ``/``, no ``..``).
WIDGET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_WIDGET_ID = re.compile(WIDGET_ID_PATTERN)

#: The list stops with an error after this many pages, so a broken API that
#: always sends a next page cannot keep the call running.
MAX_PAGES = 100

_VERSION_PATH = "/api/v1/version"
_WIDGETS_PATH = "/api/v1/widgets"


class WidgetAnswerError(Exception):
    """The widget API sent an answer that does not have the expected shape."""


def widget_path(widget_id: object) -> str:
    """The path of one widget. Refuses an id that does not match the pattern."""
    if not isinstance(widget_id, str) or not _WIDGET_ID.match(widget_id):
        raise ValueError(f"widget_id must match {WIDGET_ID_PATTERN}")
    return f"{_WIDGETS_PATH}/{quote(widget_id, safe='')}"


def _widget(value: object) -> dict[str, Any]:
    """One widget from an answer: ``{"id", "name", "color"}``."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise WidgetAnswerError("the widget API sent a widget without an id")
    return {"id": value["id"], "name": value.get("name"), "color": value.get("color")}


class ExampleWidgetConnector(HttpConnector):
    """Connector for the made-up widget API, version 1."""

    product = PRODUCT
    version = VERSION
    impl_id = IMPL_ID

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Read the token from the target's secret in Vault, as the caller."""
        secret = await load_vault_secret_data(target, operator)
        token = secret.get("token")
        if not token:
            raise VaultCredentialsReadError(
                f"the secret of target {target.name!r} has no 'token' field"
            )
        return {"Authorization": f"Bearer {strip_credential_value(token)}"}

    async def fingerprint(
        self, target: Target, operator: Operator | None = None
    ) -> FingerprintResult:
        """Read the version of the widget API."""
        probed_at = datetime.now(UTC)
        try:
            answer = await self._get_json(
                target, _VERSION_PATH, operator=operator or synthesise_system_operator()
            )
        except Exception as exc:  # any failure means "not reachable", with the reason
            return FingerprintResult(
                vendor="example",
                product=PRODUCT,
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {_VERSION_PATH}",
                extras={"error": type(exc).__name__},
            )
        version = answer.get("version")
        return FingerprintResult(
            vendor="example",
            product=PRODUCT,
            version=version if isinstance(version, str) else None,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {_VERSION_PATH}",
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Check that the widget API answers."""
        start = time.monotonic()
        probed_at = datetime.now(UTC)
        reason: str | None = None
        try:
            await self._get_json(target, _VERSION_PATH, operator=synthesise_system_operator())
        except Exception as exc:  # any failure means "not ok", with the reason
            reason = type(exc).__name__
        return ProbeResult(
            ok=reason is None,
            reason=reason,
            latency_ms=(time.monotonic() - start) * 1000.0,
            probed_at=probed_at,
        )

    async def get_widget(
        self, operator: Operator, target: Target, widget_id: object
    ) -> dict[str, Any]:
        """Read one widget."""
        return _widget(await self._get_json(target, widget_path(widget_id), operator=operator))

    async def list_widgets(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``example.widget.list``: every widget, from all pages.

        A failed page or an answer of the wrong shape raises an error. The
        list is never cut short without an error.
        """
        del params
        widgets: list[dict[str, Any]] = []
        page: object = 1
        for _ in range(MAX_PAGES):
            answer = await self._get_json(
                target, _WIDGETS_PATH, operator=operator, params={"page": page}
            )
            items = answer.get("items")
            if not isinstance(items, list):
                raise WidgetAnswerError("the widget API sent a page without an 'items' list")
            widgets.extend(_widget(item) for item in items)
            page = answer.get("next_page")
            if page is None:
                return {"items": widgets}
        raise WidgetAnswerError(f"the widget list has more than {MAX_PAGES} pages")

    async def rename_widget(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``example.widget.rename``: give one widget a new name.

        MEHO runs this only after a person approved the call. The widget is
        found by its id, the same id the approver saw in the preview.
        """
        path = widget_path(params.get("widget_id"))
        answer = await self._post_json(
            target, path, operator=operator, verb="PATCH", json={"name": params["new_name"]}
        )
        return _widget(answer)
