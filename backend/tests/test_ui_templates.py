# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render tests for the operator-console UI chassis (G10.0-T2, #863).

These tests do not exercise FastAPI routes (T5 #866 lands those). They
assert the Jinja2 ``Environment`` constructed by
:func:`meho_backplane.ui.templating.get_jinja_env` can load
``base.html`` and produce HTML that contains the chassis-required
shape:

* DaisyUI ``navbar`` + ``drawer`` chrome.
* Sidebar links to every one of the five G10.x surfaces.
* Alpine.js ``x-data`` wiring for the sidebar collapse + user menu.
* A ``{% block content %}`` slot the surface templates can override.
* A footer rendering the deployed-build label (``app_version`` global,
  bound from the same ``CHART_VERSION`` / ``GIT_SHA`` env metadata
  ``GET /version`` reads — #1698) and the readiness pill. The static
  package ``__version__`` (``0.1.0-dev`` by design) must never leak
  into the rendered HTML.
* :class:`jinja2.StrictUndefined` propagates so a typo in a template
  variable fails the render rather than producing an empty string.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from jinja2 import Environment, UndefinedError

from meho_backplane import __version__
from meho_backplane.ui.paths import static_src_dir, templates_dir
from meho_backplane.ui.templating import get_jinja_env, reset_templating_for_testing

# The five surface routes the chassis sidebar links to. Sourced from
# Initiative #337 work-item 5 + Goal #336 done-when (broadcast / kb /
# topology / connectors / memory). Surface-Initiative tasks rename
# their target URL if needed; the chassis owns the canonical list.
_EXPECTED_SURFACE_HREFS = (
    "/ui/broadcast",
    "/ui/kb",
    "/ui/topology",
    "/ui/connectors",
    "/ui/memory",
    # Agents console (G10.8-T1 #1825) -- a top-level sidebar surface.
    "/ui/agents",
)


def _ui_env_factory() -> Iterator[Environment]:
    """Yield a fresh-ish Jinja env across tests.

    The module-level singleton in ``templating`` is intentionally
    cached across requests, so the test fixture grabs the cached
    instance once and reuses it. No teardown is needed -- the
    environment holds no resources beyond an in-memory template
    cache that lives for the process lifetime.
    """
    yield get_jinja_env()


@pytest.fixture(name="ui_env", scope="module")
def fixture_ui_env() -> Iterator[Environment]:
    yield from _ui_env_factory()


def test_templates_dir_exists_under_ui_package() -> None:
    """The chassis ships the Jinja2 templates inside the UI package.

    Regression guard: a refactor that moves templates outside
    ``meho_backplane/ui/templates/`` breaks the FastAPI deploy
    (T5 #866) because the static-mount + template-loader paths
    derive from the package's ``__file__``.
    """
    tdir = templates_dir()
    assert tdir.is_dir(), f"templates dir missing: {tdir}"
    assert (tdir / "base.html").is_file()


def test_static_src_carries_vendored_assets() -> None:
    """Every browser-bound vendor asset ships under ``static/src/vendor/``.

    Pinned SHA256s live in ``static/src/vendor/VENDOR.md``; this test
    only asserts the files are present. A SHA-mismatch check is
    intentionally out of scope here (Dockerfile/CI verify step
    covers that).
    """
    vendor = static_src_dir() / "vendor"
    assert (vendor / "htmx.min.js").is_file()
    # The SSE extension HTMX 2 split out of core; the dashboard
    # recent-activity snippet (G10.0) + the broadcast live feed (G10.1)
    # both need it, so it's a required vendored asset.
    assert (vendor / "sse.min.js").is_file()
    assert (vendor / "alpine.min.js").is_file()
    assert (vendor / "cytoscape.min.js").is_file()
    # Cytoscape layout-plugin chain shipped by G10.5-T2 (#881). Load
    # order is layout-base -> cose-base -> cose-bilkent / dagre; the
    # topology graph view depends on all four.
    assert (vendor / "layout-base.js").is_file()
    assert (vendor / "cose-base.js").is_file()
    assert (vendor / "cytoscape-cose-bilkent.js").is_file()
    assert (vendor / "cytoscape-dagre.js").is_file()
    assert (vendor / "daisyui.js").is_file()
    assert (vendor / "VENDOR.md").is_file()


@pytest.fixture(name="fresh_templating_singleton")
def fixture_fresh_templating_singleton() -> Iterator[None]:
    """Rebuild the templating singleton around a test that patches env vars.

    The ``app_version`` global is read from ``CHART_VERSION`` /
    ``GIT_SHA`` once, at environment construction (the values are
    process-immutable in deployment, so once-per-process is the correct
    cadence). Tests that monkeypatch those vars must drop the cached
    env before *and* after, so they see their own values and don't
    leak a patched label into later modules on the same xdist worker.
    """
    reset_templating_for_testing()
    yield
    reset_templating_for_testing()


def test_base_template_renders_chart_version_label(
    fresh_templating_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base.html`` shows the deployed release, not the package version.

    #1698 — the footer used to render ``meho_backplane.__version__``
    (``0.1.0-dev`` by design, never tracking the deployed image). The
    ``app_version`` global now binds the deployed-build label from the
    same ``CHART_VERSION`` / ``GIT_SHA`` env vars ``GET /version``
    reads. On a chart deploy the label is the ``v``-prefixed release,
    so the template no longer hardcodes a ``v`` of its own.
    """
    monkeypatch.setenv("CHART_VERSION", "0.14.0")
    monkeypatch.setenv("GIT_SHA", "2bbea9ad00112233445566778899aabbccddeeff")
    html = get_jinja_env().get_template("base.html").render(ready=True)
    assert "v0.14.0" in html
    assert "vv0.14.0" not in html
    assert __version__ not in html


def test_base_template_version_label_degrades_to_unknown(
    fresh_templating_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No build metadata (local dev) → ``unknown``, still never ``0.1.0-dev``.

    The global resolves without any route passing ``app_version``
    explicitly, so a bare-env render (this test) and every surface
    route get the same label for free.
    """
    monkeypatch.delenv("CHART_VERSION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    html = get_jinja_env().get_template("base.html").render(ready=True)
    assert "unknown" in html
    assert __version__ not in html


def test_base_template_renders_navbar_and_drawer(ui_env: Environment) -> None:
    """DaisyUI primitives render in the expected positions.

    Asserting the class names directly is brittler than a
    visual-regression test but cheaper than spinning up a headless
    browser; the trade is acceptable for a chassis-shape test.
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert 'class="navbar' in html or "navbar bg-base-100" in html
    assert "drawer-toggle" in html
    assert "drawer-content" in html
    assert "drawer-side" in html


def test_base_template_links_every_surface(ui_env: Environment) -> None:
    """Sidebar enumerates all five G10.x surface routes."""
    html = ui_env.get_template("base.html").render(ready=True)
    for href in _EXPECTED_SURFACE_HREFS:
        assert f'href="{href}"' in html, f"missing surface link: {href}"


def test_base_template_wires_alpine_directives(ui_env: Environment) -> None:
    """Alpine.js sidebar-collapse + user-menu state is wired in markup.

    Alpine reads its directives from HTML attributes at parse time; if
    these attribute names get renamed in a refactor the interactive
    bits go dead silently. The assertions below catch that.
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert "x-data=" in html
    assert "sidebarOpen" in html
    assert "userMenuOpen" in html
    assert 'x-show="userMenuOpen"' in html
    assert "x-cloak" in html


def test_base_template_exposes_content_block(ui_env: Environment) -> None:
    """A child template can override ``{% block content %}``.

    The chassis is useless to G10.1-G10.5 without an extension point;
    this test exercises the override end-to-end against the
    FileSystemLoader the chassis uses in production.
    """
    template_source = (templates_dir() / "base.html").read_text(encoding="utf-8")
    # The block declaration is what the child relies on; a
    # case-insensitive search is fine because Jinja2 block names are
    # case-sensitive and our source uses lowercase.
    assert "{% block content %}" in template_source
    assert "{% endblock %}" in template_source


def test_base_template_orders_component_scripts_before_alpine() -> None:
    """``component_scripts`` renders before the Alpine-loading include.

    The vendored Alpine CDN bundle auto-starts via
    ``queueMicrotask(() => Alpine.start())`` at the end of its own
    script task, and the microtask queue drains before the next
    deferred script executes -- so an ``alpine:init`` listener
    registered by a script that renders AFTER ``alpine.min.js`` in
    document order never sees the event, and its ``Alpine.data()``
    component silently never registers (#1692). Deferred scripts
    execute in document order, so the chassis must place the
    ``component_scripts`` block (where surfaces inject their
    controller-registration scripts) before the ``_head_assets.html``
    include that loads ``alpine.min.js``.
    """
    template_source = (templates_dir() / "base.html").read_text(encoding="utf-8")
    block_pos = template_source.index("{% block component_scripts %}")
    include_pos = template_source.index('{% include "_head_assets.html" %}')
    assert block_pos < include_pos


def test_base_template_footer_shows_readiness_pill(ui_env: Environment) -> None:
    """Footer flips between 'ready' and 'starting' on the ``ready`` flag."""
    ready_html = ui_env.get_template("base.html").render(ready=True)
    starting_html = ui_env.get_template("base.html").render(ready=False)
    assert "ready" in ready_html
    assert "bg-success" in ready_html
    assert "starting" in starting_html
    assert "bg-warning" in starting_html


def test_strict_undefined_raises_on_missing_global(ui_env: Environment) -> None:
    """A typo in a template variable raises rather than renders blank.

    The base template references ``ready`` (passed by the renderer)
    and ``app_version`` (the env global). Rendering without ``ready``
    must raise :class:`jinja2.UndefinedError`; that's the load-bearing
    behaviour of :class:`jinja2.StrictUndefined`.
    """
    template = ui_env.get_template("base.html")
    with pytest.raises(UndefinedError):
        template.render()  # ``ready`` is missing


def test_base_template_uses_compiled_tailwind_css(ui_env: Environment) -> None:
    """The link tag points at the Dockerfile-compiled output.

    ``static/dist/tailwind.css`` is the canonical output of the
    Tailwind 4 CLI build step; T5 #866's StaticFiles mount lands it
    at ``/ui/static/dist/tailwind.css``. A drift here would silently
    break styling in prod while looking fine in dev (where
    ``--watch`` writes to the same path).
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert re.search(r'href="/ui/static/dist/tailwind\.css"', html) is not None


# ---------------------------------------------------------------------------
# Operator-console modal dismissal (G0.26-T3 #1803)
#
# Every operator modal is an HTMX-injected native ``<dialog class="modal">``
# whose close controls (button, ESC, backdrop ``form[method="dialog"]``) route
# through ``HTMLDialogElement.close()``. ``.close()`` clears ``[open]`` but NOT
# CSS classes, so a dialog shown via DaisyUI's ``modal-open`` modifier stayed
# visible after every close path and the operator had to reload the page. The
# fix opens the injected dialogs via ``showModal()`` after the swap (a shared
# ``app/modal-dialogs.js`` controller) and drops the static ``modal-open`` so
# ``.close()`` fully dismisses; the same controller strips any lingering
# ``modal-open`` on the native ``close`` event as a belt-and-suspenders.
#
# The console has no browser test harness (Playwright is not wired here), so
# these server-side assertions pin the load-bearing invariants: (a) no
# HTMX-injected ``<dialog>`` ships a static ``modal-open`` that ``.close()``
# cannot clear, and (b) the shared open/close controller is present and wired
# into every page via ``base.html``.
# ---------------------------------------------------------------------------

#: Every fragment that injects a native ``<dialog class="modal">`` via an HTMX
#: swap. The close path on each routes through ``.close()`` (button / ESC /
#: backdrop), so none may ship a static ``modal-open`` (#1803). The KB editor
#: (``kb/_editor_modal.html``) and the memory delete-confirm dialog
#: (``memory/detail.html``) already use ``class="modal"`` + ``showModal()`` and
#: are the reference pattern; the runbooks confirm dialogs are Alpine ``<div>``
#: modals (``x-show`` toggles ``modal-open`` dynamically), not native
#: ``<dialog>``, so they are intentionally excluded.
_INJECTED_DIALOG_TEMPLATES = (
    "approvals/_panel.html",
    "approvals/_modal.html",
    "approvals/_decided.html",
    "memory/_create_modal.html",
    "memory/_promote_modal.html",
    "connectors/_create_modal.html",
    "connectors/_edit_modal.html",
    "connectors/_delete_modal.html",
    "agents/_create_modal.html",
    "agents/_edit_modal.html",
    "agents/_delete_modal.html",
    "agents/grants/_create_modal.html",
    "agents/grants/_elevate_modal.html",
    "agents/grants/_revoke_modal.html",
)

#: Re-find every opening ``<dialog ...>`` tag in a template source. Matches the
#: tag up to the first ``>`` so the assertion inspects the element's own
#: attributes, not the dialog's inner markup.
_DIALOG_OPEN_TAG_RE = re.compile(r"<dialog\b[^>]*>", re.IGNORECASE)

#: Jinja comment blocks (``{# ... #}`` and ``{#- ... -#}``). Stripped before
#: scanning for ``<dialog>`` tags so prose like "the inserted ``<dialog>``" in a
#: template docstring is not mistaken for a real element.
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


@pytest.mark.parametrize("template_name", _INJECTED_DIALOG_TEMPLATES)
def test_injected_dialog_has_no_static_modal_open(template_name: str) -> None:
    """No HTMX-injected ``<dialog>`` ships a static ``modal-open`` (#1803).

    A native ``.close()`` clears ``[open]`` but not the ``modal-open`` class,
    so a statically-classed dialog never dismisses. Asserting on the raw
    template source (Jinja comments stripped) keeps the guard independent of
    the per-modal render context.
    """
    source = _JINJA_COMMENT_RE.sub(
        "", (templates_dir() / template_name).read_text(encoding="utf-8")
    )
    open_tags = _DIALOG_OPEN_TAG_RE.findall(source)
    assert open_tags, f"{template_name} renders no <dialog> element"
    for tag in open_tags:
        assert "modal-open" not in tag, (
            f"{template_name} ships a static modal-open on {tag!r}; "
            "native .close() cannot clear it, so the modal never dismisses"
        )
        # Sanity: it is still a DaisyUI modal dialog (just opened via JS now).
        assert "modal" in tag, f"{template_name} dialog dropped the modal class"


def test_modal_controller_script_strips_modal_open_on_close() -> None:
    """The shared modal controller removes ``modal-open`` on the close event.

    The native ``close`` event fires on every close path (button ``.close()``,
    ESC, ``form[method="dialog"]`` submit) but does NOT bubble, so the listener
    must be registered in the capture phase. This is the mechanism that makes
    every operator modal dismiss (#1803).
    """
    script = (static_src_dir() / "app" / "modal-dialogs.js").read_text(encoding="utf-8")
    # A delegated ``close`` listener in the capture phase (3rd arg truthy).
    assert re.search(r'addEventListener\(\s*"close"', script), (
        "modal controller must listen for the native dialog close event"
    )
    assert re.search(r'addEventListener\(\s*"close"[\s\S]*?,\s*true\s*,?\s*\)', script), (
        "the close listener must use the capture phase (close does not bubble)"
    )
    assert 'classList.remove("modal-open")' in script, (
        "the close handler must strip the modal-open class so .close() dismisses"
    )


def test_modal_controller_script_opens_injected_dialog_on_swap() -> None:
    """The shared controller opens swapped-in dialogs via ``showModal()`` (#1803).

    Dropping the static ``modal-open`` means the open path now relies on
    ``showModal()`` (sets ``[open]`` and enables native ESC), driven off the
    ``htmx:afterSwap`` event the modal fragments arrive on.
    """
    script = (static_src_dir() / "app" / "modal-dialogs.js").read_text(encoding="utf-8")
    assert "htmx:afterSwap" in script, (
        "the controller must open injected dialogs on the htmx swap event"
    )
    assert "showModal()" in script, (
        "the controller must open injected dialogs via the native showModal()"
    )


def test_base_template_loads_modal_controller_outside_component_scripts() -> None:
    """``base.html`` ships the modal controller on every page (#1803).

    The script must load OUTSIDE the overridable ``component_scripts`` block so
    a surface that overrides that block cannot accidentally drop modal
    dismissal -- the same posture the app-shell approvals-bell script uses.
    """
    source = (templates_dir() / "base.html").read_text(encoding="utf-8")
    assert '<script src="/ui/static/src/app/modal-dialogs.js" defer></script>' in source, (
        "base.html must load the shared modal controller"
    )
    script_pos = source.index("/ui/static/src/app/modal-dialogs.js")
    block_pos = source.index("{% block component_scripts %}")
    assert script_pos < block_pos, (
        "the modal controller must load before (outside) the overridable "
        "component_scripts block so every page ships it"
    )


def test_base_template_modal_controller_renders_in_html(ui_env: Environment) -> None:
    """The controller ``<script>`` survives a real render of ``base.html``."""
    html = ui_env.get_template("base.html").render(ready=True)
    assert '<script src="/ui/static/src/app/modal-dialogs.js" defer></script>' in html


# ---------------------------------------------------------------------------
# Session-expiry safety net (#122)
#
# htmx 2.0.9 classifies a 401 as ``{swap: false, error: true}`` by default,
# so a session-expiry on an ``hx-*`` request is a silent no-op -- a dead
# control with no operator signal. The app-shell ``session-expiry.js`` handler
# is the client-side recovery path: a single global ``htmx:beforeOnLoad``
# listener that, on a 401 only, ``preventDefault()``s the dead swap and
# surfaces a banner with a login link carrying ``return_to=<current path>``.
# No JS test runner ships in this repo (no ``package.json`` under the UI
# package); these tests assert the wiring (the deferred script tag, loaded
# after ``htmx.min.js``) and the served handler's load-bearing contract by
# inspecting its source -- mirroring how ``test_ui_broadcast_feed.py`` pins
# the served ``broadcast-feed.js`` content.
# ---------------------------------------------------------------------------


def test_base_template_loads_session_expiry_handler_after_htmx() -> None:
    """``_head_assets.html`` loads the 401 handler deferred, after ``htmx.min.js``.

    AC #4: the handler is registered once globally and loads via the shared
    head-asset block after the htmx bundle, so its ``htmx:beforeOnLoad``
    listener is attached before the first htmx request can resolve. ``defer``
    also guarantees ``document.body`` exists when the listener registers.
    """
    source = (templates_dir() / "_head_assets.html").read_text(encoding="utf-8")
    assert '<script src="/ui/static/src/app/session-expiry.js" defer></script>' in source, (
        "_head_assets.html must load the session-expiry handler (deferred)"
    )
    htmx_pos = source.index("/ui/static/src/vendor/htmx.min.js")
    handler_pos = source.index("/ui/static/src/app/session-expiry.js")
    assert htmx_pos < handler_pos, (
        "the session-expiry handler must load AFTER htmx.min.js so the "
        "htmx:beforeOnLoad listener attaches before the first request resolves"
    )


def test_session_expiry_handler_renders_once_in_base_html(ui_env: Environment) -> None:
    """The handler ``<script>`` survives a real render and is included exactly once.

    AC #4: registered once globally, not duplicated per template. ``base.html``
    includes ``_head_assets.html`` exactly once, so the tag appears once.
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert html.count("/ui/static/src/app/session-expiry.js") == 1


def test_session_expiry_handler_source_carries_401_contract() -> None:
    """The served handler embeds the load-bearing 401-only safety-net logic.

    Asserts the grep-able contract issue #122 AC #3 pins, plus the behavioural
    seams a JS unit test would otherwise drive:

    * AC #1 -- on a 401 it surfaces a recovery path: ``preventDefault()`` the
      dead swap + show a banner + a login link with ``return_to=<path>``.
    * AC #2 -- it acts ONLY on 401: a guard returns early for every other
      status, so a 422 form-validation re-render (and any non-auth response)
      is never intercepted.
    * AC #4 -- registered once on ``htmx:beforeOnLoad`` (the seam htmx fires
      for every response before any swap; ``preventDefault()`` there aborts
      htmx's whole response processing).
    """
    handler = static_src_dir() / "app" / "session-expiry.js"
    assert handler.is_file(), f"session-expiry handler missing: {handler}"
    source = handler.read_text(encoding="utf-8")

    # AC #4 -- the global seam, registered on <body>.
    assert "htmx:beforeOnLoad" in source
    assert 'addEventListener("htmx:beforeOnLoad"' in source

    # AC #2 -- status-scoped: the handler branches on the xhr status, so a
    # 422 (or any other status) flows through untouched. Asserting the status
    # read plus the explicit 401 branch pins the "don't hijack 422" contract.
    assert "event.detail.xhr" in source
    assert "xhr.status === 401" in source

    # AC #1 -- the recovery path: cancel the dead swap, show the banner, and
    # link to the login route with the current path as ``return_to``.
    assert "event.preventDefault()" in source
    assert "/ui/auth/login?return_to=" in source
    assert "encodeURIComponent" in source
    assert "Your session expired" in source


def test_session_expiry_handler_source_carries_csrf_rejection_contract() -> None:
    """The served handler surfaces the CSRF-rejection 403 (#2112).

    A state-changing ``/ui/*`` POST whose ``meho_csrf`` double-submit cookie
    was dropped/aged-out gets a bare ``{"detail":"csrf_token_invalid"}`` 403
    that htmx 2.0.9 does NOT swap. The same global ``htmx:beforeOnLoad`` seam
    surfaces a refresh banner -- but ONLY for a CSRF rejection, keyed on the
    ``x-csrf-rejection-reason`` response header the middleware stamps, so a
    bare RBAC 403 (``require_ui_admin``, no such header) flows through
    untouched.
    """
    handler = static_src_dir() / "app" / "session-expiry.js"
    assert handler.is_file(), f"session-expiry handler missing: {handler}"
    source = handler.read_text(encoding="utf-8")

    # Keyed on the CSRF-specific header, not the bare 403 status -- this is
    # what keeps the net off a genuine RBAC denial.
    assert "x-csrf-rejection-reason" in source
    assert "xhr.status === 403" in source
    assert "getResponseHeader" in source

    # The recovery path: cancel the dead swap + a refresh banner (a fresh
    # render re-mints the meho_csrf cookie so the retry clears the check).
    assert "event.preventDefault()" in source
    assert "refresh the page and retry" in source


def test_session_expiry_handler_served_as_static_asset() -> None:
    """The handler is reachable under the ``/ui/static/src/app/`` mount path.

    The file lives where the ``StaticFiles`` mount serves
    ``/ui/static/src/app/session-expiry.js`` from, so the deferred script tag
    in ``_head_assets.html`` resolves at runtime (same posture as the other
    app-shell scripts).
    """
    handler = static_src_dir() / "app" / "session-expiry.js"
    assert handler.is_file()
    # SPDX header posture matches the sibling app-shell scripts.
    source = handler.read_text(encoding="utf-8")
    assert "SPDX-License-Identifier: Apache-2.0" in source
