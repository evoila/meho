# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test the custom assertions.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.assertions import (
    assert_all_keys_present,
    assert_datetime_recent,
    assert_dict_contains,
    assert_json_serializable,
    assert_list_contains_items,
    assert_no_duplicates,
    assert_valid_email,
    assert_valid_url,
    assert_valid_uuid,
)


@pytest.mark.unit
def test_assert_valid_uuid_with_valid_uuid():
    """Test assert_valid_uuid passes for valid UUID"""
    assert_valid_uuid("550e8400-e29b-41d4-a716-446655440000")
    # Should not raise


@pytest.mark.unit
def test_assert_valid_uuid_with_invalid_uuid():
    """Test assert_valid_uuid fails for invalid UUID"""
    with pytest.raises(AssertionError, match="not a valid UUID"):
        assert_valid_uuid("not-a-uuid")


@pytest.mark.unit
def test_assert_datetime_recent_with_recent_datetime():
    """Test assert_datetime_recent passes for recent datetime"""
    now = datetime.now(UTC)
    assert_datetime_recent(now, seconds=5)
    # Should not raise


@pytest.mark.unit
def test_assert_datetime_recent_with_old_datetime():
    """Test assert_datetime_recent fails for old datetime"""
    old = datetime.now(UTC) - timedelta(seconds=10)

    with pytest.raises(AssertionError, match="more than 5 seconds old"):
        assert_datetime_recent(old, seconds=5)


@pytest.mark.unit
def test_assert_datetime_recent_with_future_datetime():
    """Test assert_datetime_recent fails for future datetime"""
    future = datetime.now(UTC) + timedelta(seconds=10)

    with pytest.raises(AssertionError, match="in the future"):
        assert_datetime_recent(future, seconds=5)


@pytest.mark.unit
def test_assert_dict_contains_success():
    """Test assert_dict_contains passes when dict contains expected keys"""
    actual = {"a": 1, "b": 2, "c": 3}
    expected = {"a": 1, "b": 2}

    assert_dict_contains(actual, expected)
    # Should not raise


@pytest.mark.unit
def test_assert_dict_contains_missing_key():
    """Test assert_dict_contains fails when key is missing"""
    actual = {"a": 1}
    expected = {"a": 1, "b": 2}

    with pytest.raises(AssertionError, match="Missing key 'b'"):
        assert_dict_contains(actual, expected)


@pytest.mark.unit
def test_assert_dict_contains_wrong_value():
    """Test assert_dict_contains fails when value doesn't match"""
    actual = {"a": 1, "b": 99}
    expected = {"a": 1, "b": 2}

    with pytest.raises(AssertionError, match="has value '99' but expected '2'"):
        assert_dict_contains(actual, expected)


@pytest.mark.unit
def test_assert_list_contains_items_success():
    """Test assert_list_contains_items passes when all items present"""
    actual = [1, 2, 3, 4, 5]
    expected = [1, 3, 5]

    assert_list_contains_items(actual, expected)
    # Should not raise


@pytest.mark.unit
def test_assert_list_contains_items_missing():
    """Test assert_list_contains_items fails when items missing"""
    actual = [1, 2, 3]
    expected = [1, 4, 5]

    with pytest.raises(AssertionError, match="Missing items"):
        assert_list_contains_items(actual, expected)


@pytest.mark.unit
def test_assert_no_duplicates_success():
    """Test assert_no_duplicates passes for unique list"""
    items = [1, 2, 3, 4, 5]

    assert_no_duplicates(items)
    # Should not raise


@pytest.mark.unit
def test_assert_no_duplicates_fails():
    """Test assert_no_duplicates fails for list with duplicates"""
    items = [1, 2, 3, 2, 4]

    with pytest.raises(AssertionError, match="Found duplicates"):
        assert_no_duplicates(items)


@pytest.mark.unit
def test_assert_all_keys_present_success():
    """Test assert_all_keys_present passes when all keys present"""
    actual = {"a": 1, "b": 2, "c": 3}
    required = ["a", "b"]

    assert_all_keys_present(actual, required)
    # Should not raise


@pytest.mark.unit
def test_assert_all_keys_present_fails():
    """Test assert_all_keys_present fails when keys missing"""
    actual = {"a": 1}
    required = ["a", "b", "c"]

    with pytest.raises(AssertionError, match="Missing required keys"):
        assert_all_keys_present(actual, required)


@pytest.mark.unit
def test_assert_valid_email_success():
    """Test assert_valid_email passes for valid email"""
    assert_valid_email("user@example.com")
    assert_valid_email("test.user+tag@sub.domain.com")
    # Should not raise


@pytest.mark.unit
def test_assert_valid_email_fails():
    """Test assert_valid_email fails for invalid email"""
    with pytest.raises(AssertionError, match="not a valid email"):
        assert_valid_email("not-an-email")


@pytest.mark.unit
def test_assert_valid_url_success():
    """Test assert_valid_url passes for valid URL"""
    assert_valid_url("https://example.com")
    assert_valid_url("http://api.example.com/path/to/resource")
    # Should not raise


@pytest.mark.unit
def test_assert_valid_url_fails():
    """Test assert_valid_url fails for invalid URL"""
    with pytest.raises(AssertionError, match="not a valid URL"):
        assert_valid_url("not a url")


@pytest.mark.unit
def test_assert_json_serializable_success():
    """Test assert_json_serializable passes for serializable objects"""
    assert_json_serializable({"a": 1, "b": "test"})
    assert_json_serializable([1, 2, 3])
    assert_json_serializable("string")
    # Should not raise


@pytest.mark.unit
def test_assert_json_serializable_fails():
    """Test assert_json_serializable fails for non-serializable objects"""

    class NonSerializable:
        pass

    with pytest.raises(AssertionError, match="not JSON serializable"):
        assert_json_serializable(NonSerializable())
