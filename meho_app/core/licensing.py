# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
License key validation and edition gating for MEHO open-core.

Ed25519-signed license keys determine whether the application runs in
community or enterprise mode. The LicenseService singleton is initialized
once at startup and exposes edition status for router-level gating.

Import direction: licensing.py -> config.py (one-way only).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Grace period: enterprise mode continues for this many days after license expiry
GRACE_PERIOD_DAYS = 30

# Production public key (replace during release build)
_PUBLIC_KEY_B64 = "VbBoUWJAXqZH9i9R_dS_F_oTi621bfknNu1poul2dis"

# Test public key (used when MEHO_LICENSE_ENV=test)
_TEST_PUBLIC_KEY_B64 = "FqC74x-mlaoRPvJDg_2fS4zLZrnIXVgd1ytVAMbkebo"


class Edition(str, Enum):
    """Application edition determined by license key presence and validity."""

    COMMUNITY = "community"
    ENTERPRISE = "enterprise"


class LicensePayload(BaseModel):
    """Validated payload from a signed license key."""

    org: str
    tier: str
    features: list[str]
    issued_at: str
    expires_at: str | None = None
    max_tenants: int | None = None
    license_id: str


@dataclass(frozen=True)
class LicenseInfo:
    """Immutable license state after validation."""

    edition: Edition
    features: frozenset[str]
    org: str | None
    expires_at: datetime | None
    max_tenants: int | None
    in_grace_period: bool


def _get_public_key() -> Ed25519PublicKey:
    """Load the appropriate Ed25519 public key for verification."""
    import os

    key_b64 = _PUBLIC_KEY_B64
    if os.environ.get("MEHO_LICENSE_ENV") == "test":
        key_b64 = _TEST_PUBLIC_KEY_B64

    # base64url decode with padding and validate key length
    raw = base64.urlsafe_b64decode(key_b64 + "==")
    if len(raw) != 32:
        msg = "No valid public key configured for license verification"
        raise ValueError(msg)
    return Ed25519PublicKey.from_public_bytes(raw)


def _validate_license_key(key: str) -> LicensePayload | None:
    """
    Validate an Ed25519-signed license key.

    Key format: base64url(header).base64url(payload).base64url(signature)
    Returns the decoded payload on success, None on any failure.
    """
    try:
        parts = key.strip().split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode()

        # Decode and verify signature
        sig = base64.urlsafe_b64decode(sig_b64 + "==")
        public_key = _get_public_key()
        public_key.verify(sig, signing_input)

        # Decode and validate payload
        payload_json = base64.urlsafe_b64decode(payload_b64 + "==")
        data = json.loads(payload_json)
        return LicensePayload(**data)

    except (InvalidSignature, ValueError, json.JSONDecodeError, Exception) as e:
        logger.debug(f"License key validation failed: {e}")
        return None


class LicenseService:
    """
    Validates and caches license state. Initialized once at startup.

    Usage:
        svc = LicenseService()           # community mode (no key)
        svc = LicenseService(key="...")   # validates key, determines edition
    """

    def __init__(self, license_key: str | None = None) -> None:
        if not license_key:
            self._info = LicenseInfo(
                edition=Edition.COMMUNITY,
                features=frozenset(),
                org=None,
                expires_at=None,
                max_tenants=1,
                in_grace_period=False,
            )
            logger.info("Community edition -- enterprise routers excluded")
            return

        payload = _validate_license_key(license_key)
        if payload is None:
            self._info = LicenseInfo(
                edition=Edition.COMMUNITY,
                features=frozenset(),
                org=None,
                expires_at=None,
                max_tenants=1,
                in_grace_period=False,
            )
            logger.warning("Invalid license key -- falling back to community edition")
            return

        # Parse expiry and apply grace period logic
        expires_at: datetime | None = None
        in_grace_period = False
        edition = Edition.ENTERPRISE

        if payload.expires_at:
            try:
                expires_at = datetime.fromisoformat(payload.expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning(
                    f"Invalid expires_at format in license: {payload.expires_at}"
                )
                expires_at = None

        if expires_at is not None:
            now = datetime.now(timezone.utc)
            if now > expires_at:
                grace_end = expires_at + timedelta(days=GRACE_PERIOD_DAYS)
                if now <= grace_end:
                    remaining = (grace_end - now).days
                    in_grace_period = True
                    edition = Edition.ENTERPRISE
                    logger.warning(
                        f"License expired on {expires_at.date()} -- "
                        f"grace period active ({remaining} days remaining)"
                    )
                else:
                    edition = Edition.COMMUNITY
                    in_grace_period = False
                    logger.warning(
                        f"License expired on {expires_at.date()} -- "
                        f"grace period ended, community edition active"
                    )

        self._info = LicenseInfo(
            edition=edition,
            features=frozenset(payload.features),
            org=payload.org,
            expires_at=expires_at,
            max_tenants=payload.max_tenants,
            in_grace_period=in_grace_period,
        )

        if edition == Edition.ENTERPRISE and not in_grace_period:
            logger.info(f"Enterprise edition active (org={payload.org})")

    @property
    def edition(self) -> Edition:
        """Current application edition."""
        return self._info.edition

    @property
    def is_enterprise(self) -> bool:
        """Whether the application is running in enterprise mode."""
        return self._info.edition == Edition.ENTERPRISE

    @property
    def org(self) -> str | None:
        """Organization name from the license, or None for community."""
        return self._info.org

    @property
    def features(self) -> frozenset[str]:
        """Set of enabled enterprise features."""
        return self._info.features

    @property
    def expires_at(self) -> datetime | None:
        """License expiry datetime, or None for perpetual/community."""
        return self._info.expires_at

    @property
    def in_grace_period(self) -> bool:
        """Whether the license is in the post-expiry grace period."""
        return self._info.in_grace_period

    def has_feature(self, feature: str) -> bool:
        """Check if a specific enterprise feature is enabled."""
        return feature in self._info.features

    def to_api_response(self) -> dict:
        """Serialize for the /api/v1/license endpoint."""
        return {
            "edition": self._info.edition.value,
            "features": sorted(self._info.features),
            "org": self._info.org,
            "expires_at": (
                self._info.expires_at.isoformat() if self._info.expires_at else None
            ),
            "in_grace_period": self._info.in_grace_period,
        }


@lru_cache(maxsize=1)
def get_license_service() -> LicenseService:
    """
    Get the LicenseService singleton.

    Follows the same LRU-cached pattern as get_config().
    Import from config is deferred to avoid circular imports.
    """
    from meho_app.core.config import get_config

    config = get_config()
    return LicenseService(license_key=config.license_key)
