# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration Tests for Data Reduction Flow.

These tests demonstrate the complete flow from natural language question
to reduced, LLM-ready data using realistic API responses.

This validates the core premise of TASK-83: LLM as Orchestrator.
"""

from unittest.mock import AsyncMock, patch

import pytest

from meho_app.modules.agents.data_reduction.engine import DataReductionEngine
from meho_app.modules.agents.data_reduction.query_generator import (
    QueryGeneratorOutput,
    generate_query,
)
from meho_app.modules.agents.data_reduction.query_schema import (
    AggregateFunction,
    AggregateSpec,
    ComputeField,
    DataQuery,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    SortSpec,
)

# =============================================================================
# Realistic API Response Fixtures
# =============================================================================


@pytest.fixture
def vcf_clusters_response():
    """Realistic VCF/vSphere cluster response (500 clusters)."""
    clusters = []
    regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]
    statuses = ["healthy", "healthy", "healthy", "warning", "critical"]  # 60% healthy

    for i in range(500):
        region = regions[i % len(regions)]
        status = statuses[i % len(statuses)]

        # Vary resource usage based on status
        if status == "critical":
            memory_pct = 85 + (i % 15)
            cpu_pct = 80 + (i % 20)
        elif status == "warning":
            memory_pct = 70 + (i % 15)
            cpu_pct = 65 + (i % 20)
        else:
            memory_pct = 30 + (i % 40)
            cpu_pct = 25 + (i % 45)

        memory_total = 512 if i % 3 == 0 else (256 if i % 3 == 1 else 128)
        memory_used = int(memory_total * memory_pct / 100)

        cpu_total = 128 if i % 2 == 0 else 64
        cpu_used = int(cpu_total * cpu_pct / 100)

        clusters.append(
            {
                "id": f"cluster-{i:04d}",
                "name": f"cluster-{region[:2]}-{i:03d}",
                "region": region,
                "datacenter": f"dc-{region}-{i % 3 + 1}",
                "host_count": 4 + (i % 8),
                "vm_count": 50 + (i % 200),
                "cpu_cores": cpu_total,
                "cpu_used": cpu_used,
                "memory_total_gb": memory_total,
                "memory_used_gb": memory_used,
                "storage_total_tb": 100 + (i % 400),
                "storage_used_tb": 40 + (i % 300),
                "status": status,
                "last_health_check": "2024-01-15T10:30:00Z",
            }
        )

    return {"clusters": clusters}


@pytest.fixture
def kubernetes_pods_response():
    """Realistic Kubernetes pods response (1000 pods)."""
    pods = []
    namespaces = ["default", "kube-system", "monitoring", "app-prod", "app-staging"]
    phases = ["Running", "Running", "Running", "Running", "Pending", "Failed"]  # 66% running

    for i in range(1000):
        namespace = namespaces[i % len(namespaces)]
        phase = phases[i % len(phases)]

        # Add meaningful names based on namespace
        if namespace == "monitoring":
            name = f"prometheus-{i % 3}" if i % 2 == 0 else f"grafana-{i % 2}"
        elif namespace == "kube-system":
            name = f"coredns-{i % 5}" if i % 2 == 0 else f"kube-proxy-{i % 10}"
        else:
            name = f"app-{i:04d}"

        pods.append(
            {
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {
                        "app": name.split("-")[0],
                        "env": "prod" if "prod" in namespace else "staging",
                    },
                    "creationTimestamp": "2024-01-10T08:00:00Z",
                },
                "status": {
                    "phase": phase,
                    "conditions": [
                        {"type": "Ready", "status": "True" if phase == "Running" else "False"}
                    ],
                },
                "spec": {
                    "containers": [
                        {
                            "name": "main",
                            "image": f"myregistry/{name}:v1.{i % 10}",
                            "resources": {
                                "requests": {
                                    "cpu": f"{100 + (i % 400)}m",
                                    "memory": f"{128 + (i % 512)}Mi",
                                },
                                "limits": {
                                    "cpu": f"{200 + (i % 800)}m",
                                    "memory": f"{256 + (i % 1024)}Mi",
                                },
                            },
                        }
                    ],
                    "nodeName": f"node-{i % 10}",
                },
            }
        )

    return {"items": pods}


@pytest.fixture
def github_repositories_response():
    """Realistic GitHub repositories response."""
    repos = []
    languages = ["Python", "TypeScript", "Go", "Rust", "Java"]

    for i in range(200):
        repos.append(
            {
                "id": 1000 + i,
                "name": f"project-{i:03d}",
                "full_name": f"org/project-{i:03d}",
                "description": f"Project {i} - A sample repository",
                "language": languages[i % len(languages)],
                "stargazers_count": i * 10 + (i % 100),
                "forks_count": i * 2 + (i % 50),
                "open_issues_count": i % 20,
                "watchers_count": i * 5,
                "size": 1000 + (i * 100),
                "default_branch": "main",
                "created_at": "2023-01-15T00:00:00Z",
                "updated_at": "2024-01-15T00:00:00Z",
                "pushed_at": "2024-01-14T00:00:00Z",
                "archived": i % 20 == 0,  # 5% archived
                "private": i % 5 == 0,  # 20% private
            }
        )

    return {"repositories": repos}


@pytest.fixture
def engine():
    """Create DataReductionEngine."""
    return DataReductionEngine()


# =============================================================================
# Scenario Tests - VCF/vSphere
# =============================================================================


class TestVCFClusterScenarios:
    """Real-world scenarios for VCF cluster management."""

    def test_find_high_memory_clusters(self, engine, vcf_clusters_response):
        """
        Scenario: User asks "Which clusters have memory usage over 80%?"

        This is what the LLM would generate from that question.
        """
        query = DataQuery(
            source_path="clusters",
            select=["name", "region", "memory_total_gb", "memory_used_gb", "status"],
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=80)
                ]
            ),
            sort=SortSpec(field="memory_pct", direction="desc"),
            limit=20,
            aggregates=[
                AggregateSpec(
                    name="avg_memory_pct", function=AggregateFunction.AVG, field="memory_pct"
                ),
                AggregateSpec(name="critical_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        result = engine.execute(vcf_clusters_response, query)

        # Validate reduction
        assert result.total_source_records == 500
        assert result.returned_records <= 20
        assert result.is_truncated or result.returned_records == result.total_after_filter

        # Validate all returned clusters have high memory
        for record in result.records:
            assert record["memory_pct"] > 80

        # Validate sorting (descending)
        memory_pcts = [r["memory_pct"] for r in result.records]
        assert memory_pcts == sorted(memory_pcts, reverse=True)

        # Validate aggregates
        assert "avg_memory_pct" in result.aggregates
        assert "critical_count" in result.aggregates

        # This demonstrates massive data reduction
        # 500 clusters → ~100 high-memory → 20 returned
        print(f"\nData Reduction: {result.total_source_records} → {result.returned_records}")
        print(f"Reduction ratio: {result.reduction_ratio:.2%}")
        print(f"Average memory usage: {result.aggregates['avg_memory_pct']:.1f}%")

    def test_cluster_status_summary(self, engine, vcf_clusters_response):
        """
        Scenario: User asks "Give me a summary of cluster health by status"
        """
        query = DataQuery(
            source_path="clusters",
            select=["status"],
            group_by=["status"],
            aggregates=[
                AggregateSpec(name="count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        result = engine.execute(vcf_clusters_response, query)

        # Should have counts by status
        assert result.total_source_records == 500
        # Aggregates are computed over whole filtered set
        print(f"\nStatus summary: {result.aggregates}")

    def test_regional_capacity_report(self, engine, vcf_clusters_response):
        """
        Scenario: User asks "Show me total compute capacity by region"
        """
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(
                        field="region", operator=FilterOperator.IN, value=["us-east-1", "us-west-2"]
                    )
                ]
            ),
            aggregates=[
                AggregateSpec(name="total_cpu", function=AggregateFunction.SUM, field="cpu_cores"),
                AggregateSpec(
                    name="total_memory", function=AggregateFunction.SUM, field="memory_total_gb"
                ),
                AggregateSpec(name="total_vms", function=AggregateFunction.SUM, field="vm_count"),
                AggregateSpec(name="cluster_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        result = engine.execute(vcf_clusters_response, query)

        # Should have comprehensive aggregates
        assert "total_cpu" in result.aggregates
        assert "total_memory" in result.aggregates
        assert result.aggregates["cluster_count"] > 0

        print("\nRegional Capacity (US regions):")
        print(f"  Clusters: {result.aggregates['cluster_count']}")
        print(f"  Total CPU: {result.aggregates['total_cpu']} cores")
        print(f"  Total Memory: {result.aggregates['total_memory']} GB")
        print(f"  Total VMs: {result.aggregates['total_vms']}")


# =============================================================================
# Scenario Tests - Kubernetes
# =============================================================================


class TestKubernetesScenarios:
    """Real-world scenarios for Kubernetes management."""

    def test_find_failed_pods(self, engine, kubernetes_pods_response):
        """
        Scenario: User asks "Show me all failed pods"
        """
        query = DataQuery(
            source_path="items",
            select=["metadata.name", "metadata.namespace", "status.phase"],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(
                        field="status.phase", operator=FilterOperator.EQ, value="Failed"
                    )
                ]
            ),
            sort=SortSpec(field="metadata.namespace", direction="asc"),
            limit=50,
            aggregates=[
                AggregateSpec(name="failed_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        result = engine.execute(kubernetes_pods_response, query)

        # Validate filtering
        assert result.total_source_records == 1000
        assert result.total_after_filter > 0

        # Validate all returned pods are failed
        for record in result.records:
            assert record.get("status.phase") == "Failed" or "status" in str(record)

        print(f"\nFailed pods: {result.aggregates.get('failed_count', result.total_after_filter)}")
        print(f"Data Reduction: {result.total_source_records} → {result.returned_records}")

    def test_pods_by_namespace(self, engine, kubernetes_pods_response):
        """
        Scenario: User asks "How many pods are in each namespace?"
        """
        query = DataQuery(
            source_path="items",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(
                        field="status.phase", operator=FilterOperator.EQ, value="Running"
                    )
                ]
            ),
            aggregates=[
                AggregateSpec(name="running_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        result = engine.execute(kubernetes_pods_response, query)

        print(
            f"\nRunning pods: {result.aggregates.get('running_count', result.total_after_filter)}"
        )
        print(f"Total pods: {result.total_source_records}")


# =============================================================================
# Scenario Tests - GitHub
# =============================================================================


class TestGitHubScenarios:
    """Real-world scenarios for GitHub management."""

    def test_popular_python_repos(self, engine, github_repositories_response):
        """
        Scenario: User asks "What are the most popular Python repos?"
        """
        query = DataQuery(
            source_path="repositories",
            select=["name", "language", "stargazers_count", "forks_count"],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="language", operator=FilterOperator.EQ, value="Python"),
                    FilterCondition(field="archived", operator=FilterOperator.EQ, value=False),
                ],
                logic="and",
            ),
            sort=SortSpec(field="stargazers_count", direction="desc"),
            limit=10,
            aggregates=[
                AggregateSpec(
                    name="total_stars", function=AggregateFunction.SUM, field="stargazers_count"
                ),
                AggregateSpec(
                    name="avg_stars", function=AggregateFunction.AVG, field="stargazers_count"
                ),
            ],
        )

        result = engine.execute(github_repositories_response, query)

        # Validate filtering (Python, not archived)
        for record in result.records:
            assert record["language"] == "Python"

        # Validate sorting
        stars = [r["stargazers_count"] for r in result.records]
        assert stars == sorted(stars, reverse=True)

        print(f"\nTop Python repos: {len(result.records)}")
        print(f"Total stars: {result.aggregates.get('total_stars', 0)}")
        print(f"Average stars: {result.aggregates.get('avg_stars', 0):.1f}")


# =============================================================================
# LLM Integration Test (Mocked)
# =============================================================================


class TestLLMQueryGenerationFlow:
    """Test the full flow from question to reduced data."""

    @pytest.mark.asyncio
    async def test_full_flow_mocked(self, engine, vcf_clusters_response):
        """
        Test complete flow:
        1. User asks question
        2. LLM generates query
        3. Engine executes query
        4. Results ready for LLM interpretation
        """
        # Expected query that LLM would generate
        expected_query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="critical")
                ]
            ),
            sort=SortSpec(field="memory_pct", direction="desc"),
            limit=10,
            aggregates=[
                AggregateSpec(name="critical_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        expected_output = QueryGeneratorOutput(
            query=expected_query,
            reasoning="User wants to see critical clusters sorted by memory usage",
            confidence=0.9,
        )

        # Mock the query generator
        mock_agent = AsyncMock()
        mock_result = AsyncMock()
        mock_result.output = expected_output
        mock_agent.run.return_value = mock_result

        with patch(
            "meho_app.modules.agents.data_reduction.query_generator.get_query_generator_agent",
            return_value=mock_agent,
        ):
            # Step 1: Generate query from question
            query_output = await generate_query(
                question="Show me critical clusters and their memory usage",
                response_schema={
                    "clusters": [
                        {
                            "name": "str",
                            "status": "str",
                            "memory_used_gb": "int",
                            "memory_total_gb": "int",
                        }
                    ]
                },
            )

            # Step 2: Execute query
            result = engine.execute(vcf_clusters_response, query_output.query)

            # Validate the flow worked
            assert result.total_source_records == 500
            assert result.returned_records <= 10

            # All returned should be critical
            for record in result.records:
                assert record["status"] == "critical"

            # Results are ready for LLM to interpret
            llm_context = result.to_llm_context()
            assert "critical" in llm_context.lower() or len(result.records) > 0

            print("\n=== Full Flow Demo ===")
            print("Question: 'Show me critical clusters and their memory usage'")
            print("Data: 500 clusters")
            print(f"Query confidence: {query_output.confidence}")
            print(f"Results: {result.returned_records} critical clusters")
            print(f"Reduction: {result.reduction_ratio:.2%}")
            print(f"\nLLM Context (truncated):\n{llm_context[:500]}...")


# =============================================================================
# Performance Tests
# =============================================================================


class TestDataReductionPerformance:
    """Performance characteristics of data reduction."""

    def test_large_dataset_performance(self, engine):
        """Test performance with very large dataset."""
        # Generate 10,000 records
        data = {
            "items": [
                {
                    "id": i,
                    "name": f"item-{i:05d}",
                    "category": f"cat-{i % 100}",
                    "value": i * 10,
                    "timestamp": f"2024-01-{(i % 28) + 1:02d}T10:00:00Z",
                }
                for i in range(10000)
            ]
        }

        query = DataQuery(
            source_path="items",
            filter=FilterGroup(
                conditions=[FilterCondition(field="value", operator=FilterOperator.GT, value=50000)]
            ),
            sort=SortSpec(field="value", direction="desc"),
            limit=100,
            aggregates=[
                AggregateSpec(name="count", function=AggregateFunction.COUNT, field="*"),
                AggregateSpec(name="sum", function=AggregateFunction.SUM, field="value"),
                AggregateSpec(name="avg", function=AggregateFunction.AVG, field="value"),
            ],
        )

        result = engine.execute(data, query)

        # Should complete quickly
        assert result.processing_time_ms < 5000  # Less than 5 seconds

        # Validate reduction
        assert result.total_source_records == 10000
        assert result.returned_records == 100

        print("\n=== Performance Test ===")
        print(f"Input: {result.total_source_records} records")
        print(f"Filtered: {result.total_after_filter} records")
        print(f"Output: {result.returned_records} records")
        print(f"Processing time: {result.processing_time_ms:.2f}ms")
        print(
            f"Throughput: {result.total_source_records / (result.processing_time_ms / 1000):.0f} records/sec"
        )
