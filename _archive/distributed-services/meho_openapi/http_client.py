"""
Generic HTTP client for calling API endpoints.

Handles authentication and request construction dynamically.
"""
import httpx
from meho_openapi.schemas import Connector, EndpointDescriptor
from meho_core.errors import UpstreamApiError
from meho_openapi.session_manager import SessionManager
from typing import Dict, Any, Optional, Tuple, Callable
import base64
from urllib.parse import urljoin
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class GenericHTTPClient:
    """Generic HTTP client for calling any API"""
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session_manager = SessionManager(timeout=timeout)
    
    async def call_endpoint(
        self,
        connector: Connector,
        endpoint: EndpointDescriptor,
        path_params: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        user_credentials: Optional[Dict[str, str]] = None,
        session_token: Optional[str] = None,
        session_expires_at: Optional[datetime] = None,
        refresh_token: Optional[str] = None,
        refresh_expires_at: Optional[datetime] = None,
        on_session_update: Optional[Callable[[str, datetime, str, Optional[str], Optional[datetime]], None]] = None
    ) -> Tuple[int, Any]:
        """
        Call an API endpoint.
        
        Args:
            connector: Connector configuration
            endpoint: Endpoint descriptor
            path_params: Path parameters
            query_params: Query parameters
            body: Request body
            user_credentials: User-specific credentials (for USER_PROVIDED strategy)
            session_token: Current session token (for SESSION auth)
            session_expires_at: Session token expiry (for SESSION auth)
            refresh_token: Current refresh token (for SESSION auth)
            refresh_expires_at: Refresh token expiry (for SESSION auth)
            on_session_update: Callback to update session state (token, expires_at, state, refresh_token, refresh_expires_at)
        
        Returns:
            (status_code, response_data)
        """
        path_params = path_params or {}
        query_params = query_params or {}
        
        logger.info(f"\n{'='*80}")
        logger.info(f"🌐 HTTP_CLIENT: Making API request")
        logger.info(f"   Connector: {connector.name}")
        logger.info(f"   Base URL: {connector.base_url}")
        logger.info(f"   Endpoint: {endpoint.method} {endpoint.path}")
        logger.info(f"   Auth Type: {connector.auth_type}")
        logger.info(f"{'='*80}")
        
        # Handle SESSION auth - auto-login if needed
        if connector.auth_type == "SESSION":
            logger.info(f"🔐 HTTP_CLIENT: SESSION auth detected, checking login state...")
            if not user_credentials:
                raise ValueError("SESSION auth requires user_credentials")
            
            try:
                # Login (or reuse/refresh existing session)
                new_token, new_refresh, new_expires, new_refresh_expires, new_state = await self.session_manager.login(
                    connector=connector,
                    credentials=user_credentials,
                    session_token=session_token,
                    session_expires_at=session_expires_at,
                    refresh_token=refresh_token,
                    refresh_expires_at=refresh_expires_at
                )
                
                # Update session state if callback provided and state changed
                if on_session_update and (new_token != session_token or new_expires != session_expires_at):
                    logger.info(f"✅ HTTP_CLIENT: Updating session state via callback")
                    import inspect
                    if inspect.iscoroutinefunction(on_session_update):
                        await on_session_update(new_token, new_expires, new_state, new_refresh, new_refresh_expires)
                    else:
                        on_session_update(new_token, new_expires, new_state, new_refresh, new_refresh_expires)
                
                # Use the new token for this request
                session_token = new_token
                session_expires_at = new_expires
                refresh_token = new_refresh
                refresh_expires_at = new_refresh_expires
                
            except Exception as e:
                logger.error(f"❌ HTTP_CLIENT: Session login failed: {e}")
                raise ValueError(f"Failed to establish session: {e}")
        
        # Build URL
        try:
            url = self._build_url(connector.base_url, endpoint.path, path_params)
            logger.info(f"✅ HTTP_CLIENT: URL built: {url}")
        except Exception as e:
            logger.error(f"❌ HTTP_CLIENT: Failed to build URL: {e}")
            raise
        
        # Build headers with authentication
        try:
            headers = self._build_headers(
                connector,
                user_credentials,
                session_token=session_token
            )
            # Don't log full credentials, just keys
            safe_headers = {k: ('***' if 'auth' in k.lower() or 'token' in k.lower() else v) for k, v in headers.items()}
            logger.info(f"✅ HTTP_CLIENT: Headers built: {safe_headers}")
        except Exception as e:
            logger.error(f"❌ HTTP_CLIENT: Failed to build headers: {e}")
            raise
        
        # Make request
        logger.info(f"🚀 HTTP_CLIENT: Sending request...")
        logger.info(f"   Method: {endpoint.method}")
        logger.info(f"   URL: {url}")
        logger.info(f"   Query params: {query_params}")
        logger.info(f"   Body: {'<present>' if body else '<none>'}")
        
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:  # verify=False for self-signed certs
            try:
                import time
                start_time = time.time()
                
                response = await client.request(
                    method=endpoint.method,
                    url=url,
                    params=query_params,
                    json=body if body else None,
                    headers=headers
                )
                
                duration = time.time() - start_time
                logger.info(f"✅ HTTP_CLIENT: Response received in {duration:.2f}s")
                logger.info(f"   Status code: {response.status_code}")
                logger.info(f"   Response headers: {dict(response.headers)}")
                
                # Parse response
                try:
                    data = response.json()
                    logger.info(f"   Response type: JSON")
                    if isinstance(data, dict):
                        logger.info(f"   Response keys: {list(data.keys())}")
                    elif isinstance(data, list):
                        logger.info(f"   Response items: {len(data)}")
                except:
                    data = response.text
                    logger.info(f"   Response type: Text ({len(data)} chars)")
                
                if response.status_code >= 400:
                    logger.error(f"❌ HTTP_CLIENT: API returned error status {response.status_code}")
                    logger.error(f"   Response: {data}")
                    raise UpstreamApiError(
                        status_code=response.status_code,
                        url=url,
                        payload=data
                    )
                
                logger.info(f"✅ HTTP_CLIENT: Request successful")
                return response.status_code, data
            
            except httpx.TimeoutException as e:
                logger.error(f"⏱️  HTTP_CLIENT: Request timeout after {self.timeout}s")
                logger.error(f"   URL: {url}")
                raise UpstreamApiError(status_code=504, url=url, message="Request timeout")
            except httpx.RequestError as e:
                logger.error(f"❌ HTTP_CLIENT: Request failed")
                logger.error(f"   Error: {e}")
                logger.error(f"   Error type: {type(e).__name__}")
                logger.error(f"   URL: {url}")
                raise UpstreamApiError(status_code=503, url=url, message=f"Request failed: {e}")
            except Exception as e:
                logger.error(f"❌ HTTP_CLIENT: Unexpected error")
                logger.error(f"   Error: {e}")
                logger.error(f"   Error type: {type(e).__name__}")
                import traceback
                logger.error(f"   Traceback:\n{traceback.format_exc()}")
                raise
    
    def _build_url(self, base_url: str, path: str, path_params: Dict[str, Any]) -> str:
        """Build full URL with path parameter substitution"""
        # Substitute path parameters
        for param_name, param_value in path_params.items():
            path = path.replace(f"{{{param_name}}}", str(param_value))
        
        # Ensure no unsubstituted parameters
        if re.search(r'\{[^}]+\}', path):
            raise ValueError(f"Missing required path parameters in: {path}")
        
        return urljoin(base_url, path.lstrip('/'))
    
    def _build_headers(
        self,
        connector: Connector,
        user_credentials: Optional[Dict[str, str]] = None,
        session_token: Optional[str] = None
    ) -> Dict[str, str]:
        """Build headers including authentication"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Use user credentials if provided (USER_PROVIDED strategy)
        creds = user_credentials if user_credentials else connector.auth_config
        
        # Add authentication based on type
        if connector.auth_type == "API_KEY":
            header_name = creds.get('header_name', 'X-API-Key')
            api_key = creds.get('api_key')
            if api_key:
                headers[header_name] = api_key
        
        elif connector.auth_type == "BASIC":
            username = creds.get('username', '')
            password = creds.get('password', '')
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers['Authorization'] = f"Basic {credentials}"
        
        elif connector.auth_type == "OAUTH2":
            access_token = creds.get('access_token')
            if access_token:
                headers['Authorization'] = f"Bearer {access_token}"
        
        elif connector.auth_type == "SESSION":
            # Add session token to headers
            if session_token:
                session_headers = self.session_manager.build_auth_headers(connector, session_token)
                headers.update(session_headers)
        
        return headers

