# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for the Intent Classifier (TASK-87)

Tests the lightweight pattern-based request type detection.
Ensures generic detection works across ANY system (no hardcoded keywords).
"""

from meho_app.modules.agents.intent_classifier import (
    ACTION_PHRASES,
    ACTION_VERBS,
    RequestType,
    detect_request_type,
)


class TestRequestTypeDetection:
    """Test request type detection for various user messages."""

    # =========================================================================
    # ACTION DETECTION TESTS
    # =========================================================================

    def test_detect_action_shutdown(self):
        """'Shut down' should be detected as ACTION."""
        result = detect_request_type("Shut down vm-57")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_delete(self):
        """'Delete' should be detected as ACTION."""
        result = detect_request_type("Delete the pod named nginx-123")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_restart(self):
        """'Restart' should be detected as ACTION."""
        result = detect_request_type("Restart the web server")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_create_phrase(self):
        """'Create a new' should be detected as ACTION."""
        result = detect_request_type("Create a new deployment for the app")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_spin_up(self):
        """'Spin up' should be detected as ACTION."""
        result = detect_request_type("Spin up 3 new containers")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_scale(self):
        """'Scale' should be detected as ACTION."""
        result = detect_request_type("Scale the deployment to 5 replicas")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_enable(self):
        """'Enable' should be detected as ACTION."""
        result = detect_request_type("Enable maintenance mode on the cluster")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_grant(self):
        """'Grant' should be detected as ACTION."""
        result = detect_request_type("Grant admin access to user john")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    def test_detect_action_deploy(self):
        """'Deploy' should be detected as ACTION."""
        result = detect_request_type("Deploy the new version to production")
        assert result.request_type == RequestType.ACTION
        assert result.confidence == "high"

    # =========================================================================
    # DATA QUERY DETECTION TESTS
    # =========================================================================

    def test_detect_data_query_list(self):
        """'List' should be detected as DATA_QUERY."""
        result = detect_request_type("List all VMs in the datacenter")
        assert result.request_type == RequestType.DATA_QUERY
        assert result.confidence == "high"

    def test_detect_data_query_show_me(self):
        """'Show me' should be detected as DATA_QUERY."""
        result = detect_request_type("Show me the running pods")
        assert result.request_type == RequestType.DATA_QUERY
        assert result.confidence == "high"

    def test_detect_data_query_what_are(self):
        """'What are the' should be detected as DATA_QUERY."""
        result = detect_request_type("What are the available namespaces?")
        assert result.request_type == RequestType.DATA_QUERY
        assert result.confidence == "high"

    def test_detect_data_query_get_the(self):
        """'Get the' (not 'get me a new') should be DATA_QUERY."""
        result = detect_request_type("Get the current status of all services")
        assert result.request_type == RequestType.DATA_QUERY
        assert result.confidence == "high"

    def test_detect_data_query_how_many(self):
        """'How many' should be detected as DATA_QUERY."""
        result = detect_request_type("How many nodes are in the cluster?")
        assert result.request_type == RequestType.DATA_QUERY
        assert result.confidence == "high"

    # =========================================================================
    # KNOWLEDGE DETECTION TESTS
    # =========================================================================

    def test_detect_knowledge_how_to(self):
        """'How to' should be detected as KNOWLEDGE."""
        result = detect_request_type("How to configure ingress in Kubernetes?")
        assert result.request_type == RequestType.KNOWLEDGE
        assert result.confidence == "high"

    def test_detect_knowledge_explain(self):
        """'Explain how' should be detected as KNOWLEDGE."""
        result = detect_request_type("Explain how pods differ from deployments")
        assert result.request_type == RequestType.KNOWLEDGE
        assert result.confidence == "high"

    def test_detect_knowledge_troubleshoot(self):
        """'Troubleshoot' should be detected as KNOWLEDGE."""
        result = detect_request_type("Troubleshoot why pod is in CrashLoopBackOff")
        assert result.request_type == RequestType.KNOWLEDGE
        assert result.confidence == "high"

    def test_detect_knowledge_best_practice(self):
        """'Best practice' should be detected as KNOWLEDGE."""
        result = detect_request_type("What's the best practice for secrets management?")
        assert result.request_type == RequestType.KNOWLEDGE
        assert result.confidence == "high"

    # =========================================================================
    # EDGE CASES AND AMBIGUOUS REQUESTS
    # =========================================================================

    def test_get_the_vs_get_me_a_new(self):
        """'Get the X' should be query, 'get me a new X' should be action."""
        # "Get the" = query
        result1 = detect_request_type("Get the list of all services")
        assert result1.request_type == RequestType.DATA_QUERY

        # "Get me a new" = action
        result2 = detect_request_type("Get me a new VM for testing")
        assert result2.request_type == RequestType.ACTION

    def test_unknown_for_ambiguous(self):
        """Ambiguous messages should return UNKNOWN."""
        result = detect_request_type("Hello!")
        assert result.request_type == RequestType.UNKNOWN
        assert result.confidence == "low"

    def test_action_takes_priority_over_data_query(self):
        """Action verbs should take priority over data query patterns."""
        # "Stop the services" has both "the" (query pattern) and "stop" (action verb)
        result = detect_request_type("Stop the services in production")
        assert result.request_type == RequestType.ACTION

    def test_knowledge_takes_highest_priority(self):
        """Knowledge patterns should be checked first."""
        # "How to stop" has both "how to" (knowledge) and "stop" (action)
        result = detect_request_type("How to stop a runaway process?")
        assert result.request_type == RequestType.KNOWLEDGE


class TestActionVerbsCoverage:
    """Test that ACTION_VERBS covers essential operation types."""

    def test_lifecycle_verbs_present(self):
        """Essential lifecycle verbs should be in ACTION_VERBS."""
        lifecycle = ["start", "stop", "restart", "reboot", "shutdown", "terminate"]
        for verb in lifecycle:
            assert verb in ACTION_VERBS, f"Missing lifecycle verb: {verb}"

    def test_crud_verbs_present(self):
        """Essential CRUD verbs should be in ACTION_VERBS."""
        crud = ["create", "delete", "update", "modify", "add", "remove"]
        for verb in crud:
            assert verb in ACTION_VERBS, f"Missing CRUD verb: {verb}"

    def test_state_change_verbs_present(self):
        """Essential state change verbs should be in ACTION_VERBS."""
        state = ["enable", "disable", "activate", "deactivate", "suspend", "resume"]
        for verb in state:
            assert verb in ACTION_VERBS, f"Missing state verb: {verb}"


class TestActionPhrasesCoverage:
    """Test that ACTION_PHRASES covers creation patterns."""

    def test_creation_phrases_present(self):
        """Creation phrases should be in ACTION_PHRASES."""
        creation = ["give me a new", "get me a new", "make a new", "create a"]
        for phrase in creation:
            assert phrase in ACTION_PHRASES, f"Missing creation phrase: {phrase}"

    def test_operation_phrases_present(self):
        """Operation phrases should be in ACTION_PHRASES."""
        ops = ["spin up", "bring down", "tear down", "turn on", "turn off"]
        for phrase in ops:
            assert phrase in ACTION_PHRASES, f"Missing operation phrase: {phrase}"


class TestGenericDetection:
    """Test that detection is generic - no system-specific keywords."""

    def test_works_for_kubernetes(self):
        """Detection should work for Kubernetes terminology."""
        result = detect_request_type("Delete the nginx-deployment pod")
        assert result.request_type == RequestType.ACTION

        result = detect_request_type("List all namespaces")
        assert result.request_type == RequestType.DATA_QUERY

    def test_works_for_vsphere(self):
        """Detection should work for vSphere terminology."""
        result = detect_request_type("Power off the SQL-Server VM")
        assert result.request_type == RequestType.ACTION

        result = detect_request_type("Show me all datastores")
        assert result.request_type == RequestType.DATA_QUERY

    def test_works_for_github(self):
        """Detection should work for GitHub terminology."""
        result = detect_request_type("Create a new issue for this bug")
        assert result.request_type == RequestType.ACTION

        result = detect_request_type("List all open pull requests")
        assert result.request_type == RequestType.DATA_QUERY

    def test_works_for_argocd(self):
        """Detection should work for ArgoCD terminology."""
        result = detect_request_type("Sync the production application")
        assert result.request_type == RequestType.ACTION

        # "Show me applications that are out of sync" matches "show me"
        result = detect_request_type("Show me applications that are out of sync")
        assert result.request_type == RequestType.DATA_QUERY

    def test_no_hardcoded_system_keywords(self):
        """ACTION_VERBS should not contain system-specific keywords."""
        system_keywords = [
            "vm",
            "pod",
            "container",
            "kubernetes",
            "k8s",
            "vsphere",
            "github",
            "argocd",
            "docker",
            "aws",
            "azure",
            "gcp",
        ]
        for keyword in system_keywords:
            assert keyword not in ACTION_VERBS, (
                f"System-specific keyword '{keyword}' found in ACTION_VERBS!"
            )


class TestDetectionResultStructure:
    """Test the DetectionResult structure."""

    def test_result_has_all_fields(self):
        """DetectionResult should have all expected fields."""
        result = detect_request_type("List all VMs")

        assert hasattr(result, "request_type")
        assert hasattr(result, "confidence")
        assert hasattr(result, "matched_pattern")
        assert hasattr(result, "reasoning")

    def test_matched_pattern_is_set(self):
        """matched_pattern should be set when pattern is found."""
        result = detect_request_type("Delete the pod")

        assert result.matched_pattern is not None
        assert result.matched_pattern in "delete the pod"  # Should match "delete"

    def test_reasoning_is_descriptive(self):
        """reasoning should explain why this type was detected."""
        result = detect_request_type("Restart the server")

        assert len(result.reasoning) > 0
        assert "action" in result.reasoning.lower() or "verb" in result.reasoning.lower()
