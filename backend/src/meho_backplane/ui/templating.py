# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Jinja2 environment factory for the UI chassis.

A single :class:`jinja2.Environment` is constructed once per process
and reused across requests. Construction-time cost (FileSystemLoader
scan + template-cache initialisation) is paid up-front; per-request
``Environment.get_template`` reads from the cache.

Configuration decisions:

* :class:`jinja2.FileSystemLoader` over :class:`PackageLoader` because
  the dev workflow runs ``tailwindcss --watch`` against the same
  template tree on disk; ``FileSystemLoader`` picks up template edits
  without a process restart when ``auto_reload=True`` is honoured by
  uvicorn's ``--reload``. The image deploy doesn't depend on reload
  (gunicorn/uvicorn workers are immutable).
* ``autoescape=select_autoescape(["html", "htm", "xml"])`` so every
  ``{{ user_input }}`` substitution HTML-escapes by default. The
  Jinja2 docs explicitly recommend ``select_autoescape`` over a
  blanket ``True`` because the latter also escapes ``.txt`` /
  ``.json`` templates we may add later; the former gates the
  behaviour on the template file extension. Source:
  https://jinja.palletsprojects.com/en/stable/api/#autoescaping
* :class:`jinja2.StrictUndefined` so a typo in a template variable
  name (``{{ apv_version }}`` instead of ``{{ app_version }}``) raises
  :class:`jinja2.UndefinedError` at render time instead of silently
  rendering an empty string. FastAPI's exception middleware turns the
  500 into a structlog event the operator sees in CI / kubectl logs.

The environment exposes one extra global, ``app_version``, bound from
:func:`meho_backplane.version.deployed_version_label` so every template
can show the *deployed* build identity in the footer without each route
having to pass it explicitly. The label reads the same ``CHART_VERSION``
/ ``GIT_SHA`` environment variables ``GET /version`` reports (#1698 —
the global used to bind the static package ``__version__``, which is
pinned to ``0.1.0-dev`` by design and never tracks the deployed image).
Binding once at environment construction is equivalent to reading
per-request: the env vars are injected at image build / Pod start and
cannot change for the life of the process. Tests that monkeypatch them
rebuild the singleton via :func:`reset_templating_for_testing`. Tenant /
operator-identity globals will be wired by T4 (#865) once the session
middleware lands.

This module deliberately does **not** wire FastAPI dependencies or
``Request`` objects. T5 (#866) builds the ``TemplateResponse`` helper
on top of this Environment; the helper signature stays a v0.2 design
question (FastAPI's :class:`starlette.templating.Jinja2Templates` is
the candidate, but the factory below leaves the choice open).
"""

from __future__ import annotations

from typing import Any, Final

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.requests import Request

from meho_backplane.ui.paths import templates_dir
from meho_backplane.version import deployed_version_label

# Cached environment singleton. Module-level cache rather than
# functools.lru_cache so the type checker sees the concrete return
# type without ``cast``; lru_cache + Jinja2 + mypy has a known
# False-positive on the cached return type at strict mode.
_ENV: Environment | None = None

#: Cached :class:`Jinja2Templates` wrapper bound to :data:`_ENV`. T5
#: (#866) route handlers call ``TemplateResponse(request, name, context)``
#: through this object so FastAPI's response wiring (URL reverse-lookup
#: helper + ``request`` context injection) lights up. Constructed
#: lazily alongside :func:`get_jinja_env` so the chassis-only Task #863
#: stays free of FastAPI route plumbing.
_TEMPLATES: Jinja2Templates | None = None

_AUTOESCAPE_EXTS: Final[tuple[str, ...]] = ("html", "htm", "xml")


def get_jinja_env() -> Environment:
    """Return the lazily-constructed UI Jinja2 environment.

    The environment is constructed the first time a UI route renders
    a template; subsequent calls return the cached instance. The
    template cache inside the environment is keyed by filename, so
    edits in dev are picked up automatically when uvicorn reload
    re-imports the module (the singleton lives in module scope so
    a reload throws it away).
    """
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(templates_dir())),
            autoescape=select_autoescape(_AUTOESCAPE_EXTS),
            undefined=StrictUndefined,
            # ``trim_blocks`` + ``lstrip_blocks`` reduce the
            # whitespace noise the {% if %} blocks otherwise leave in
            # the rendered HTML. Matches the DaisyUI snippet
            # conventions; no functional behaviour change.
            trim_blocks=True,
            lstrip_blocks=True,
            # ``keep_trailing_newline`` keeps the trailing \n on the
            # base.html output so curl-style introspection lines
            # cleanly.
            keep_trailing_newline=True,
        )
        _ENV.globals["app_version"] = deployed_version_label()
    return _ENV


def _ui_session_context_processor(request: Request) -> dict[str, Any]:
    """Surface the BFF session tenant + live readiness into every template.

    G0.15-T9 (#1217) — the operator-console header chip used to render a
    hard-coded "(sign in to choose)" placeholder regardless of whether
    the operator had already authenticated. The fix moves the chip's
    data source onto the same :class:`UISessionContext` the middleware
    already attaches to ``request.state.ui_session``: the JWT's
    ``tenant_id`` claim is the operator's default tenant and is
    auto-selected at session-create time, so by the time any UI route
    renders a page the tenant is already known.

    The context processor exposes two variables to every Jinja render:

    * ``session_tenant`` -- a ``dict`` carrying ``id`` / ``slug`` /
      ``name``, or ``None`` when the request hit a surface where the
      session middleware hasn't bound a context (the auth surfaces
      themselves, e.g. ``/ui/auth/login`` -- the operator is by
      definition not signed in there). ``base.html``'s header chip
      conditionally renders the tenant name when this dict is present
      and falls back to a neutral "Sign in" link otherwise.
    * ``ready`` -- the live backplane readiness verdict that colours
      ``base.html``'s sidebar-footer pill (green "ready" vs yellow
      "starting"). Every ``/ui/*`` surface used to hardcode
      ``ready=False`` in its own context dict, so the pill was stuck on
      "starting" on every page but the dashboard (#1776). The verdict
      now comes from ``request.state.ui_ready``, read once per
      request by :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
      from :func:`~meho_backplane.health.ui_readiness_verdict` -- the
      stale-while-revalidate accessor that serves the cached verdict and
      never runs a probe sweep on the request path.
      Because Starlette runs context processors *after* the route's own
      context dict and ``dict.update``\\ s their output over it
      (``starlette.templating.Jinja2Templates.TemplateResponse``), this
      value wins over any stray per-route literal -- so the surfaces drop
      theirs. The dashboard writes its own freshly-probed verdict back
      to ``request.state.ui_ready`` so the processor re-injects the same
      value and the dashboard's behaviour is unchanged.

      The default is ``False`` ("starting"), the correct fail-safe when
      no verdict is bound: the auth/static surfaces (no session
      middleware), or a bare ``Environment.render`` outside the request
      path. ``base.html`` reads ``ready`` under ``StrictUndefined``, so
      the key must always be present.

    Synchronous by Starlette contract: context processors execute
    inside the synchronous ``Jinja2Templates.TemplateResponse`` call;
    asynchronous processors are explicitly not supported (see
    https://www.starlette.io/templates/). Both lookups here are pure
    attribute reads off ``request.state`` -- the middleware already
    paid the DB round-trip and the (cached) probe sweep on its way in,
    so the processor itself issues zero IO.
    """
    ready = bool(getattr(request.state, "ui_ready", False))
    session_ctx = getattr(request.state, "ui_session", None)
    if session_ctx is None:
        return {"session_tenant": None, "ready": ready}
    return {
        "session_tenant": {
            "id": str(session_ctx.tenant_id),
            "slug": session_ctx.tenant_slug,
            "name": session_ctx.tenant_name,
        },
        "ready": ready,
    }


def get_templates() -> Jinja2Templates:
    """Return the lazily-constructed :class:`Jinja2Templates` wrapper.

    FastAPI 0.136's :class:`fastapi.templating.Jinja2Templates` accepts
    a pre-built ``env=`` argument; we pass our chassis singleton so the
    autoescape / ``StrictUndefined`` / ``app_version`` global plumbing
    survives across the wrapper. The wrapper itself adds two things
    on top of a bare :class:`~jinja2.Environment`:

    * A :class:`fastapi.templating._TemplateResponse` factory invoked as
      ``templates.TemplateResponse(request, "name.html", {...})`` -- the
      ``request`` argument is the FastAPI-blessed shape (Starlette 1.0
      deprecates the legacy ``(name, {request, ...})`` signature).
    * A ``url_for`` Jinja global bound to the request's router so
      surface templates can reverse route names instead of hard-coding
      ``href=`` strings.

    The wrapper also runs :func:`_ui_session_context_processor` per
    render so ``base.html`` (and any surface template inheriting it)
    sees the active BFF session's tenant on every page without each
    route having to thread ``tenant_name`` / ``tenant_slug`` through
    its context dict (G0.15-T9 #1217).

    Module-level cache (rather than :func:`functools.lru_cache`) so the
    type checker sees the concrete return type without ``cast``.
    """
    global _TEMPLATES
    if _TEMPLATES is None:
        _TEMPLATES = Jinja2Templates(
            env=get_jinja_env(),
            context_processors=[_ui_session_context_processor],
        )
    return _TEMPLATES


def reset_templating_for_testing() -> None:
    """Drop the cached env + templates wrapper. Test-only.

    Production never mutates these caches under a running process. The
    UI smoke test rebuilds the cache between cases (e.g. when verifying
    the env loads against a tmp-path override) by calling this helper
    inside its fixture teardown.
    """
    global _ENV, _TEMPLATES
    _ENV = None
    _TEMPLATES = None
