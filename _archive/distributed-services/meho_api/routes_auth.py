"""
Authentication routes for MEHO API.

MVP implementation with simple JWT tokens.
Production will use Keycloak (Task 19b).
"""
# mypy: disable-error-code="no-untyped-def"
from fastapi import APIRouter
from pydantic import BaseModel
from meho_api.auth import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class TestTokenRequest(BaseModel):
    """Request to generate a test token"""
    user_id: str
    tenant_id: str
    roles: list[str] = ["user"]


class TestTokenResponse(BaseModel):
    """Response with generated token"""
    token: str
    user_id: str
    tenant_id: str
    roles: list[str]


@router.post("/test-token", response_model=TestTokenResponse)
async def generate_test_token(request: TestTokenRequest):
    """
    Generate a test JWT token for development.
    
    NOTE: This endpoint should be DISABLED in production!
    For production, use Keycloak or another OIDC provider.
    
    Example request:
    ```json
    {
        "user_id": "demo@example.com",
        "tenant_id": "demo-tenant",
        "roles": ["admin"]
    }
    ```
    
    Returns:
        JWT token valid for 24 hours
    """
    token = create_access_token(
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        roles=request.roles
    )
    
    return TestTokenResponse(
        token=token,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        roles=request.roles
    )

