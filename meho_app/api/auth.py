# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
JWT authentication for MEHO API.

Uses Keycloak JWKS-based RS256 validation for all token verification.
Keycloak is the sole identity provider - there are no alternative auth modes.
"""

# mypy: disable-error-code="assignment,no-any-return"
import time

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from pydantic import BaseModel

from meho_app.api.config import get_api_config
from meho_app.core.auth_context import UserContext

# Security scheme — auto_error=False gives consistent 401 for missing tokens
# (instead of FastAPI's default 403 when Authorization header is absent)
security = HTTPBearer(auto_error=False)


class TokenData(BaseModel):
    """Data extracted from JWT token"""

    user_id: str
    tenant_id: str
    roles: list[str] = []
    groups: list[str] = []
    name: str | None = None  # Phase 39: Display name from JWT


class KeycloakJWTValidator:
    """
    Validate JWTs from Keycloak using JWKS (public keys).

    This validator fetches and caches JWKS for each realm, enabling
    secure token validation without a shared secret.
    """

    def __init__(self, keycloak_url: str, client_id: str, cache_ttl: int = 3600):
        """
        Initialize the Keycloak JWT validator.

        Args:
            keycloak_url: Base URL of the Keycloak server
            client_id: OIDC client ID for audience validation
            cache_ttl: How long to cache JWKS in seconds (default: 1 hour)
        """
        self.keycloak_url = keycloak_url.rstrip("/")
        self.client_id = client_id
        self.cache_ttl = cache_ttl
        # Cache: realm -> (jwks_dict, expiry_timestamp)
        self._jwks_cache: dict[str, tuple[dict, float]] = {}

    def _extract_realm_from_issuer(self, issuer: str) -> str:
        """
        Extract realm name from the token issuer.

        Keycloak issuer format: {base_url}/realms/{realm}
        Example: http://localhost:8080/realms/example-tenant

        Args:
            issuer: The 'iss' claim from the token

        Returns:
            The realm name

        Raises:
            HTTPException: If issuer format is invalid
        """
        try:
            # Handle both http and https, and various base URLs
            if "/realms/" not in issuer:
                raise ValueError("Missing /realms/ in issuer")
            realm = issuer.split("/realms/")[-1].strip("/")
            if not realm:
                raise ValueError("Empty realm name")
            return realm
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token issuer format: {issuer}",
            ) from None

    async def get_jwks(self, realm: str) -> dict:
        """
        Fetch and cache JWKS for a realm.

        Args:
            realm: The Keycloak realm name

        Returns:
            JWKS dictionary containing public keys

        Raises:
            HTTPException: If JWKS cannot be fetched
        """
        current_time = time.time()

        # Check cache
        if realm in self._jwks_cache:
            jwks, expiry = self._jwks_cache[realm]
            if current_time < expiry:
                return jwks

        # Fetch fresh JWKS
        jwks_url = f"{self.keycloak_url}/realms/{realm}/protocol/openid-connect/certs"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(jwks_url)
                response.raise_for_status()
                jwks = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Unknown realm: {realm}"
                ) from e
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to fetch JWKS from Keycloak: {e}",
            ) from e
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Cannot connect to Keycloak: {e}",
            ) from e

        # Cache the JWKS
        self._jwks_cache[realm] = (jwks, current_time + self.cache_ttl)

        return jwks

    def _find_key_by_kid(self, jwks: dict, kid: str) -> dict | None:
        """
        Find the signing key in JWKS by key ID.

        Args:
            jwks: The JWKS dictionary
            kid: Key ID from token header

        Returns:
            The matching key or None
        """
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        return None

    async def validate_token(self, token: str) -> TokenData:
        """
        Validate a Keycloak JWT and extract claims.

        Steps:
        1. Decode header (without verification) to get 'kid' and extract issuer
        2. Extract realm from issuer
        3. Fetch JWKS for that realm
        4. Find matching key by 'kid'
        5. Validate signature, expiry
        6. Extract and return TokenData

        Args:
            token: The JWT token string

        Returns:
            TokenData with user information

        Raises:
            HTTPException: If token is invalid
        """
        # Decode header without verification to get kid and issuer
        try:
            unverified_header = jwt.get_unverified_header(token)
            unverified_claims = jwt.get_unverified_claims(token)
        except JWTError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token format: {e}"
            ) from e

        # Get key ID from header
        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing key ID (kid)"
            )

        # Extract realm from issuer
        issuer = unverified_claims.get("iss")
        if not issuer:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing issuer (iss)"
            )

        realm = self._extract_realm_from_issuer(issuer)

        # Fetch JWKS for this realm
        jwks = await self.get_jwks(realm)

        # Find the signing key
        signing_key = self._find_key_by_kid(jwks, kid)
        if not signing_key:
            # Key not found - maybe JWKS was rotated, try refreshing cache
            self._jwks_cache.pop(realm, None)
            jwks = await self.get_jwks(realm)
            signing_key = self._find_key_by_kid(jwks, kid)

            if not signing_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token signing key not found in JWKS",
                )

        # Validate the token
        try:
            # python-jose can decode directly with the JWK
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                # Audience validation is optional for Keycloak
                # as it uses 'azp' (authorized party) instead
                options={"verify_aud": False},
            )
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
            ) from None
        except JWTError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Token validation failed: {e}"
            ) from e

        # Extract claims
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject (sub)"
            )

        # Get email as alternative user identifier if preferred_username not available
        email = payload.get("email", payload.get("preferred_username", user_id))

        # Extract roles from realm_access or custom roles claim
        roles: list[str] = []

        # Check custom 'roles' claim first (configured in our realm)
        if "roles" in payload:
            roles = payload["roles"] if isinstance(payload["roles"], list) else []
        # Fall back to standard Keycloak structure
        elif "realm_access" in payload:
            roles = payload["realm_access"].get("roles", [])

        # Extract groups if present
        groups: list[str] = payload.get("groups", [])

        # Phase 39: Extract display name from JWT for war room sender attribution
        name = payload.get("name") or payload.get("preferred_username") or email

        return TokenData(
            user_id=email,  # Use email as user_id for better readability
            tenant_id=realm,  # Realm name is the tenant ID
            roles=roles,
            groups=groups,
            name=name,
        )


# Singleton validator instance
_keycloak_validator: KeycloakJWTValidator | None = None


def get_keycloak_validator() -> KeycloakJWTValidator:
    """Get or create the Keycloak JWT validator singleton."""
    global _keycloak_validator
    if _keycloak_validator is None:
        config = get_api_config()
        _keycloak_validator = KeycloakJWTValidator(
            keycloak_url=config.keycloak_url,
            client_id=config.keycloak_client_id,
            cache_ttl=config.jwks_cache_ttl,
        )
    return _keycloak_validator


def reset_keycloak_validator():
    """Reset validator singleton (for testing)."""
    global _keycloak_validator
    _keycloak_validator = None


async def get_current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> UserContext:
    """
    FastAPI dependency to get current user from Keycloak JWT token.

    Validates tokens via Keycloak JWKS (RS256).

    Also supports tenant context switching for superadmins via X-Acting-As-Tenant header.
    When a global_admin sends this header, their tenant_id is overridden while preserving
    their original identity for audit purposes.

    Also sets observability context so all traces include user/tenant info.

    Usage:
        @router.get("/protected")
        async def protected_route(user: UserContext = Depends(get_current_user)):
            # user.user_id, user.tenant_id, user.roles available

    Returns:
        UserContext for the authenticated user

    Raises:
        HTTPException: 401 if credentials are missing or invalid
    """
    from meho_app.core.otel import get_logger

    logger = get_logger(__name__)
    from meho_app.core.otel.context import set_request_context

    # Consistent 401 for missing Authorization header
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Debug: Log token presence (not the actual token for security)
    if not token:
        logger.warning("[Auth] No token provided in request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No authentication token provided"
        )

    logger.debug(f"[Auth] Validating token (length: {len(token)}, starts with: {token[:20]}...)")

    # Validate token via Keycloak JWKS
    validator = get_keycloak_validator()
    try:
        token_data = await validator.validate_token(token)
        logger.debug(
            f"[Auth] Token valid for user: {token_data.user_id}, tenant: {token_data.tenant_id}"
        )
    except HTTPException as e:
        logger.warning(f"[Auth] Token validation failed: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"[Auth] Unexpected error validating token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Token validation error: {e}"
        ) from e

    user = UserContext(
        user_id=token_data.user_id,
        name=token_data.name,
        tenant_id=token_data.tenant_id,
        roles=token_data.roles,
        groups=token_data.groups,
    )

    # Check for tenant context override (superadmin only) - TASK-140 Phase 2
    acting_as_tenant = request.headers.get("X-Acting-As-Tenant")
    if acting_as_tenant and user.is_global_admin():
        # Store original identity for audit purposes
        user = UserContext(
            user_id=user.user_id,
            name=user.name,
            tenant_id=acting_as_tenant,  # Override tenant context
            system_id=user.system_id,
            roles=user.roles,
            groups=user.groups,
            original_user_id=user.user_id,
            original_tenant_id=user.tenant_id,
            acting_as_superadmin=True,
        )

    # Set observability context for tracing
    set_request_context(
        user_id=user.get_audit_user_id(),
        tenant_id=user.tenant_id,
    )

    return user
