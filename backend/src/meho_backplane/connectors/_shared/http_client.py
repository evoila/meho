# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one way a connector package builds an ``httpx`` client.

The connector kit offers :func:`build_http_client` as
``kit.build_http_client``. It returns an ``httpx.AsyncClient`` whose request
hook is :func:`~meho_backplane.auth.principal_block.check_before_send`, so the
check of the caller's block runs right before each request, also before each
redirect hop. The template's guard test of a connector package reports every
other way to build an ``httpx`` client. See ``docs-site/extend/connector-kit.md``,
"A client your package builds".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from meho_backplane.auth.principal_block import check_before_send

__all__ = ["build_http_client"]


def _retries(transport: httpx.AsyncBaseTransport | None) -> int:
    """The connection retries of *transport*: 0 for a transport that is not ``httpx``'s own."""
    if not isinstance(transport, httpx.AsyncHTTPTransport):
        return 0
    retries = getattr(getattr(transport, "_pool", None), "_retries", None)
    # Unknown means "may retry": refuse rather than guess.
    return retries if isinstance(retries, int) else -1


def build_http_client(
    *,
    event_hooks: Mapping[str, list[Any]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    mounts: Mapping[str, httpx.AsyncBaseTransport | None] | None = None,
    **options: Any,
) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` that checks the caller's block before each request.

    It takes the options of ``httpx.AsyncClient``, for example ``base_url``,
    ``timeout``, ``verify``, ``auth`` and ``follow_redirects`` (``False`` by
    default: the client follows no redirects). When it follows redirects, the
    check also runs before each redirect hop.

    *event_hooks* may hold hooks of the package. The check is added as the last
    ``"request"`` hook, so it runs right before the request goes out.

    A transport (*transport*, or one in *mounts*) must not retry by itself:
    such a retry sends again without the hook. So an
    ``httpx.AsyncHTTPTransport`` with ``retries`` other than 0 is refused with
    :class:`ValueError`. A package that wants a retry sends the request again
    through the client, and the check runs again.
    """
    for name, given in [
        ("transport", transport),
        *((f"mounts[{k!r}]", v) for k, v in (mounts or {}).items()),
    ]:
        if _retries(given) != 0:
            raise ValueError(
                f"build_http_client: {name} retries by itself, without the check of the "
                "caller's block; use httpx.AsyncHTTPTransport(retries=0) and send a retry "
                "again through the client"
            )
    hooks = {key: list(value) for key, value in (event_hooks or {}).items()}
    hooks["request"] = [*hooks.get("request", []), check_before_send]
    return httpx.AsyncClient(event_hooks=hooks, transport=transport, mounts=mounts, **options)
