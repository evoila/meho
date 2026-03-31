# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Custom assertions for testing.

Usage:
    from tests.support.assertions import assert_valid_uuid

    assert_valid_uuid(some_id, "User ID should be valid UUID")
"""

import uuid as uuid_module
from datetime import UTC, datetime, timedelta
from typing import Any


def assert_valid_uuid(value: str, msg: str = "") -> None:
    """
    Assert that value is a valid UUID.

    Args:
        value: String to check
        msg: Custom error message

    Raises:
        AssertionError: If value is not a valid UUID
    """
    try:
        uuid_module.UUID(value)
    except (ValueError, AttributeError, TypeError) as e:
        error_msg = f"'{value}' is not a valid UUID"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg) from e


def assert_datetime_recent(dt: Any, seconds: int = 5, msg: str = "") -> None:
    """
    Assert that datetime is within recent seconds.

    Args:
        dt: Datetime to check
        seconds: Maximum age in seconds
        msg: Custom error message

    Raises:
        AssertionError: If datetime is too old or in the future
    """
    if not isinstance(dt, datetime):
        error_msg = f"Expected datetime, got {type(dt)}"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)

    # Handle both naive and timezone-aware datetimes
    if dt.tzinfo is None:  # noqa: SIM108 -- readability preferred over ternary
        # Naive datetime (e.g., from PostgreSQL TIMESTAMP)
        now = datetime.now(tz=UTC)
    else:
        # Timezone-aware datetime
        now = datetime.now(UTC)

    # Allow 1 second clock skew for future times
    if dt > now + timedelta(seconds=1):
        error_msg = f"Datetime {dt} is in the future (now={now})"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)

    # Check if too old
    if dt < now - timedelta(seconds=seconds):
        error_msg = f"Datetime {dt} is more than {seconds} seconds old (now={now})"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_dict_contains(actual: dict, expected: dict, msg: str = "") -> None:
    """
    Assert that actual dict contains all keys from expected with same values.

    Args:
        actual: Actual dictionary
        expected: Expected key-value pairs
        msg: Custom error message

    Raises:
        AssertionError: If keys are missing or values don't match
    """
    for key, value in expected.items():
        if key not in actual:
            error_msg = f"Missing key '{key}' in actual dict"
            if msg:
                error_msg = f"{msg}: {error_msg}"
            raise AssertionError(error_msg)

        if actual[key] != value:
            error_msg = f"Key '{key}' has value '{actual[key]}' but expected '{value}'"
            if msg:
                error_msg = f"{msg}: {error_msg}"
            raise AssertionError(error_msg)


def assert_list_contains_items(actual: list, expected_items: list, msg: str = "") -> None:
    """
    Assert that actual list contains all expected items.

    Args:
        actual: Actual list
        expected_items: Items that should be in the list
        msg: Custom error message

    Raises:
        AssertionError: If any expected item is missing
    """
    missing_items = []
    for item in expected_items:
        if item not in actual:
            missing_items.append(item)

    if missing_items:
        error_msg = f"Missing items in actual list: {missing_items}"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_no_duplicates(items: list, msg: str = "") -> None:
    """
    Assert that list contains no duplicates.

    Args:
        items: List to check
        msg: Custom error message

    Raises:
        AssertionError: If duplicates found
    """
    seen = set()
    duplicates = []

    for item in items:
        # Handle unhashable types
        try:
            if item in seen:
                duplicates.append(item)
            seen.add(item)
        except TypeError:
            # Skip unhashable items
            pass

    if duplicates:
        error_msg = f"Found duplicates: {duplicates}"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_all_keys_present(actual: dict, required_keys: list[str], msg: str = "") -> None:
    """
    Assert that all required keys are present in dict.

    Args:
        actual: Dictionary to check
        required_keys: Keys that must be present
        msg: Custom error message

    Raises:
        AssertionError: If any required key is missing
    """
    missing_keys = []
    for key in required_keys:
        if key not in actual:
            missing_keys.append(key)

    if missing_keys:
        error_msg = f"Missing required keys: {missing_keys}"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_valid_email(email: str, msg: str = "") -> None:
    """
    Assert that string is a valid email address.

    Args:
        email: String to check
        msg: Custom error message

    Raises:
        AssertionError: If email is invalid
    """
    import re

    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

    if not re.match(pattern, email):
        error_msg = f"'{email}' is not a valid email address"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_valid_url(url: str, msg: str = "") -> None:
    """
    Assert that string is a valid URL.

    Args:
        url: String to check
        msg: Custom error message

    Raises:
        AssertionError: If URL is invalid
    """
    import re

    pattern = r"^https?://[^\s/$.?#].[^\s]*$"

    if not re.match(pattern, url):
        error_msg = f"'{url}' is not a valid URL"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg)


def assert_json_serializable(obj: Any, msg: str = "") -> None:
    """
    Assert that object is JSON serializable.

    Args:
        obj: Object to check
        msg: Custom error message

    Raises:
        AssertionError: If object cannot be JSON serialized
    """
    import json

    try:
        json.dumps(obj)
    except (TypeError, ValueError) as e:
        error_msg = f"Object is not JSON serializable: {e!s}"
        if msg:
            error_msg = f"{msg}: {error_msg}"
        raise AssertionError(error_msg) from e
