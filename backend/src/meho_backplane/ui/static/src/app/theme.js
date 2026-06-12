/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 evoila Group
 *
 * Theme bootstrap + toggle for the Operator Console.
 *
 * Loaded synchronously (NO `defer`) from _head_assets.html so the
 * stored theme lands on <html> before first paint — the no-FOUC
 * requirement is exactly why this cannot be a deferred script. It is
 * an external first-party file (not inline) to preserve the chassis
 * "zero inline script" CSP posture.
 *
 * Resolution order: explicit operator choice in localStorage, else the
 * OS preference (prefers-color-scheme), else the brand default (dark).
 *
 * `window.mehoToggleTheme()` is called by the theme toggles (Alpine
 * x-on:click). It flips the attribute, persists the choice, and emits
 * `meho-theme-changed` (hyphenated — Alpine's x-on listens to it
 * directly) so the other toggle instance can sync its icon and so
 * canvas-rendered surfaces (the Cytoscape topology graph reads its
 * palette from CSS custom properties at init) can re-read
 * theme-dependent colors without a reload.
 */
(function () {
  var KEY = "meho.theme";
  var DARK = "meho-dark";
  var LIGHT = "meho-light";

  var stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch (e) {
    /* storage may be blocked; fall through to OS preference */
  }
  var theme =
    stored === DARK || stored === LIGHT
      ? stored
      : window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
        ? LIGHT
        : DARK;
  document.documentElement.setAttribute("data-theme", theme);

  window.mehoToggleTheme = function () {
    var next =
      document.documentElement.getAttribute("data-theme") === DARK ? LIGHT : DARK;
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {
      /* non-persistent toggle is still a working toggle */
    }
    window.dispatchEvent(
      new CustomEvent("meho-theme-changed", { detail: { theme: next } })
    );
    return next;
  };
})();
