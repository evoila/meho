# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Packaged Tier-1 redaction policies -- accessed via ``importlib.resources``.

This subpackage ships YAML policy fixtures as package data alongside
the loader so the packaged wheel can resolve them without a
bind-mount. See :mod:`meho_backplane.redaction.policy` for the loader
(:func:`~meho_backplane.redaction.policy.load_policy_yaml`).
"""
