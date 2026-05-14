# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Version 1 of the backplane HTTP API.

v0.1 ships two routes:

* :mod:`~meho_backplane.api.v1.auth_config` — public
  ``GET /api/v1/auth-config`` that returns the Keycloak realm issuer +
  audience so ``meho login`` can run the device-code flow against the
  right realm without operator flags. Unauthenticated by design — this
  is the OAuth metadata the CLI needs *before* it can auth.
* :mod:`~meho_backplane.api.v1.health` — authenticated
  ``GET /api/v1/health`` that exercises the entire federation chain
  (JWT validation → Vault OIDC login → secret read) and returns the
  operator identity plus dependency status to the CLI's
  ``meho status`` command.

v0.2 layers in:

* :mod:`~meho_backplane.api.v1.retrieve` — G0.4-T5 hybrid retrieval.
* :mod:`~meho_backplane.api.v1.targets` — G0.3-T3 targets CRUD.
* :mod:`~meho_backplane.api.v1.feed` — G6.1-T4 SSE broadcast feed.
* :mod:`~meho_backplane.api.v1.connectors` — G0.2-T6 generic dispatch.
* :mod:`~meho_backplane.api.v1.operations` — G0.6-T8 operation meta-tool
  surface (``/groups`` / ``/search`` / ``/call`` / ``/{descriptor_id}``).
"""
