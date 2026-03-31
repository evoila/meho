"""
Simple JWT authentication for MEHO API.

NOTE: This is MVP authentication for development/testing.
Production should use OIDC (Auth0, Keycloak, etc.)
"""
# mypy: disable-error-code="assignment,no-any-return"
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from meho_api.config import get_api_config
from meho_core.auth_context import UserContext


# Security scheme
security = HTTPBearer()


class TokenData(BaseModel):
    """Data extracted from JWT token"""
    user_id: str
    tenant_id: str
    roles: list[str] = []
    groups: list[str] = []


def create_access_token(
    user_id: str,
    tenant_id: str,
    roles: list[str] = None,
    groups: list[str] = None
) -> str:
    """
    Create JWT access token.
    
    NOTE: For development/testing only!
    Production should use proper OIDC provider.
    
    Args:
        user_id: User identifier
        tenant_id: Tenant identifier
        roles: User roles
        groups: User groups
        
    Returns:
        JWT token string
    """
    config = get_api_config()
    
    expires = datetime.now(timezone.utc) + timedelta(hours=config.jwt_expiration_hours)
    
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": roles or [],
        "groups": groups or [],
        "exp": expires,
        "iat": datetime.now(timezone.utc)
    }
    
    return jwt.encode(payload, config.jwt_secret_key, algorithm=config.jwt_algorithm)


def verify_token(token: str) -> TokenData:
    """
    Verify JWT token and extract data.
    
    Args:
        token: JWT token string
        
    Returns:
        TokenData with user information
        
    Raises:
        HTTPException: If token is invalid or expired
    """
    config = get_api_config()
    
    try:
        payload = jwt.decode(
            token,
            config.jwt_secret_key,
            algorithms=[config.jwt_algorithm]
        )
        
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user ID"
            )
        
        return TokenData(
            user_id=user_id,
            tenant_id=payload.get("tenant_id"),
            roles=payload.get("roles", []),
            groups=payload.get("groups", [])
        )
        
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}"
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> UserContext:
    """
    FastAPI dependency to get current user from JWT token.
    
    Usage:
        @router.get("/protected")
        async def protected_route(user: UserContext = Depends(get_current_user)):
            # user.user_id, user.tenant_id, user.roles available
    
    Returns:
        UserContext for the authenticated user
    """
    token = credentials.credentials
    token_data = verify_token(token)
    
    return UserContext(
        user_id=token_data.user_id,
        tenant_id=token_data.tenant_id,
        roles=token_data.roles,
        groups=token_data.groups
    )


def create_test_token(
    user_id: str = "test-user@example.com",
    tenant_id: str = "test-tenant",
    roles: list[str] = None
) -> str:
    """
    Create a test token for development.
    
    Example:
        # Create admin token
        token = create_test_token("admin@company.com", "company", ["admin"])
        
        # Use in requests
        headers = {"Authorization": f"Bearer {token}"}
        requests.get("http://localhost:8000/api/chat", headers=headers)
    
    Args:
        user_id: User ID
        tenant_id: Tenant ID
        roles: User roles
        
    Returns:
        JWT token string
    """
    return create_access_token(user_id, tenant_id, roles or ["user"])

