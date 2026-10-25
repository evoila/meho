# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared paging for the fakes of the connector's VI-JSON seam (#4862).

vCenter may split a ``RetrievePropertiesEx`` answer into pages: a
``RetrieveResult`` with some objects and a ``token``. The caller then sends
``ContinueRetrievePropertiesEx`` with that token, on the session of the first
call, until a page comes back without a token.

:class:`PagedVim` is a mixin for a fake ``_post_vmomi_json``. It splits a
many-object answer into pages of ``page_size`` objects, gives each page a
token that works once, and records each continuation call with the session
token it was sent with. The fakes of the alarm, teardown and other vSphere
tests mix it in, so every test of a paged read uses one fake. All ids and
names in the tests that use it are made up.
"""

from __future__ import annotations

from typing import Any


class PagedVim:
    """Mixin: answer a many-object read page by page behind single-use tokens.

    Knobs a test sets:

    * ``page_size`` -- at most this many objects per page (``None``: one page);
    * ``endless_pages`` -- every page carries a new token (an answer that
      never ends);
    * ``fail_continue`` -- a ``ContinueRetrievePropertiesEx`` call raises this;
    * ``session`` -- the session token the fake reports to ``session_used``.

    What a test reads back: ``continue_calls``, the ``(token, session_token)``
    of every continuation call, and ``_pending``, the pages still behind a
    token (empty when every page was read).
    """

    def __init__(self) -> None:
        self.page_size: int | None = None
        self.endless_pages = False
        self.fail_continue: Exception | None = None
        self.session = "<session-1>"
        self.continue_calls: list[tuple[str, str | None]] = []
        self._pending: dict[str, list[list[dict[str, Any]]]] = {}
        self._tokens = 0

    def _record_session(self, session_used: Any) -> None:
        """Report the session the call used, the way the connector does."""
        if session_used is not None:
            session_used.token = self.session

    def _serve_objects(self, objects: list[dict[str, Any]]) -> dict[str, Any]:
        """Split *objects* into pages of ``page_size`` and answer the first page."""
        size = self.page_size or max(len(objects), 1)
        return self._serve([objects[i : i + size] for i in range(0, len(objects), size)] or [[]])

    def _serve(self, pages: list[list[dict[str, Any]]]) -> dict[str, Any]:
        """Answer the first page; keep the rest behind a single-use token."""
        first, rest = pages[0], pages[1:]
        result: dict[str, Any] = {"objects": first}
        if rest or self.endless_pages:
            self._tokens += 1
            token = f"<token-{self._tokens}>"
            self._pending[token] = rest or [[]]
            result["token"] = token
        return result

    def _continue(self, token: str, session_token: str | None) -> dict[str, Any]:
        """Answer a ``ContinueRetrievePropertiesEx`` call: the next page for *token*."""
        self.continue_calls.append((token, session_token))
        if self.fail_continue is not None:
            raise self.fail_continue
        pages = self._pending.pop(token)  # a token works once
        return self._serve(pages)
