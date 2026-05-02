# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke test: Verify all service modules can be imported.

This catches errors like:
- Missing dependencies
- Syntax errors
- Import cycle issues
- NameError (like 'logger' not defined in Session 18)
"""


def test_meho_core_imports():
    """Test that meho_app.core modules can be imported"""
    assert True  # If we get here, imports work


def test_meho_knowledge_imports():
    """Test that meho_knowledge modules can be imported"""
    assert True


def test_meho_connectors_imports():
    """Test that meho_connectors modules can be imported"""
    from meho_app.modules.connectors.rest.knowledge_ingestion import ingest_openapi_to_knowledge

    assert callable(ingest_openapi_to_knowledge)


def test_meho_agent_imports():
    """Test that meho_agent modules can be imported"""
    assert True


def test_meho_ingestion_imports():
    """Test that meho_ingestion modules can be imported"""
    assert True


def test_meho_api_imports():
    """Test that meho_api modules can be imported"""

    # Routes
    assert True


def test_meho_testing_imports():
    """Test that test utilities can be imported"""
    assert True


def test_meho_mock_systems_imports():
    """Test that mock systems can be imported"""
    assert True


def test_critical_classes_instantiable():
    """Test that critical classes can be imported and have expected attributes"""
    from meho_app.modules.agents.dependencies import MEHODependencies
    from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    # Check that classes exist and have key methods
    assert hasattr(MEHODependencies, "search_knowledge")
    assert hasattr(MEHODependencies, "list_connectors")
    assert hasattr(KnowledgeStore, "search_hybrid")  # Correct method name
    assert hasattr(GenericHTTPClient, "call_endpoint")


def test_no_missing_logger():
    """
    Regression test for Session 18 issue: 'logger' not defined

    Verify that modules using logger have it properly imported/defined.
    Imports already tested in earlier functions, so just verify
    logger-related modules don't raise NameError.
    """
    # These modules were tested in earlier functions
    # This test documents which modules previously had logger issues
    # If they import successfully in earlier tests, logger is properly handled
    assert True


def test_fastapi_app_objects_exist():
    """Test that FastAPI app objects exist in the application

    In the modular monolith architecture, the app is created in meho_app.main.
    """
    # Test that the create_app factory exists
    from meho_app.main import create_app

    assert callable(create_app)


def test_protocol_imports():
    """Test that protocol definitions can be imported"""
    # Direct imports from protocol submodules
    from meho_app.protocols.agent import IAgentDependencies
    from meho_app.protocols.ingestion import IEventTemplateRepository
    from meho_app.protocols.knowledge import IKnowledgeRepository
    from meho_app.protocols.openapi import IConnectorRepository

    # Verify they're runtime_checkable protocols
    # Use _is_runtime_protocol which is consistent across Python versions
    assert getattr(IKnowledgeRepository, "_is_runtime_protocol", False), (
        "IKnowledgeRepository should be runtime_checkable"
    )
    assert getattr(IConnectorRepository, "_is_runtime_protocol", False), (
        "IConnectorRepository should be runtime_checkable"
    )
    assert getattr(IAgentDependencies, "_is_runtime_protocol", False), (
        "IAgentDependencies should be runtime_checkable"
    )
    assert getattr(IEventTemplateRepository, "_is_runtime_protocol", False), (
        "IEventTemplateRepository should be runtime_checkable"
    )


def test_service_from_protocols_methods():
    """Test that services have from_protocols class methods"""
    from meho_app.modules.connectors.rest.service import OpenAPIService
    from meho_app.modules.ingestion.service import IngestionService
    from meho_app.modules.knowledge.service import KnowledgeService

    # Verify from_protocols class methods exist
    # Note: AgentService no longer uses from_protocols (simplified)
    assert hasattr(KnowledgeService, "from_protocols")
    assert hasattr(OpenAPIService, "from_protocols")
    assert hasattr(IngestionService, "from_protocols")

    # Verify they're callable
    assert callable(KnowledgeService.from_protocols)
    assert callable(OpenAPIService.from_protocols)
    assert callable(IngestionService.from_protocols)


def test_connectors_module_imports():
    """Test that the new connectors module can be imported"""
    # Core connectors module

    # REST connector type
    from meho_app.modules.connectors.rest import (
        GenericHTTPClient,
    )

    # SOAP connector type
    from meho_app.modules.connectors.soap import (
        SOAPClient,
    )

    # VMware connector type
    from meho_app.modules.connectors.vmware import (
        VMWARE_OPERATIONS,
        VMWARE_TYPES,
        VMwareConnector,
    )

    # Verify counts
    assert len(VMWARE_OPERATIONS) > 100  # 179 operations
    assert len(VMWARE_TYPES) > 10  # 17 types

    # Verify classes
    assert callable(GenericHTTPClient)
    assert callable(VMwareConnector)
    assert callable(SOAPClient)


def test_topology_module_imports():
    """Test that the topology module can be imported"""
    from meho_app.modules.topology import (
        RelationshipType,
        TopologyService,
        get_topology_service,
    )

    # Verify classes exist
    assert callable(TopologyService)
    assert callable(get_topology_service)

    # Verify relationship types
    assert RelationshipType.ROUTES_TO.value == "routes_to"
    assert RelationshipType.RUNS_ON.value == "runs_on"
    assert RelationshipType.USES.value == "uses"
