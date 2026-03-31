"""
OpenAPI Service Routes - Direct HTTP endpoints (if needed).

Note: Most operations go through BFF (meho_api). 
This service primarily provides repository/database access.
"""
from fastapi import APIRouter, Depends, HTTPException
from meho_openapi.user_credentials import UserCredentialRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/openapi", tags=["openapi"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint"""
    return {"status": "healthy", "service": "meho-openapi"}


# NOTE: The route below was incomplete/broken. 
# If direct credential deletion is needed, it should be implemented properly
# with full imports, dependency injection, etc.
# For now, credential management goes through the BFF (meho_api).

# @router.delete("/credentials/{connector_id}/{user_id}", status_code=204)
# async def delete_credential(
#     connector_id: str,
#     user_id: str,
#     repo: UserCredentialRepository = Depends(get_credential_repository)
# ):
#     """Delete user credential"""
#     try:
#         deleted = await repo.delete_credentials(user_id, connector_id)
#         if not deleted:
#             raise HTTPException(status_code=404, detail="Credential not found")
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Failed to delete credential: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=f"Failed to delete credential: {str(e)}")
