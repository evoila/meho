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
:data:`meho_backplane.__version__` so every template can show the
backplane version in the footer without each route having to pass it
explicitly. Tenant / operator-identity globals will be wired by T4
(#865) once the session middleware lands.

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

from meho_backplane import __version__
from meho_backplane.ui.paths import templates_dir

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
        _ENV.globals["app_version"] = __version__
    return _ENV


def _ui_session_context_processor(request: Request) -> dict[str, Any]:
    """Surface the active BFF session's tenant into every UI template.

    G0.15-T9 (#1217) — the operator-console header chip used to render a
    hard-coded "(sign in to choose)" placeholder regardless of whether
    the operator had already authenticated. The fix moves the chip's
    data source onto the same :class:`UISessionContext` the middleware
    already attaches to ``request.state.ui_session``: the JWT's
    ``tenant_id`` claim is the operator's default tenant and is
    auto-selected at session-create time, so by the time any UI route
    renders a page the tenant is already known.

    The context processor exposes one variable to every Jinja render:

    * ``session_tenant`` -- a ``dict`` carrying ``id`` / ``slug`` /
      ``name``, or ``None`` when the request hit a surface where the
      session middleware hasn't bound a context (the auth surfaces
      themselves, e.g. ``/ui/auth/login`` -- the operator is by
      definition not signed in there). ``base.html``'s header chip
      conditionally renders the tenant name when this dict is present
      and falls back to a neutral "Sign in" link otherwise.

    Synchronous by Starlette contract: context processors execute
    inside the synchronous ``Jinja2Templates.TemplateResponse`` call;
    asynchronous processors are explicitly not supported (see
    https://www.starlette.io/templates/). The lookup here is a pure
    attribute read off ``request.state`` -- the middleware already
    paid the DB round-trip on its way in, so the processor itself
    issues zero IO.
    """
    session_ctx = getattr(request.state, "ui_session", None)
    if session_ctx is None:
        return {"session_tenant": None}
    return {
        "session_tenant": {
            "id": str(session_ctx.tenant_id),
            "slug": session_ctx.tenant_slug,
            "name": session_ctx.tenant_name,
        },
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
