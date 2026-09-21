# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cache-Control-aware static file serving for the operator console.

The console references its compiled Tailwind bundle and its vendored JS
at **stable, unversioned** URLs -- ``/ui/static/dist/tailwind.css``,
``/ui/static/src/vendor/*.js``, the self-hosted fonts, and the ``app/``
controllers. The *content* of those files changes on every release, but
the URL does not (the console is server-rendered HTMX/Jinja2, not a
hashed-filename SPA bundle).

Starlette's :class:`~starlette.staticfiles.StaticFiles` emits ``ETag``
and ``Last-Modified`` for each asset but **no** ``Cache-Control``
header. A response with a validator but no explicit freshness lifetime
is *heuristically cacheable* (RFC 9111 §4.2.2): a browser may reuse its
cached copy WITHOUT revalidating for a heuristic window (commonly 10% of
the ``Last-Modified`` age). Because the URL is stable across releases,
after a roll a returning operator's browser keeps serving the PREVIOUS
release's cached CSS -- or, during the window evoila/meho#3647 was live,
the empty (utilities-less) bundle -- until the heuristic window expires
or the operator hard-reloads. That is the recurring "the console lost
its CSS after a deploy, a hard refresh fixes it" symptom.

:class:`RevalidateStaticFiles` sets ``Cache-Control: no-cache`` on every
asset. ``no-cache`` does not mean "do not cache" -- it means "cache, but
revalidate with the origin before every reuse". The browser issues a
conditional ``If-None-Match`` request; the origin already answers a
matching validator with ``304 Not Modified`` (an empty-body response),
so the full asset is re-downloaded only when it actually changed. The
cost is one cheap revalidation round-trip per asset per load on an
internal operator console; the payoff is that a deploy can never leave a
browser rendering against a stale stylesheet. HTML pages are already
served ``Cache-Control: no-store`` by the UI routers, so only the static
mount needed this.
"""

from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class RevalidateStaticFiles(StaticFiles):
    """:class:`StaticFiles` that forces revalidation of every asset.

    Overrides the single response choke point
    (:meth:`~starlette.staticfiles.StaticFiles.get_response`, which
    returns either the ``200`` :class:`~starlette.responses.FileResponse`
    or the ``304`` ``NotModifiedResponse``) and stamps
    ``Cache-Control: no-cache`` on whatever comes back. ``setdefault``
    is used so an asset that ever grows its own explicit caching policy
    is left untouched.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", "no-cache")
        return response
