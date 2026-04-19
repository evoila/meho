# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Connector Helpers.

Utility functions for ARN parsing, tag normalization, and timestamp formatting
used across all AWS service handlers and serializers.
"""

from datetime import datetime
from typing import Any


def parse_arn(arn: str) -> dict[str, str]:
    """
    Parse an AWS ARN into its component parts.

    ARN format: arn:partition:service:region:account:resource

    Args:
        arn: The ARN string to parse.

    Returns:
        Dictionary with keys: partition, service, region, account, resource.

    Raises:
        ValueError: If the ARN format is invalid.
    """
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        raise ValueError(f"Invalid ARN format: {arn}")

    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def normalize_tags(tags: list[dict[str, str]] | None) -> dict[str, str]:
    """
    Convert boto3 tag format to a simple key-value dictionary.

    boto3 returns tags as: [{"Key": "Name", "Value": "my-instance"}, ...]
    This converts to: {"Name": "my-instance", ...}

    Args:
        tags: List of boto3-style tag dictionaries, or None.

    Returns:
        Dictionary mapping tag keys to tag values.
    """
    if not tags:
        return {}

    return {tag.get("Key", ""): tag.get("Value", "") for tag in tags}


def format_aws_timestamp(ts: Any) -> str | None:
    """
    Convert a datetime object (from boto3) to an ISO 8601 string.

    Args:
        ts: A datetime object, string, or None/falsy value.

    Returns:
        ISO 8601 formatted string, or None if input is None/falsy.
    """
    if not ts:
        return None

    if isinstance(ts, datetime):
        return ts.isoformat()

    if isinstance(ts, str):
        return ts

    return None
