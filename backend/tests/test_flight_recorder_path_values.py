# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A trace address masks the path segments that hold a value of the call's parameters."""

from __future__ import annotations

import pytest

from meho_backplane.flight_recorder._capture_util import mask_path_values, path_values

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
