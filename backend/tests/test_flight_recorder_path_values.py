# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A trace address masks the path segments that hold a value of the call's parameters."""

from __future__ import annotations

import httpx
import pytest

from meho_backplane.flight_recorder._capture_util import (
    encoded_safe_url,
    mask_path_values,
    path_values,
)

_ORIGIN = "https://svc.example.com"


def test_path_values_keeps_strings_and_whole_numbers() -> None:
    values = path_values({"name": "k1", "id": 7, "on": True, "empty": "", "spec": {"a": "b"}})
    assert values == frozenset({"k1", "7"})


def test_path_values_of_no_mapping_is_empty() -> None:
    assert path_values(None) == frozenset()


def test_a_segment_equal_to_a_value_is_masked() -> None:
    url = f"{_ORIGIN}/v1/keys/k1/versions"
    assert mask_path_values(url, frozenset({"k1"})) == f"{_ORIGIN}/v1/keys/[redacted]/versions"


def test_a_segment_is_compared_after_percent_decoding() -> None:
    url = f"{_ORIGIN}/v1/keys/a%20b"
    assert mask_path_values(url, frozenset({"a b"})) == f"{_ORIGIN}/v1/keys/[redacted]"


def test_a_long_value_inside_a_segment_is_masked() -> None:
    url = f"{_ORIGIN}/v1/keys/long-made-up-value:rotate"
    masked = mask_path_values(url, frozenset({"long-made-up-value"}))
    assert masked == f"{_ORIGIN}/v1/keys/[redacted]"


@pytest.mark.parametrize("value", ["v", "v1x", "keys"])
def test_fixed_segments_stay_when_no_segment_equals_a_short_value(value: str) -> None:
    url = f"{_ORIGIN}/v1/keyset/x"
    assert mask_path_values(url, frozenset({value})) == url


def test_an_address_without_a_path_stays() -> None:
    assert mask_path_values(_ORIGIN, frozenset({"k1"})) == _ORIGIN


def test_a_short_value_that_stands_alone_inside_a_segment_is_masked() -> None:
    url = f"{_ORIGIN}/v1/keys/ab:rotate"
    assert mask_path_values(url, frozenset({"ab"})) == f"{_ORIGIN}/v1/keys/[redacted]"


def test_a_value_with_a_slash_is_masked_in_its_encoded_segment() -> None:
    url = f"{_ORIGIN}/v1/keys/a%2Fb/versions"
    masked = mask_path_values(url, frozenset({"a/b"}))
    assert masked == f"{_ORIGIN}/v1/keys/[redacted]/versions"


def test_a_value_with_a_slash_is_masked_in_every_segment_it_covers() -> None:
    url = f"{_ORIGIN}/v1/a/b/versions"
    masked = mask_path_values(url, frozenset({"a/b"}))
    assert masked == f"{_ORIGIN}/v1/[redacted]/[redacted]/versions"


def test_the_template_masks_where_it_puts_a_parameter() -> None:
    url = f"{_ORIGIN}/v1/keys/ab:rotate"
    masked = mask_path_values(url, frozenset({"ab"}), "/v1/keys/{key}:rotate")
    assert masked == f"{_ORIGIN}/v1/keys/[redacted]"


def test_the_template_masks_a_value_with_an_encoded_slash() -> None:
    url = f"{_ORIGIN}/v1/keys/a%2Fb/versions"
    masked = mask_path_values(url, frozenset({"a/b"}), "/v1/keys/{key}/versions")
    assert masked == f"{_ORIGIN}/v1/keys/[redacted]/versions"


def test_the_template_keeps_a_fixed_segment_that_equals_a_value() -> None:
    url = f"{_ORIGIN}/v1/settings/proxy/7"
    masked = mask_path_values(url, frozenset({"proxy", "7"}), "/v1/settings/proxy/{id}")
    assert masked == f"{_ORIGIN}/v1/settings/proxy/[redacted]"


def test_the_template_allows_a_mount_prefix_and_a_trailing_slash() -> None:
    url = f"{_ORIGIN}/api/v1/keys/k1/"
    masked = mask_path_values(url, frozenset({"k1"}), "/v1/keys/{key}")
    assert masked == f"{_ORIGIN}/api/v1/keys/[redacted]/"


def test_a_reserved_placeholder_masks_every_segment_it_fills() -> None:
    url = f"{_ORIGIN}/kv/a/b/c"
    masked = mask_path_values(url, frozenset({"a/b/c"}), "/kv/{+path}")
    assert masked == f"{_ORIGIN}/kv/[redacted]/[redacted]/[redacted]"


def test_an_address_that_does_not_follow_the_template_is_masked_by_value() -> None:
    url = f"{_ORIGIN}/v2/other/k1"
    masked = mask_path_values(url, frozenset({"k1"}), "/v1/keys/{key}")
    assert masked == f"{_ORIGIN}/v2/other/[redacted]"


def test_encoded_safe_url_keeps_the_encoded_path_only() -> None:
    url = httpx.URL("https://user:pw@svc.example.com:8443/v1/keys/a%2Fb?ticket=t#frag")
    assert encoded_safe_url(url) == "https://svc.example.com:8443/v1/keys/a%2Fb"
