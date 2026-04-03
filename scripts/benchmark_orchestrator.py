#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Orchestrator Performance Benchmark Script (TASK-181)

Measures and compares performance metrics for the orchestrator agent:
- TTFUR (Time to First Useful Response)
- Total execution time
- Routing decision time

Usage:
    # Benchmark with default settings
    python scripts/benchmark_orchestrator.py

    # Benchmark with specific query
    python scripts/benchmark_orchestrator.py --query "Show me all VMs"

    # Compare with legacy agent
    python scripts/benchmark_orchestrator.py --compare

    # Run multiple iterations
    python scripts/benchmark_orchestrator.py --iterations 5

Requirements:
    - Backend running at localhost:8000
    - At least one active connector configured
    - Valid authentication token

Performance Targets:
    - TTFUR (1 connector): < 5s
    - TTFUR (3 connectors): < 7s
    - Total time (1 connector): < 10s
    - Total time (3 connectors): < 15s
    - Routing decision: < 2s
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

# Configuration
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_QUERY = "What is the status of my systems?"
BENCHMARK_QUERIES = [
    "What is the status of my systems?",
    "Show me all running containers",
    "List the top 5 resource-consuming workloads",
]


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    query: str
    ttfur_ms: float | None = None  # Time to first useful response
    total_time_ms: float = 0.0
    routing_time_ms: float | None = None
    connectors_queried: list[str] = field(default_factory=list)
    iterations: int = 0
    success: bool = False
    error: str | None = None
    is_orchestrator: bool = True
    partial: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "query": self.query[:50] + "..." if len(self.query) > 50 else self.query,
            "ttfur_ms": round(self.ttfur_ms, 1) if self.ttfur_ms else None,
            "total_time_ms": round(self.total_time_ms, 1),
            "routing_time_ms": round(self.routing_time_ms, 1) if self.routing_time_ms else None,
            "connectors": len(self.connectors_queried),
            "iterations": self.iterations,
            "success": self.success,
            "partial": self.partial,
            "is_orchestrator": self.is_orchestrator,
        }


@dataclass
class BenchmarkSummary:
    """Summary of multiple benchmark runs."""

    results: list[BenchmarkResult]
    orchestrator_enabled: bool = True

    @property
    def avg_ttfur_ms(self) -> float | None:
        """Average TTFUR across all runs."""
        ttfurs = [r.ttfur_ms for r in self.results if r.ttfur_ms is not None and r.success]
        return statistics.mean(ttfurs) if ttfurs else None

    @property
    def avg_total_time_ms(self) -> float:
        """Average total time across all runs."""
        times = [r.total_time_ms for r in self.results if r.success]
        return statistics.mean(times) if times else 0.0

    @property
    def avg_routing_time_ms(self) -> float | None:
        """Average routing time across all runs."""
        times = [r.routing_time_ms for r in self.results if r.routing_time_ms is not None and r.success]
        return statistics.mean(times) if times else None

    @property
    def success_rate(self) -> float:
        """Success rate (0.0 - 1.0)."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.success) / len(self.results)

    def print_summary(self) -> None:
        """Print formatted summary to stdout."""
        mode = "Orchestrator" if self.orchestrator_enabled else "Legacy Agent"
        print(f"\n{'=' * 60}")
        print(f"BENCHMARK SUMMARY - {mode}")
        print(f"{'=' * 60}")
        print(f"Total runs: {len(self.results)}")
        print(f"Success rate: {self.success_rate * 100:.1f}%")
        print(f"\nPerformance Metrics:")
        print(f"  Avg TTFUR: {self.avg_ttfur_ms:.1f}ms" if self.avg_ttfur_ms else "  Avg TTFUR: N/A")
        print(f"  Avg Total Time: {self.avg_total_time_ms:.1f}ms")
        if self.avg_routing_time_ms:
            print(f"  Avg Routing Time: {self.avg_routing_time_ms:.1f}ms")
        
        # Check against targets
        print(f"\nTarget Comparison:")
        if self.avg_ttfur_ms:
            ttfur_target = 5000  # 5s
            status = "✓" if self.avg_ttfur_ms < ttfur_target else "✗"
            print(f"  {status} TTFUR < 5s: {self.avg_ttfur_ms:.1f}ms")
        
        total_target = 10000  # 10s
        status = "✓" if self.avg_total_time_ms < total_target else "✗"
        print(f"  {status} Total < 10s: {self.avg_total_time_ms:.1f}ms")
        
        if self.avg_routing_time_ms:
            routing_target = 2000  # 2s
            status = "✓" if self.avg_routing_time_ms < routing_target else "✗"
            print(f"  {status} Routing < 2s: {self.avg_routing_time_ms:.1f}ms")
        
        print(f"{'=' * 60}\n")


async def get_auth_token(_api_url: str) -> str:
    """Get authentication token from environment or prompt."""
    import asyncio

    token = os.environ.get("MEHO_TOKEN")
    if token:
        return token

    # Try to read from .env file
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    def _read_env_token() -> str:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("MEHO_TOKEN="):
                        return line.split("=", 1)[1].strip()
        return ""

    env_token = await asyncio.to_thread(_read_env_token)
    if env_token:
        return env_token

    print("Warning: No MEHO_TOKEN found. Benchmark may fail without authentication.")
    return ""


async def run_benchmark(  # NOSONAR (cognitive complexity)
    api_url: str,
    query: str,
    token: str,
    use_orchestrator: bool = True,
) -> BenchmarkResult:
    """Run a single benchmark against the chat API.
    
    Args:
        api_url: Base API URL
        query: Query to send
        token: Authentication token
        use_orchestrator: Whether to use orchestrator mode
    
    Returns:
        BenchmarkResult with timing metrics
    """
    result = BenchmarkResult(query=query, is_orchestrator=use_orchestrator)
    
    headers = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    payload = {"message": query}
    
    start_time = time.perf_counter()
    first_useful_time: float | None = None
    routing_start_time: float | None = None
    routing_end_time: float | None = None
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{api_url}/api/chat/stream",
                json=payload,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    result.error = f"HTTP {response.status_code}"
                    result.total_time_ms = (time.perf_counter() - start_time) * 1000
                    return result
                
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    
                    try:
                        data = json.loads(line[6:])
                        event_type = data.get("type")
                        
                        # Track routing time
                        if event_type == "iteration_start":
                            routing_start_time = time.perf_counter()
                        elif event_type == "dispatch_start" and routing_start_time:
                            routing_end_time = time.perf_counter()
                            result.routing_time_ms = (routing_end_time - routing_start_time) * 1000
                        
                        # Track TTFUR (first early_findings or final_answer)
                        if event_type in ("early_findings", "final_answer") and first_useful_time is None:
                            first_useful_time = time.perf_counter()
                            result.ttfur_ms = (first_useful_time - start_time) * 1000
                        
                        # Track completion
                        if event_type == "orchestrator_complete":
                            data_payload = data.get("data", {})
                            result.iterations = data_payload.get("iterations", 0)
                            result.success = data_payload.get("success", False)
                            result.partial = data_payload.get("partial", False)
                        
                        if event_type == "final_answer":
                            data_payload = data.get("data", data)
                            result.connectors_queried = data_payload.get("connectors_queried", [])
                            if not result.success:  # Legacy agent
                                result.success = True
                        
                        # Legacy agent done event
                        if event_type == "done":
                            result.success = True
                        
                    except json.JSONDecodeError:
                        continue
        
        result.total_time_ms = (time.perf_counter() - start_time) * 1000
        
    except Exception as e:
        result.error = str(e)
        result.total_time_ms = (time.perf_counter() - start_time) * 1000
    
    return result


async def run_benchmark_suite(
    api_url: str,
    queries: list[str],
    iterations: int,
    token: str,
    use_orchestrator: bool = True,
) -> BenchmarkSummary:
    """Run multiple benchmark iterations.
    
    Args:
        api_url: Base API URL
        queries: List of queries to test
        iterations: Number of times to run each query
        token: Authentication token
        use_orchestrator: Whether to use orchestrator
    
    Returns:
        BenchmarkSummary with all results
    """
    results: list[BenchmarkResult] = []
    
    mode = "orchestrator" if use_orchestrator else "legacy"
    total_runs = len(queries) * iterations
    current = 0
    
    for query in queries:
        for i in range(iterations):
            current += 1
            print(f"[{current}/{total_runs}] Running {mode} benchmark: {query[:30]}...")
            
            result = await run_benchmark(api_url, query, token, use_orchestrator)
            results.append(result)
            
            if result.success:
                print(f"  ✓ TTFUR: {result.ttfur_ms:.1f}ms, Total: {result.total_time_ms:.1f}ms")
            else:
                print(f"  ✗ Failed: {result.error or 'Unknown error'}")
            
            # Small delay between runs to avoid overwhelming the server
            await asyncio.sleep(0.5)
    
    return BenchmarkSummary(results=results, orchestrator_enabled=use_orchestrator)


def compare_summaries(orchestrator: BenchmarkSummary, legacy: BenchmarkSummary) -> None:
    """Print comparison between orchestrator and legacy agent."""
    print(f"\n{'=' * 60}")
    print("COMPARISON: Orchestrator vs Legacy Agent")
    print(f"{'=' * 60}")
    
    # TTFUR comparison
    if orchestrator.avg_ttfur_ms and legacy.avg_ttfur_ms:
        diff = orchestrator.avg_ttfur_ms - legacy.avg_ttfur_ms
        better = "orchestrator" if diff < 0 else "legacy"
        print(f"\nTTFUR:")
        print(f"  Orchestrator: {orchestrator.avg_ttfur_ms:.1f}ms")
        print(f"  Legacy: {legacy.avg_ttfur_ms:.1f}ms")
        print(f"  Winner: {better} ({abs(diff):.1f}ms faster)")
    
    # Total time comparison
    diff = orchestrator.avg_total_time_ms - legacy.avg_total_time_ms
    better = "orchestrator" if diff < 0 else "legacy"
    print(f"\nTotal Time:")
    print(f"  Orchestrator: {orchestrator.avg_total_time_ms:.1f}ms")
    print(f"  Legacy: {legacy.avg_total_time_ms:.1f}ms")
    print(f"  Winner: {better} ({abs(diff):.1f}ms faster)")
    
    # Success rate comparison
    print(f"\nSuccess Rate:")
    print(f"  Orchestrator: {orchestrator.success_rate * 100:.1f}%")
    print(f"  Legacy: {legacy.success_rate * 100:.1f}%")
    
    print(f"{'=' * 60}\n")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Benchmark the MEHO orchestrator agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"API URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--query",
        help="Custom query to benchmark (default: use preset queries)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per query (default: 3)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare orchestrator with legacy agent",
    )
    parser.add_argument(
        "--output",
        help="Output file for JSON results",
    )
    
    args = parser.parse_args()
    
    # Get auth token
    token = await get_auth_token(args.api_url)
    
    # Determine queries to run
    queries = [args.query] if args.query else BENCHMARK_QUERIES
    
    print(f"\nMEHO Orchestrator Benchmark")
    print(f"API URL: {args.api_url}")
    print(f"Queries: {len(queries)}")
    print(f"Iterations: {args.iterations}")
    print(f"Total runs: {len(queries) * args.iterations}")
    if args.compare:
        print("Mode: Compare orchestrator vs legacy")
    print()
    
    # Run orchestrator benchmark
    print("Running orchestrator benchmarks...")
    orchestrator_summary = await run_benchmark_suite(
        args.api_url, queries, args.iterations, token, use_orchestrator=True
    )
    orchestrator_summary.print_summary()
    
    # Optionally compare with legacy
    legacy_summary: BenchmarkSummary | None = None
    if args.compare:
        print("Running legacy agent benchmarks...")
        legacy_summary = await run_benchmark_suite(
            args.api_url, queries, args.iterations, token, use_orchestrator=False
        )
        legacy_summary.print_summary()
        compare_summaries(orchestrator_summary, legacy_summary)
    
    # Output to file if requested
    if args.output:
        output = {
            "timestamp": datetime.now().isoformat(),
            "config": {
                "api_url": args.api_url,
                "queries": queries,
                "iterations": args.iterations,
            },
            "orchestrator": {
                "results": [r.to_dict() for r in orchestrator_summary.results],
                "avg_ttfur_ms": orchestrator_summary.avg_ttfur_ms,
                "avg_total_time_ms": orchestrator_summary.avg_total_time_ms,
                "avg_routing_time_ms": orchestrator_summary.avg_routing_time_ms,
                "success_rate": orchestrator_summary.success_rate,
            },
        }
        if legacy_summary:
            output["legacy"] = {
                "results": [r.to_dict() for r in legacy_summary.results],
                "avg_ttfur_ms": legacy_summary.avg_ttfur_ms,
                "avg_total_time_ms": legacy_summary.avg_total_time_ms,
                "success_rate": legacy_summary.success_rate,
            }
        
        output_json = json.dumps(output, indent=2)
        await asyncio.to_thread(Path(args.output).write_text, output_json)
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
