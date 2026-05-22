# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF (Backend-for-Frontend) auth for the operator console (stub).

Empty package for chassis Task #863. The order subsequent Tasks fill
this package in:

* T3 (#864) -- ``session.py``: ``web_session`` ORM model +
  ``SessionService`` with encrypted access-token / refresh-token
  custody, refresh-rotation per RFC 9700, Alembic migration.
* T4 (#865) -- ``flow.py`` + ``middleware.py``: OAuth 2.1
  Authorization Code + PKCE flow against Keycloak, ``meho-web``
  client registration, session-cookie middleware that loads operator
  identity on every ``/ui/*`` request and 302-redirects to
  ``/ui/auth/login`` when missing/expired.

This package is imported once T3 + T4 land; the stub keeps
``from meho_backplane.ui.auth import ...`` failing loudly while the
chassis is the only thing in place.
"""
