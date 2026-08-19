# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source ingest-config resolution tests (#2881).

Covers defaults, overrides, defensive coercion of malformed operator-authored
``extras`` values, and the security-relevant clamps (body cap can only be
lowered; replay window is bounded above).
"""

from __future__ import annotations

from meho_backplane.events.ingest.config import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_REPLAY_WINDOW_SECONDS,
    DEFAULT_SIGNATURE_HEADER,
    MAX_REPLAY_WINDOW_SECONDS,
    resolve_ingest_config,
)


def test_defaults_on_empty_extras() -> None:
    cfg = resolve_ingest_config({})
    assert cfg.max_body_bytes == DEFAULT_MAX_BODY_BYTES
    assert cfg.replay_window_seconds == DEFAULT_REPLAY_WINDOW_SECONDS
    assert cfg.require_timestamp is True
    assert cfg.signature_header == DEFAULT_SIGNATURE_HEADER
    assert cfg.basic_username == ""
    assert cfg.rate_per_minute_override is None


def test_body_cap_override_can_only_lower() -> None:
    assert resolve_ingest_config({"max_body_bytes": 1024}).max_body_bytes == 1024
    # An override above the 256 KiB ceiling is clamped down, never widened.
    assert (
        resolve_ingest_config({"max_body_bytes": 10 * 1024 * 1024}).max_body_bytes
        == DEFAULT_MAX_BODY_BYTES
    )


def test_replay_window_clamped_to_max() -> None:
    assert resolve_ingest_config({"replay_window_seconds": 60}).replay_window_seconds == 60
    assert (
        resolve_ingest_config({"replay_window_seconds": 999_999}).replay_window_seconds
        == MAX_REPLAY_WINDOW_SECONDS
    )


def test_malformed_values_degrade_to_defaults() -> None:
    cfg = resolve_ingest_config(
        {"max_body_bytes": "huge", "replay_window_seconds": -5, "signature_header": ""}
    )
    assert cfg.max_body_bytes == DEFAULT_MAX_BODY_BYTES
    assert cfg.replay_window_seconds == DEFAULT_REPLAY_WINDOW_SECONDS
    assert cfg.signature_header == DEFAULT_SIGNATURE_HEADER


def test_rate_override_zero_disables_for_source() -> None:
    # 0 is a meaningful per-source "disable", distinct from an absent override.
    assert resolve_ingest_config({"rate_per_minute": 0}).rate_per_minute_override == 0
    assert resolve_ingest_config({"rate_per_minute": 5}).rate_per_minute_override == 5
    # bool is a subclass of int but never a valid cap.
    assert resolve_ingest_config({"rate_per_minute": True}).rate_per_minute_override is None
    assert resolve_ingest_config({"rate_per_minute": "9"}).rate_per_minute_override is None


def test_header_name_overrides() -> None:
    cfg = resolve_ingest_config(
        {
            "signature_header": "X-Grafana-Alerting-Signature",
            "timestamp_header": "X-When",
            "static_header": "X-Harbor-Secret",
            "delivery_id_header": "X-GitHub-Delivery",
            "basic_username": "am",
        }
    )
    assert cfg.signature_header == "X-Grafana-Alerting-Signature"
    assert cfg.timestamp_header == "X-When"
    assert cfg.static_header == "X-Harbor-Secret"
    assert cfg.delivery_id_header == "X-GitHub-Delivery"
    assert cfg.basic_username == "am"
