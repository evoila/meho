# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Configuration for MEHO API (BFF) service.
"""

# mypy: disable-error-code="no-untyped-def"
import json
from typing import Any

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CORS_ORIGINS = ["http://localhost:3000", "http://localhost:5173"]


class MEHOAPIConfig(BaseSettings):
    """MEHO API configuration"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    _cors_origins: list[str] = PrivateAttr(default_factory=list)

    # Service URLs
    knowledge_service_url: str = Field(default="http://localhost:8001")
    openapi_service_url: str = Field(default="http://localhost:8002")
    agent_service_url: str = Field(default="http://localhost:8003")
    ingestion_service_url: str = Field(default="http://localhost:8004")
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Redis connection URL for state persistence"
    )

    # Keycloak authentication settings (REQUIRED)
    keycloak_url: str = Field(default="http://localhost:8080", description="Keycloak server URL")
    keycloak_client_id: str = Field(
        default="meho-api", description="OIDC client ID for audience validation"
    )
    jwks_cache_ttl: int = Field(default=3600, description="JWKS cache TTL in seconds")

    # Keycloak Admin credentials (for tenant realm management)
    keycloak_admin_username: str = Field(
        default="admin", description="Keycloak admin username for realm management"
    )
    keycloak_admin_password: str = Field(
        default="", description="Keycloak admin password (REQUIRED for tenant management)"
    )

    # API Settings
    api_host: str = Field(default="0.0.0.0")  # noqa: S104 -- intentional bind to all interfaces
    api_port: int = Field(default=8000)

    # CORS Settings (raw value ingested from env; normalized post-init)
    cors_origins_input: Any = Field(
        default=None,
        alias="cors_origins",
        description="Raw CORS origins as provided via environment variable",
    )

    # Environment
    environment: str = Field(default="development")

    @model_validator(mode="after")
    def finalize_cors(self) -> "MEHOAPIConfig":
        self._cors_origins = self._normalize_cors(self.cors_origins_input)
        return self

    @staticmethod
    def _normalize_cors(value: Any) -> list[str]:
        """
        Allow CORS_ORIGINS to be provided as:
          - JSON array (["http://a","http://b"])
          - Compose-style bracketed values without quotes ([http://a,http://b])
          - Comma/space separated string (http://a,http://b)
          - Single origin string
        """
        if value is None:
            return DEFAULT_CORS_ORIGINS.copy()

        if isinstance(value, list):
            return [origin.strip() for origin in value if origin and origin.strip()]

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return DEFAULT_CORS_ORIGINS.copy()

            if raw.startswith("[") and raw.endswith("]"):
                raw = raw[1:-1]

            # If any quote characters remain, attempt to parse the sequence as JSON.
            if '"' in raw or "'" in raw:
                try:
                    parsed = json.loads(f"[{raw}]")
                    if isinstance(parsed, list):
                        return [
                            origin.strip()
                            for origin in parsed
                            if isinstance(origin, str) and origin.strip()
                        ]
                except json.JSONDecodeError:
                    # If JSON parsing fails, fall back to comma-separated parsing below.
                    pass

            items: list[str] = []
            for origin in raw.split(","):
                cleaned = origin.strip().strip("\"'")
                if cleaned:
                    items.append(cleaned)

            return items or DEFAULT_CORS_ORIGINS.copy()

        # Unsupported type; fall back to defaults
        return DEFAULT_CORS_ORIGINS.copy()

    @property
    def cors_origins(self) -> list[str]:
        return self._cors_origins or DEFAULT_CORS_ORIGINS.copy()


# Singleton
_config = None


def get_api_config() -> MEHOAPIConfig:
    """Get API configuration singleton"""
    global _config
    if _config is None:
        _config = MEHOAPIConfig()
    return _config


def reset_api_config():
    """Reset config singleton (for testing)"""
    global _config
    _config = None
