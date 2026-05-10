# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Version 1 of the backplane HTTP API.

v0.1 ships a single authenticated route — :mod:`~meho_backplane.api.v1.health`
— that exercises the entire federation chain (JWT validation → Vault
OIDC login → secret read) and returns the operator identity plus
dependency status to the CLI's ``meho status`` command.
"""
