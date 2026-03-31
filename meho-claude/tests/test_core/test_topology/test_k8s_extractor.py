"""Tests for K8sEntityExtractor topology extraction."""

import pytest

from meho_claude.core.topology.models import ExtractionResult


# Sample K8s data as returned by .to_dict()
SAMPLE_POD_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {
                    "name": "nginx-abc",
                    "namespace": "default",
                    "uid": "pod-uid-1",
                },
                "spec": {"nodeName": "node-1"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.5",
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                    ],
                },
            },
            {
                "metadata": {
                    "name": "redis-xyz",
                    "namespace": "cache",
                    "uid": "pod-uid-2",
                },
                "spec": {"nodeName": "node-2"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.6",
                    "conditions": [],
                },
            },
        ]
    }
}

SAMPLE_NODE_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {"name": "node-1", "uid": "node-uid-1"},
                "spec": {"providerID": "vsphere://421A6D12-ABC-DEF-GHI-123456789ABC"},
                "status": {
                    "addresses": [
                        {"type": "InternalIP", "address": "192.168.1.10"},
                        {"type": "Hostname", "address": "node-1.k8s.local"},
                    ],
                    "conditions": [],
                },
            },
            {
                "metadata": {"name": "node-2", "uid": "node-uid-2"},
                "spec": {},
                "status": {
                    "addresses": [
                        {"type": "InternalIP", "address": "192.168.1.11"},
                    ],
                    "conditions": [],
                },
            },
        ]
    }
}

SAMPLE_DEPLOYMENT_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {
                    "name": "nginx-deployment",
                    "namespace": "default",
                    "uid": "deploy-uid-1",
                },
                "spec": {"replicas": 3},
                "status": {"availableReplicas": 3, "readyReplicas": 3},
            },
        ]
    }
}

SAMPLE_SERVICE_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {
                    "name": "nginx-svc",
                    "namespace": "default",
                    "uid": "svc-uid-1",
                },
                "spec": {
                    "type": "ClusterIP",
                    "clusterIP": "10.96.0.100",
                    "ports": [{"port": 80, "targetPort": 8080}],
                    "selector": {"app": "nginx"},
                },
            },
        ]
    }
}

SAMPLE_INGRESS_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {
                    "name": "web-ingress",
                    "namespace": "default",
                    "uid": "ingress-uid-1",
                },
                "spec": {
                    "rules": [
                        {
                            "host": "web.example.com",
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "backend": {
                                            "service": {
                                                "name": "nginx-svc",
                                                "port": {"number": 80},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
        ]
    }
}

SAMPLE_NAMESPACE_LIST_DATA = {
    "data": {
        "items": [
            {
                "metadata": {"name": "default", "uid": "ns-uid-1"},
                "status": {"phase": "Active"},
            },
            {
                "metadata": {"name": "kube-system", "uid": "ns-uid-2"},
                "status": {"phase": "Active"},
            },
        ]
    }
}


@pytest.fixture
def extractor():
    from meho_claude.core.topology.extractors.kubernetes import K8sEntityExtractor

    return K8sEntityExtractor()


class TestK8sExtractorRegistration:
    def test_registered_in_extractor_registry(self):
        from meho_claude.core.topology.extractors.kubernetes import K8sEntityExtractor
        from meho_claude.core.topology.extractor import get_extractor_class

        # Import extractors package to trigger registration
        import meho_claude.core.topology.extractors  # noqa: F401

        cls = get_extractor_class("kubernetes")
        assert cls is K8sEntityExtractor


class TestK8sExtractPods:
    def test_extract_pods_returns_entities(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", SAMPLE_POD_LIST_DATA)

        assert isinstance(result, ExtractionResult)
        assert result.source_connector == "prod-cluster"
        assert result.source_operation == "list-pods"

        pods = [e for e in result.entities if e.entity_type == "kubernetes_pod"]
        assert len(pods) == 2

    def test_pod_entity_fields(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", SAMPLE_POD_LIST_DATA)

        pods = [e for e in result.entities if e.entity_type == "kubernetes_pod"]
        pod = next(p for p in pods if p.canonical_id == "pod-uid-1")

        assert pod.name == "nginx-abc"
        assert pod.connector_name == "prod-cluster"
        assert pod.connector_type == "kubernetes"
        assert pod.scope == {"namespace": "default"}
        assert "Pod default/nginx-abc" in pod.description

    def test_pod_raw_attributes(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", SAMPLE_POD_LIST_DATA)

        pods = [e for e in result.entities if e.entity_type == "kubernetes_pod"]
        pod = next(p for p in pods if p.canonical_id == "pod-uid-1")

        assert pod.raw_attributes["ip_address"] == "10.0.0.5"
        assert pod.raw_attributes["hostname"] == "nginx-abc"
        assert pod.raw_attributes["node_name"] == "node-1"
        assert pod.raw_attributes["phase"] == "Running"

    def test_pod_member_of_namespace_relationship(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", SAMPLE_POD_LIST_DATA)

        # Namespace entities synthesized from pod extraction
        ns_entities = [e for e in result.entities if e.entity_type == "kubernetes_namespace"]
        assert len(ns_entities) >= 1  # At least one namespace synthesized

        # member_of relationships
        member_ofs = [r for r in result.relationships if r.relationship_type == "member_of"]
        assert len(member_ofs) >= 1


class TestK8sExtractNodes:
    def test_extract_nodes_returns_entities(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        assert len(nodes) == 2

    def test_node_entity_fields(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-1")

        assert node.name == "node-1"
        assert node.connector_type == "kubernetes"
        assert "K8s node node-1" in node.description

    def test_node_provider_id_normalization(self, extractor):
        """provider_id: vsphere://UPPER-CASE-UUID -> vsphere://lower-case-uuid."""
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-1")

        provider_id = node.raw_attributes["provider_id"]
        assert provider_id == "vsphere://421a6d12-abc-def-ghi-123456789abc"

    def test_node_ip_address_from_status_addresses(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-1")

        assert node.raw_attributes["ip_address"] == "192.168.1.10"

    def test_node_hostname_from_status_addresses(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-1")

        assert node.raw_attributes["hostname"] == "node-1.k8s.local"

    def test_node_without_provider_id(self, extractor):
        """Nodes without spec.providerID should have empty provider_id."""
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-2")

        assert node.raw_attributes.get("provider_id", "") == ""

    def test_node_without_hostname_address(self, extractor):
        """Nodes without Hostname address should have empty hostname."""
        result = extractor.extract("prod-cluster", "kubernetes", "list-nodes", SAMPLE_NODE_LIST_DATA)

        nodes = [e for e in result.entities if e.entity_type == "kubernetes_node"]
        node = next(n for n in nodes if n.canonical_id == "node-uid-2")

        assert node.raw_attributes.get("hostname", "") == ""


class TestK8sExtractDeployments:
    def test_extract_deployments_returns_entities(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-deployments", SAMPLE_DEPLOYMENT_LIST_DATA
        )

        deploys = [e for e in result.entities if e.entity_type == "kubernetes_deployment"]
        assert len(deploys) == 1

    def test_deployment_entity_fields(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-deployments", SAMPLE_DEPLOYMENT_LIST_DATA
        )

        deploy = result.entities[0]
        assert deploy.name == "nginx-deployment"
        assert deploy.canonical_id == "deploy-uid-1"
        assert deploy.scope == {"namespace": "default"}


class TestK8sExtractServices:
    def test_extract_services_returns_entities(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-services", SAMPLE_SERVICE_LIST_DATA
        )

        services = [e for e in result.entities if e.entity_type == "kubernetes_service"]
        assert len(services) == 1

    def test_service_entity_fields(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-services", SAMPLE_SERVICE_LIST_DATA
        )

        svc = [e for e in result.entities if e.entity_type == "kubernetes_service"][0]
        assert svc.name == "nginx-svc"
        assert svc.canonical_id == "svc-uid-1"
        assert svc.scope == {"namespace": "default"}


class TestK8sExtractIngresses:
    def test_extract_ingresses_returns_entities(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-ingresses", SAMPLE_INGRESS_LIST_DATA
        )

        ingresses = [e for e in result.entities if e.entity_type == "kubernetes_ingress"]
        assert len(ingresses) == 1

    def test_ingress_routes_to_service_relationship(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-ingresses", SAMPLE_INGRESS_LIST_DATA
        )

        routes = [r for r in result.relationships if r.relationship_type == "routes_to"]
        assert len(routes) >= 1


class TestK8sExtractNamespaces:
    def test_extract_namespaces_returns_entities(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-namespaces", SAMPLE_NAMESPACE_LIST_DATA
        )

        namespaces = [e for e in result.entities if e.entity_type == "kubernetes_namespace"]
        assert len(namespaces) == 2

    def test_namespace_entity_fields(self, extractor):
        result = extractor.extract(
            "prod-cluster", "kubernetes", "list-namespaces", SAMPLE_NAMESPACE_LIST_DATA
        )

        ns = [e for e in result.entities if e.entity_type == "kubernetes_namespace"]
        default_ns = next(n for n in ns if n.canonical_id == "ns-uid-1")
        assert default_ns.name == "default"


class TestK8sExtractorEdgeCases:
    def test_unknown_operation_returns_empty(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "get-configmap", {"data": {}})

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_missing_items_key_returns_empty(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", {"data": {}})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_malformed_items_skipped(self, extractor):
        malformed_data = {
            "data": {
                "items": [
                    {"metadata": {}},  # Missing uid/name
                    {
                        "metadata": {
                            "name": "good-pod",
                            "namespace": "default",
                            "uid": "good-uid",
                        },
                        "spec": {},
                        "status": {},
                    },
                ]
            }
        }
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", malformed_data)

        # Should only extract the valid pod
        pods = [e for e in result.entities if e.entity_type == "kubernetes_pod"]
        assert len(pods) == 1
        assert pods[0].canonical_id == "good-uid"

    def test_empty_result_data(self, extractor):
        result = extractor.extract("prod-cluster", "kubernetes", "list-pods", {})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0
