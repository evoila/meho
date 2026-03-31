# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for all BFF routes.

Ensures all BFF endpoints have proper request/response schemas and
don't break when services change their schemas.

Note: Workflow routes removed in Session 80, replaced by Recipes.
"""


class TestBFFChatRoutesContract:
    """Test BFF chat routes contracts"""

    def test_chat_session_response_schema(self):
        """Verify chat session response schema is defined"""
        from meho_app.api.routes_chat_sessions import SessionResponse

        fields = set(SessionResponse.model_fields.keys())

        # Core fields for chat session
        required_fields = {"id", "created_at"}

        missing = required_fields - fields

        assert not missing, f"SessionResponse missing required fields: {missing}"

    def test_message_response_schema(self):
        """Verify message response schema is defined"""
        from meho_app.api.routes_chat_sessions import MessageResponse

        fields = set(MessageResponse.model_fields.keys())

        # Core fields for message (workflow_id removed)
        required_fields = {"id", "role", "content", "created_at"}

        missing = required_fields - fields

        assert not missing, f"MessageResponse missing required fields: {missing}"


class TestBFFAuthRoutesContract:
    """Test BFF auth routes contracts"""

    def test_auth_routes_exist(self):
        """Verify auth routes module exists"""
        from meho_app.api import routes_auth

        assert hasattr(routes_auth, "router")

    def test_auth_discover_tenant_endpoint_exists(self):
        """
        Verify auth routes have tenant discovery endpoint.

        Frontend uses tenant discovery for email-based SSO.
        Keycloak is the sole authentication provider.
        """
        import inspect

        from meho_app.api import routes_auth

        # Check that tenant discovery function exists
        members = inspect.getmembers(routes_auth, inspect.isfunction)
        function_names = [name for name, _ in members]

        has_discover_endpoint = any(
            "discover" in name.lower() or "tenant" in name.lower() for name in function_names
        )

        assert has_discover_endpoint, "Auth routes should have tenant discovery endpoint"


class TestBFFKnowledgeRoutesContract:
    """Test BFF knowledge routes contracts"""

    def test_knowledge_routes_have_required_endpoints(self):
        """Verify BFF has all knowledge endpoints"""
        import inspect

        from meho_app.api import routes_knowledge

        members = inspect.getmembers(routes_knowledge, inspect.isfunction)
        function_names = [name for name, _ in members]

        # Required endpoints
        required_endpoints = ["list_chunks", "list_documents", "delete_document"]

        for endpoint in required_endpoints:
            assert endpoint in function_names, (
                f"BFF routes_knowledge should have {endpoint} endpoint"
            )

    def test_list_chunks_response_schema(self):
        """Verify ListChunksResponse schema is complete"""
        from meho_app.api.routes_knowledge import ListChunksResponse

        fields = set(ListChunksResponse.model_fields.keys())

        required_fields = {"chunks", "total"}

        missing = required_fields - fields

        assert not missing, f"ListChunksResponse missing required fields: {missing}"

    def test_knowledge_chunk_response_compatible_with_knowledge_service(self):
        """
        Test BFF KnowledgeChunkResponse matches what Knowledge service returns.

        Bug prevented: Session 43 - BFF expected fields Knowledge didn't provide.
        """
        from meho_app.api.routes_knowledge import KnowledgeChunkResponse as BFFResponse
        from meho_app.modules.knowledge.api_schemas import ChunkResponse as KnowledgeResponse

        bff_fields = set(BFFResponse.model_fields.keys())
        knowledge_fields = set(KnowledgeResponse.model_fields.keys())

        # Knowledge service must provide all fields BFF expects
        missing_in_knowledge = bff_fields - knowledge_fields

        assert not missing_in_knowledge, (
            f"Knowledge ChunkResponse missing fields that BFF expects: {missing_in_knowledge}. "
            f"This causes 500 errors when BFF tries to construct responses. "
            f"Add these fields to meho_knowledge/api_schemas.py:ChunkResponse"
        )


class TestBFFConnectorRoutesContract:
    """Test BFF connector routes contracts"""

    def test_connector_routes_exist(self):
        """Verify BFF has connector routes"""
        from meho_app.api import connectors

        assert hasattr(connectors, "router")

    def test_connector_endpoints_defined(self):
        """Verify connector CRUD endpoints exist"""
        import inspect

        from meho_app.api.connectors.operations import crud

        # Check CRUD operations module has connector functions
        crud_members = inspect.getmembers(crud, inspect.isfunction)
        function_names = [name for name, _ in crud_members]

        # Should have connector management endpoints
        has_connector_operations = any("connector" in name.lower() for name in function_names)

        assert has_connector_operations, "BFF should have connector management endpoints"


class TestBFFRecipeRoutesContract:
    """Test BFF recipe routes contracts (Session 80 - Unified Execution)"""

    def test_recipe_routes_exist(self):
        """Verify BFF has recipe routes"""
        from meho_app.api import routes_recipes

        assert hasattr(routes_recipes, "router")

    def test_recipe_endpoints_defined(self):
        """Verify recipe CRUD endpoints exist"""
        import inspect

        from meho_app.api import routes_recipes

        members = inspect.getmembers(routes_recipes, inspect.isfunction)
        function_names = [name for name, _ in members]

        # Required recipe endpoints
        required_endpoints = ["list_recipes", "get_recipe", "create_recipe"]

        for endpoint in required_endpoints:
            assert endpoint in function_names, f"BFF routes_recipes should have {endpoint} endpoint"
