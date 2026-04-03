# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Atlassian Connector Module.

Shared infrastructure for Jira (Phase 42) and Confluence (Phase 43).
Provides AtlassianHTTPConnector base class with email:api_token Basic Auth,
bidirectional ADF-markdown converter, and custom field resolver.
"""

from meho_app.modules.connectors.atlassian.base import AtlassianHTTPConnector
from meho_app.modules.connectors.atlassian.field_resolver import FieldResolver

__all__ = ["AtlassianHTTPConnector", "FieldResolver"]
