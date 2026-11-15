# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The broadcast page on a phone (#6000).

* Each feed row uses a 6-column grid below the ``sm`` width: time and
  agent, then the announcement text, then the details button and the
  targets. The phase and claim columns, and the column header, show
  from ``sm`` up.
* The two filters fold away behind a "Filters" button below ``md``, so
  the newest announcements sit at the top of the screen.
* The sidebar starts closed on a narrow screen, so a phone shows the page
  and not the menu.

The console has no browser test harness, so these tests pin the markup
and script that make the phone layout work. The look itself was checked
in a real browser at 390 px and 1366 px wide.
"""

from __future__ import annotations

import re

import respx

from tests.test_ui_broadcast_feed import (  # noqa: F401 -- the three fixtures are autouse
    _TENANT_A,
    _authenticated_client,
    _bff_env,
    _empty_backlog_prelude_by_default,
    _seed_session_sync,
    _ui_operator_role,
)


def _feed_page() -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    assert response.status_code == 200, response.text
    return response.text


def _row_template(body: str) -> str:
    """The server-authored event row inside the feed's ``x-for`` template."""
    feed = body.split('id="broadcast-feed"', 1)[1]
    return feed.split('<template x-for="ev in visibleEvents"', 1)[1].split("</li>", 1)[0]


def test_feed_rows_use_a_six_column_grid_on_a_phone() -> None:
    row = _row_template(_feed_page())

    li_class = re.search(r'<li\b[^>]*?\sclass="([^"]+)"', row)
    assert li_class is not None
    assert "grid-cols-6" in li_class.group(1)
    assert "sm:grid-cols-12" in li_class.group(1)
    # Agent on line one, announcement text on line two, targets on line three.
    assert row.count('class="col-span-4 truncate sm:col-span-2"') == 1
    assert row.count("col-span-6 truncate") == 1
    assert row.count('class="col-span-5 truncate text-xs opacity-80 sm:col-span-2"') == 1
    # Phase and claim cells show from sm up only.
    assert row.count('class="hidden sm:col-span-1 sm:block"') == 1
    assert row.count('class="hidden truncate text-xs sm:col-span-2 sm:block"') == 1


def test_announcement_rows_fold_out_their_details_on_a_phone() -> None:
    row = _row_template(_feed_page())

    # A tap on the row folds out the details.
    assert 'x-data="{ detailsOpen: false }"' in row
    assert 'x-on:click="detailsOpen = !detailsOpen"' in row
    announcement = row
    button = re.search(r'<button[^>]*aria-label="Announcement details"[^>]*>', announcement)
    assert button is not None
    assert "sm:hidden" in button.group(0)
    assert ":aria-expanded=" in button.group(0)
    assert 'x-on:click.stop="detailsOpen = !detailsOpen"' in button.group(0)
    details = announcement.split("data-announcement-details", 1)[0].rsplit("<div", 1)[1]
    assert 'class="col-span-6 space-y-1 text-xs sm:hidden"' in details
    assert 'x-show="detailsOpen"' in details
    body = announcement.split("data-announcement-details", 1)[1].split("</div>", 1)[0]
    # The phase, the full text wrapped (not cut), the targets and the claim
    # metadata; agent-authored text stays on x-text (escaped).
    assert "phaseBadgeClass(ev.phase)" in body
    assert "whitespace-pre-wrap" in body and 'x-text="ev.activity"' in body
    assert "truncate" not in body
    assert "announcementTargets(ev)" in body
    assert "announcementMeta(ev)" in body
    assert "x-html" not in announcement


def test_feed_column_header_shows_from_sm_up_only() -> None:
    body = _feed_page()

    header = re.search(
        r'<div\s+class="(hidden grid-cols-12[^"]*)"\s*>\s*<span class="col-span-2">Time', body
    )
    assert header is not None
    assert header.group(1).endswith("sm:grid")


def test_filters_fold_behind_a_button_on_a_phone() -> None:
    body = _feed_page()

    form = body.split('aria-label="Filter agent announcements"', 1)[0].rsplit("<form", 1)[1]
    assert 'x-data="{ filtersOpen: false }"' in form
    toggle = body.split('aria-controls="broadcast-filter-fields"', 1)[0].rsplit("<button", 1)[1]
    assert "md:hidden" in toggle
    fields = body.split('id="broadcast-filter-fields"', 1)[1].split(">", 1)[0]
    assert ":class=\"filtersOpen ? '' : 'max-md:hidden'\"" in fields
    # Without JavaScript the fields stay visible: no static hidden class.
    assert 'class="card-body grid gap-3 md:grid-cols-2"' in fields


def test_sidebar_starts_closed_on_a_narrow_screen() -> None:
    body = _feed_page()

    init = body.split("init() {", 1)[1].split("},", 1)[0]
    narrow_check = (
        "if (!window.matchMedia('(min-width: 64rem)').matches) "
        "{ this.sidebarOpen = false; return; }"
    )
    assert narrow_check in init
    # The saved choice is read only after the narrow-screen check.
    assert init.index(narrow_check) < init.index("localStorage.getItem('meho.sidebar')")
