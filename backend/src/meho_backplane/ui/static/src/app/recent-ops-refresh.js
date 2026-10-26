// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Recent-operations refresh for the dashboard tray and the connector
// detail card (#3758).
//
// Both cards are rendered on the server from the audit log and carry
// ``data-recent-ops-refresh`` plus ``data-refresh-url``. Every POLL_MS
// while the page is visible, this script asks htmx to fetch the card's
// URL and swap the response in place of the card (``outerHTML``). The
// fresh card brings its own "Updated" time. When the tab becomes visible
// again, the cards refresh at once. A hidden tab sends no requests, and a
// card never has two refreshes in flight.
//
// If a refresh fails, the card keeps its rows and its status line says
// the refresh failed; the next tick tries again.
//
// Why a plain timer and not ``hx-trigger="every 5s [filter]"``: htmx
// evaluates trigger filters with ``eval``, which the console's CSP does
// not allow. Loaded with ``defer`` from the page's ``scripts`` block.

(() => {
  const POLL_MS = 5000;
  const inFlight = new Set();

  function markFailed(card) {
    const status = card.querySelector("[data-refresh-status]");
    if (status) {
      status.textContent = "Refresh failed. Showing older data; retrying.";
    }
  }

  function refreshCard(card) {
    const url = card.dataset.refreshUrl;
    if (!url || inFlight.has(url)) {
      return;
    }
    inFlight.add(url);
    htmx
      .ajax("GET", url, { source: card, target: card, swap: "outerHTML" })
      .catch(() => markFailed(card))
      .finally(() => inFlight.delete(url));
  }

  function refreshAll() {
    if (document.visibilityState !== "visible") {
      return;
    }
    document.querySelectorAll("[data-recent-ops-refresh]").forEach(refreshCard);
  }

  // htmx does not swap an error response (or a failed send); say so on
  // the card instead.
  function onRefreshError(event) {
    const card = event.detail && event.detail.target;
    if (card && card.hasAttribute && card.hasAttribute("data-recent-ops-refresh")) {
      markFailed(card);
    }
  }
  document.body.addEventListener("htmx:responseError", onRefreshError);
  document.body.addEventListener("htmx:sendError", onRefreshError);

  window.setInterval(refreshAll, POLL_MS);
  document.addEventListener("visibilitychange", refreshAll);
})();
