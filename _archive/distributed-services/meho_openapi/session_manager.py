"""
Session manager for handling session-based authentication.

Manages login, session token storage, expiry tracking, and auto-login.
"""
import httpx
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import urljoin
import json

from meho_openapi.schemas import Connector

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages session-based authentication for connectors"""
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
    
    async def login(
        self,
        connector: Connector,
        credentials: Dict[str, str],
        session_token: Optional[str] = None,
        session_expires_at: Optional[datetime] = None,
        refresh_token: Optional[str] = None,
        refresh_expires_at: Optional[datetime] = None
    ) -> Tuple[str, Optional[str], datetime, Optional[datetime], str]:
        """
        Login to get a session token (and optionally refresh token).
        
        Args:
            connector: Connector configuration with login_url and login_config
            credentials: User credentials (username, password, etc.)
            session_token: Current session token (if any)
            session_expires_at: Current session expiry (if any)
            refresh_token: Current refresh token (if any)
            refresh_expires_at: Current refresh expiry (if any)
        
        Returns:
            (session_token, refresh_token, session_expires_at, refresh_expires_at, session_state)
        
        Raises:
            ValueError: If connector doesn't support SESSION auth
            UpstreamApiError: If login fails
        """
        # Check if we need to login
        if session_token and session_expires_at:
            now = datetime.utcnow()
            # If token is still valid for more than 5 minutes, reuse it
            if session_expires_at > now + timedelta(minutes=5):
                logger.info(f"✅ SESSION_MANAGER: Reusing valid session token (expires in {(session_expires_at - now).total_seconds():.0f}s)")
                return session_token, refresh_token, session_expires_at, refresh_expires_at, "LOGGED_IN"
            else:
                logger.info(f"⚠️  SESSION_MANAGER: Session token expired or expiring soon")
                
                # Try to refresh using refresh token if available
                if refresh_token and connector.login_config and connector.login_config.get('refresh_url'):
                    # Check if refresh token is still valid
                    if not refresh_expires_at or refresh_expires_at > now:
                        try:
                            logger.info(f"🔄 SESSION_MANAGER: Attempting to refresh using refresh token")
                            new_session_token, new_expires_at = await self.refresh(connector, refresh_token)
                            logger.info(f"✅ SESSION_MANAGER: Token refreshed successfully")
                            return new_session_token, refresh_token, new_expires_at, refresh_expires_at, "LOGGED_IN"
                        except Exception as e:
                            logger.warning(f"⚠️  SESSION_MANAGER: Refresh failed: {e}, will perform full login")
                    else:
                        logger.info(f"⚠️  SESSION_MANAGER: Refresh token expired, performing full login")
                
                logger.info(f"🔐 SESSION_MANAGER: Performing full login")
        
        # Validate connector configuration
        if connector.auth_type != "SESSION":
            raise ValueError(f"Connector auth_type must be SESSION, got: {connector.auth_type}")
        
        if not connector.login_url:
            raise ValueError("Connector login_url is required for SESSION auth")
        
        if not connector.login_config:
            raise ValueError("Connector login_config is required for SESSION auth")
        
        logger.info(f"\n{'='*80}")
        logger.info(f"🔐 SESSION_MANAGER: Logging in to connector")
        logger.info(f"   Connector: {connector.name}")
        logger.info(f"   Login URL: {connector.login_url}")
        logger.info(f"   Login Method: {connector.login_method or 'POST'}")
        logger.info(f"{'='*80}")
        
        # Build login URL
        login_url = urljoin(connector.base_url, connector.login_url.lstrip('/'))
        logger.info(f"✅ SESSION_MANAGER: Full login URL: {login_url}")
        
        # Determine login auth type (basic or body)
        login_auth_type = connector.login_config.get('login_auth_type', 'body')
        logger.info(f"✅ SESSION_MANAGER: Login auth type: {login_auth_type}")
        
        # Build login headers (default + custom)
        login_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Add custom login headers from config
        custom_headers = connector.login_config.get('login_headers', {})
        if custom_headers:
            logger.info(f"✅ SESSION_MANAGER: Adding custom login headers: {list(custom_headers.keys())}")
            login_headers.update(custom_headers)
        
        # Make login request
        login_method = (connector.login_method or "POST").upper()
        
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            try:
                logger.info(f"🚀 SESSION_MANAGER: Sending login request...")
                
                # Choose auth method based on login_auth_type
                if login_auth_type == 'basic':
                    # Basic Auth: Send credentials in Authorization header
                    username = credentials.get('username', '')
                    password = credentials.get('password', '')
                    logger.info(f"✅ SESSION_MANAGER: Using Basic Auth for user: {username}")
                    
                    response = await client.request(
                        method=login_method,
                        url=login_url,
                        auth=(username, password),  # httpx will encode as Basic Auth
                        headers=login_headers
                    )
                else:
                    # Body Auth (default): Send credentials in JSON body
                    login_body = self._build_login_body(connector.login_config, credentials)
                    logger.info(f"✅ SESSION_MANAGER: Using body auth with keys: {list(login_body.keys())}")
                    
                    response = await client.request(
                        method=login_method,
                        url=login_url,
                        json=login_body if login_method == "POST" else None,
                        params=login_body if login_method == "GET" else None,
                        headers=login_headers
                    )
                
                logger.info(f"✅ SESSION_MANAGER: Login response status: {response.status_code}")
                
                if response.status_code >= 400:
                    error_text = response.text
                    logger.error(f"❌ SESSION_MANAGER: Login failed with status {response.status_code}")
                    logger.error(f"   Response: {error_text}")
                    raise ValueError(f"Login failed: {response.status_code} - {error_text}")
                
                # Parse response
                try:
                    response_data = response.json()
                except:
                    response_data = {"text": response.text}
                
                logger.info(f"✅ SESSION_MANAGER: Login successful, extracting token...")
                
                # Extract session token from response
                session_token = self._extract_session_token(
                    connector.login_config,
                    response_data,
                    response.headers,
                    response.cookies
                )
                
                if not session_token:
                    logger.error(f"❌ SESSION_MANAGER: Could not extract session token from response")
                    logger.error(f"   Response data: {response_data}")
                    logger.error(f"   Headers: {dict(response.headers)}")
                    raise ValueError("Could not extract session token from login response")
                
                logger.info(f"✅ SESSION_MANAGER: Session token extracted: {session_token[:20]}...")
                
                # Extract refresh token (optional)
                refresh_token = None
                refresh_expires_at = None
                if connector.login_config.get('refresh_token_path'):
                    refresh_token = self._extract_refresh_token(
                        connector.login_config,
                        response_data
                    )
                    if refresh_token:
                        logger.info(f"✅ SESSION_MANAGER: Refresh token extracted: {refresh_token[:20] if len(refresh_token) > 20 else refresh_token[:10]}...")
                        
                        # Calculate refresh token expiry (if configured)
                        refresh_duration = connector.login_config.get('refresh_token_expires_in')
                        if refresh_duration:
                            refresh_expires_at = datetime.utcnow() + timedelta(seconds=refresh_duration)
                            logger.info(f"✅ SESSION_MANAGER: Refresh token expires at: {refresh_expires_at}")
                        else:
                            logger.info(f"✅ SESSION_MANAGER: Refresh token has no expiry")
                    else:
                        logger.warning(f"⚠️  SESSION_MANAGER: Refresh token path configured but token not found")
                
                # Calculate session expiry
                session_duration = connector.login_config.get('session_duration_seconds', 3600)
                expires_at = datetime.utcnow() + timedelta(seconds=session_duration)
                
                logger.info(f"✅ SESSION_MANAGER: Session expires at: {expires_at} (in {session_duration}s)")
                
                return session_token, refresh_token, expires_at, refresh_expires_at, "LOGGED_IN"
            
            except httpx.TimeoutException as e:
                logger.error(f"⏱️  SESSION_MANAGER: Login timeout after {self.timeout}s")
                raise ValueError(f"Login timeout: {e}")
            except httpx.RequestError as e:
                logger.error(f"❌ SESSION_MANAGER: Login request failed: {e}")
                raise ValueError(f"Login request failed: {e}")
    
    def _build_login_body(self, login_config: Dict[str, Any], credentials: Dict[str, str]) -> Dict[str, Any]:
        """
        Build login request body from template.
        
        login_config should contain:
        {
            "body_template": {
                "username": "{{username}}",
                "password": "{{password}}",
                ...
            }
        }
        """
        body_template = login_config.get('body_template', {})
        login_body = {}
        
        for key, value in body_template.items():
            if isinstance(value, str) and value.startswith('{{') and value.endswith('}}'):
                # Extract variable name from {{variable}}
                var_name = value[2:-2].strip()
                if var_name in credentials:
                    login_body[key] = credentials[var_name]
                else:
                    logger.warning(f"⚠️  SESSION_MANAGER: Missing credential: {var_name}")
                    login_body[key] = value  # Keep template as-is
            else:
                # Static value
                login_body[key] = value
        
        return login_body
    
    def _extract_session_token(
        self,
        login_config: Dict[str, Any],
        response_data: Dict[str, Any],
        headers: Any,
        cookies: Any
    ) -> Optional[str]:
        """
        Extract session token from login response.
        
        login_config should contain:
        {
            "token_location": "header" | "cookie" | "body",
            "token_name": "X-Auth-Token" | "sessionId" | etc.,
            "token_path": "$.token" (JSONPath for body location)
        }
        """
        token_location = login_config.get('token_location', 'header')
        token_name = login_config.get('token_name', 'X-Auth-Token')
        
        logger.info(f"🔍 SESSION_MANAGER: Extracting token from {token_location} with name {token_name}")
        
        if token_location == 'header':
            # Extract from response headers
            token = headers.get(token_name)
            if token:
                logger.info(f"✅ SESSION_MANAGER: Found token in header {token_name}")
                return str(token)
        
        elif token_location == 'cookie':
            # Extract from cookies
            token = cookies.get(token_name)
            if token:
                logger.info(f"✅ SESSION_MANAGER: Found token in cookie {token_name}")
                return str(token)
        
        elif token_location == 'body':
            # Extract from response body using JSONPath
            token_path = login_config.get('token_path', f'$.{token_name}')
            token = self._jsonpath_extract(response_data, token_path)
            if token:
                logger.info(f"✅ SESSION_MANAGER: Found token in body at path {token_path}")
                return str(token)
        
        logger.warning(f"⚠️  SESSION_MANAGER: Could not find token in {token_location}")
        return None
    
    def _extract_refresh_token(
        self,
        login_config: Dict[str, Any],
        response_data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Extract refresh token from login response (body only).
        
        login_config should contain:
        {
            "refresh_token_path": "$.refreshToken" or "$.refreshToken.id"
        }
        """
        refresh_token_path = login_config.get('refresh_token_path')
        if not refresh_token_path:
            return None
        
        logger.info(f"🔍 SESSION_MANAGER: Extracting refresh token from path {refresh_token_path}")
        
        # Extract from response body using JSONPath
        token = self._jsonpath_extract(response_data, refresh_token_path)
        if token:
            logger.info(f"✅ SESSION_MANAGER: Found refresh token at path {refresh_token_path}")
            return str(token)
        
        logger.warning(f"⚠️  SESSION_MANAGER: Could not find refresh token at {refresh_token_path}")
        return None
    
    def _jsonpath_extract(self, data: Any, path: str) -> Optional[Any]:
        """
        Simple JSONPath extraction (supports $.key and $.key.subkey).
        
        For more complex paths, consider using jsonpath-ng library.
        """
        if not path.startswith('$.'):
            return None
        
        keys = path[2:].split('.')
        current = data
        
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
                if current is None:
                    return None
            else:
                return None
        
        return current
    
    async def refresh(
        self,
        connector: Connector,
        refresh_token: str
    ) -> Tuple[str, datetime]:
        """
        Refresh access token using refresh token.
        
        Args:
            connector: Connector configuration with refresh_url in login_config
            refresh_token: Current refresh token
        
        Returns:
            (new_access_token, new_expires_at)
        
        Raises:
            ValueError: If connector doesn't support refresh or refresh fails
        """
        if not connector.login_config:
            raise ValueError("Connector login_config is required for refresh")
        
        refresh_url = connector.login_config.get('refresh_url')
        if not refresh_url:
            raise ValueError("Connector does not support token refresh (no refresh_url configured)")
        
        logger.info(f"\n{'='*80}")
        logger.info(f"🔄 SESSION_MANAGER: Refreshing access token")
        logger.info(f"   Connector: {connector.name}")
        logger.info(f"   Refresh URL: {refresh_url}")
        logger.info(f"{'='*80}")
        
        # Build refresh URL
        full_refresh_url = urljoin(connector.base_url, refresh_url.lstrip('/'))
        logger.info(f"✅ SESSION_MANAGER: Full refresh URL: {full_refresh_url}")
        
        # Build refresh request body from template
        refresh_body = self._build_refresh_body(connector.login_config, refresh_token)
        logger.info(f"✅ SESSION_MANAGER: Refresh body keys: {list(refresh_body.keys())}")
        
        # Get refresh method (default to POST)
        refresh_method = connector.login_config.get('refresh_method', 'POST').upper()
        
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            try:
                logger.info(f"🚀 SESSION_MANAGER: Sending refresh request...")
                
                response = await client.request(
                    method=refresh_method,
                    url=full_refresh_url,
                    json=refresh_body if refresh_method in ['POST', 'PATCH', 'PUT'] else None,
                    params=refresh_body if refresh_method == 'GET' else None,
                    headers={
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    }
                )
                
                logger.info(f"✅ SESSION_MANAGER: Refresh response status: {response.status_code}")
                
                if response.status_code >= 400:
                    error_text = response.text
                    logger.error(f"❌ SESSION_MANAGER: Refresh failed with status {response.status_code}")
                    logger.error(f"   Response: {error_text}")
                    raise ValueError(f"Token refresh failed: {response.status_code} - {error_text}")
                
                # Parse response
                try:
                    response_data = response.json()
                except:
                    response_data = {"text": response.text}
                
                logger.info(f"✅ SESSION_MANAGER: Refresh successful, extracting new token...")
                
                # Extract new access token
                new_token = self._extract_session_token(
                    connector.login_config,
                    response_data,
                    response.headers,
                    response.cookies
                )
                
                if not new_token:
                    logger.error(f"❌ SESSION_MANAGER: Could not extract new token from refresh response")
                    logger.error(f"   Response data: {response_data}")
                    raise ValueError("Could not extract new token from refresh response")
                
                logger.info(f"✅ SESSION_MANAGER: New access token extracted: {new_token[:20]}...")
                
                # Calculate new expiry
                session_duration = connector.login_config.get('session_duration_seconds', 3600)
                new_expires_at = datetime.utcnow() + timedelta(seconds=session_duration)
                
                logger.info(f"✅ SESSION_MANAGER: New token expires at: {new_expires_at} (in {session_duration}s)")
                
                return new_token, new_expires_at
            
            except httpx.TimeoutException as e:
                logger.error(f"⏱️  SESSION_MANAGER: Refresh timeout after {self.timeout}s")
                raise ValueError(f"Token refresh timeout: {e}")
            except httpx.RequestError as e:
                logger.error(f"❌ SESSION_MANAGER: Refresh request failed: {e}")
                raise ValueError(f"Token refresh failed: {e}")
    
    def _build_refresh_body(self, login_config: Dict[str, Any], refresh_token: str) -> Dict[str, Any]:
        """
        Build refresh request body from template.
        
        login_config should contain:
        {
            "refresh_body_template": {
                "refreshToken": {"id": "{{refresh_token}}"},
                ...
            }
        }
        """
        body_template = login_config.get('refresh_body_template', {})
        if not body_template:
            # Default: simple refresh_token field
            return {"refresh_token": refresh_token}
        
        # Replace {{refresh_token}} placeholders
        result = self._replace_template_vars(body_template, {"refresh_token": refresh_token})
        return result if isinstance(result, dict) else {"refresh_token": refresh_token}
    
    def _replace_template_vars(self, template: Any, vars: Dict[str, str]) -> Any:
        """
        Recursively replace template variables in a data structure.
        
        Replaces {{var_name}} with actual values.
        """
        if isinstance(template, dict):
            return {k: self._replace_template_vars(v, vars) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._replace_template_vars(item, vars) for item in template]
        elif isinstance(template, str):
            # Replace {{var}} with actual value
            if template.startswith('{{') and template.endswith('}}'):
                var_name = template[2:-2].strip()
                return vars.get(var_name, template)
            return template
        else:
            return template
    
    def build_auth_headers(
        self,
        connector: Connector,
        session_token: str
    ) -> Dict[str, str]:
        """
        Build authentication headers with session token.
        
        Returns headers dict with the session token in the appropriate location.
        
        For SESSION auth:
        - If header_name is specified: use that for sending token
        - Else if token_location=header and token_name exists: use token_name (e.g., vCenter)
        - Else: default to Authorization: Bearer
        """
        if not connector.login_config:
            # Fallback: Use Bearer format (most common for SESSION auth)
            return {"Authorization": f"Bearer {session_token}"}
        
        # Check if there's a specific header_name configured for requests
        header_name = connector.login_config.get('header_name')
        
        if header_name:
            # Use explicitly configured header name
            headers = {header_name: session_token}
        elif connector.login_config.get('token_location') == 'header' and connector.login_config.get('token_name'):
            # If token is extracted from header and we have the header name,
            # use the same header name for sending (common pattern for APIs like vCenter)
            headers = {connector.login_config['token_name']: session_token}
            logger.info(f"Using token_name for auth header: {connector.login_config['token_name']}")
        else:
            # Default: Use Bearer Authorization (standard for most APIs)
            headers = {"Authorization": f"Bearer {session_token}"}
        
        # Note: Cookie-based auth would be handled differently (using httpx cookies param)
        
        return headers

