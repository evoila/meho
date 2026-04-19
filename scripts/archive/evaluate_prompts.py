#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prompt Engineering Evaluation Harness for TASK-86

Usage:
    python scripts/evaluate_prompts.py --variant a  # Test Variant A
    python scripts/evaluate_prompts.py --all        # Test all variants
    python scripts/evaluate_prompts.py --scenario 1 # Test specific scenario

Requires:
    - Running MEHO services (docker-compose up)
    - Valid test token
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

VARIANTS_DIR = Path(__file__).parent.parent / "config" / "prompts" / "variants"
RESULTS_DIR = Path(__file__).parent.parent / "config" / "prompts" / "evaluation_results"

# Test scenarios (from test_scenarios.md)
TEST_SCENARIOS = {
    "S1": {
        "name": "Simple List Query",
        "messages": ["List all VMs from the vCenter"],
        "success_criteria": [
            "Uses determine_connector",
            "Single call_endpoint for listing",
            "Clean formatted output",
        ],
        "failure_indicators": [
            "Multiple call_endpoint calls",
            "No connector determination",
        ],
    },
    "S2": {
        "name": "Batch Detail Query",
        "messages": [
            "List all VMs from the vCenter",
            "Get IP addresses for all of them",
        ],
        "success_criteria": [
            "Uses batch_get_endpoint",
            "Single approval request",
            "Correlates results with VM names",
        ],
        "failure_indicators": [
            "Multiple call_endpoint calls",
            "Re-lists VMs unnecessarily",
        ],
    },
    "S3": {
        "name": "Reformat Request",
        "messages": [
            "List all VMs from the vCenter",
            "Format that as a CSV table",
        ],
        "success_criteria": [
            "Uses interpret_results",
            "Zero API calls for reformatting",
            "Correct CSV format",
        ],
        "failure_indicators": [
            "call_endpoint for reformatting",
            "Re-fetches data",
        ],
    },
    "S4": {
        "name": "System Switch",
        "messages": [
            "List all VMs from the vCenter",
            "Now check the Kubernetes cluster for pods",
        ],
        "success_criteria": [
            "Calls determine_connector for K8s",
            "Uses different connector",
            "Clear indication of switch",
        ],
        "failure_indicators": [
            "Reuses vCenter connector",
            "Calls vCenter API for K8s",
        ],
    },
    "S5": {
        "name": "Complex Multi-Step",
        "messages": ["Show me all unhealthy pods and their container logs"],
        "success_criteria": [
            "Lists pods first",
            "Filters for unhealthy",
            "Uses batch_get_endpoint for logs",
            "Provides summary",
        ],
        "failure_indicators": [
            "Multiple call_endpoint for logs",
            "Doesn't filter",
        ],
    },
    "S7": {
        "name": "Ambiguous Request",
        "messages": ["Check the system"],
        "success_criteria": [
            "Asks for clarification",
            "Lists available systems",
            "Waits for user selection",
        ],
        "failure_indicators": [
            "Guesses wrong system",
            "Proceeds without asking",
        ],
    },
    "S9": {
        "name": "Large Response",
        "messages": ["List all endpoints available"],  # Typically 500+ items
        "success_criteria": [
            "Summarizes count",
            "Shows representative sample",
            "Offers to filter",
        ],
        "failure_indicators": [
            "Dumps all items",
            "Overwhelming output",
        ],
    },
    "S10": {
        "name": "Context Recall",
        "messages": [
            "List all VMs from the vCenter",
            "What was the name of the first VM?",
        ],
        "success_criteria": [
            "No new API calls",
            "Uses conversation history",
            "Correct answer",
        ],
        "failure_indicators": [
            "Re-fetches VM list",
            "Makes API call",
        ],
    },
}


def load_variant(variant_id: str) -> str:
    """Load a prompt variant from file."""
    variant_map = {
        "current": Path(__file__).parent.parent / "config" / "prompts" / "base_system_prompt.md",
        "a": VARIANTS_DIR / "variant_a_minimal.md",
        "b": VARIANTS_DIR / "variant_b_structured.md",
        "c": VARIANTS_DIR / "variant_c_conversational.md",
        "d": VARIANTS_DIR / "variant_d_rules.md",
        "e": VARIANTS_DIR / "variant_e_tool_centric.md",
    }
    
    path = variant_map.get(variant_id.lower())
    if not path or not path.exists():
        raise ValueError(f"Unknown variant: {variant_id}")
    
    return path.read_text()


def count_lines(prompt: str) -> int:
    """Count lines in prompt."""
    return len(prompt.strip().split("\n"))


def count_tokens_estimate(prompt: str) -> int:
    """Rough token count estimate (chars / 4)."""
    return len(prompt) // 4


class PromptEvaluator:
    """Evaluates prompt variants against test scenarios."""
    
    def __init__(self, api_base: str = "http://localhost:8000"):
        self.api_base = api_base
        self.results: Dict[str, Any] = {}
    
    async def get_test_token(self) -> str:
        """Get a test authentication token."""
        import httpx
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.api_base}/api/auth/test-token",
                json={"user_id": "eval-user@test.com", "tenant_id": "demo-tenant"},
            )
            resp.raise_for_status()
            return resp.json()["token"]
    
    async def run_chat(
        self,
        messages: List[str],
        prompt: str,
        token: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a chat conversation with the given prompt."""
        import httpx
        
        # For now, we simulate - actual implementation would need to inject prompt
        # This is a placeholder for manual testing
        
        result = {
            "messages": messages,
            "prompt_lines": count_lines(prompt),
            "prompt_tokens_est": count_tokens_estimate(prompt),
            "tool_calls": [],
            "responses": [],
            "requires_manual_evaluation": True,
        }
        
        return result
    
    def evaluate_scenario(
        self,
        scenario_id: str,
        tool_calls: List[str],
        responses: List[str],
    ) -> Dict[str, Any]:
        """Evaluate a scenario result against criteria."""
        scenario = TEST_SCENARIOS.get(scenario_id, {})
        
        score = 5  # Start neutral
        notes = []
        
        # Check success criteria
        for criterion in scenario.get("success_criteria", []):
            # Simplified check - in reality would be more sophisticated
            notes.append(f"Check: {criterion}")
        
        # Check failure indicators
        for indicator in scenario.get("failure_indicators", []):
            notes.append(f"Avoid: {indicator}")
        
        return {
            "scenario": scenario_id,
            "name": scenario.get("name", "Unknown"),
            "score": score,
            "notes": notes,
            "manual_review_needed": True,
        }


def generate_evaluation_sheet() -> str:
    """Generate a markdown evaluation sheet for manual testing."""
    
    lines = [
        "# Prompt Variant Evaluation Sheet",
        "",
        f"**Generated:** {datetime.now().isoformat()}",
        "",
        "## Instructions",
        "",
        "1. Run each scenario manually in the MEHO chat",
        "2. Score each variant 0-10 based on criteria",
        "3. Note specific successes/failures",
        "4. Calculate totals at the end",
        "",
        "---",
        "",
    ]
    
    # Variant overview
    lines.extend([
        "## Variants Overview",
        "",
        "| Variant | Lines | Approach |",
        "|---------|-------|----------|",
        "| Current | 324 | Full with all rules |",
        "| A: Minimal | 32 | Essentials only |",
        "| B: Structured | 96 | XML-style sections |",
        "| C: Conversational | 56 | Friendly guide |",
        "| D: Rule-Based | 102 | MUST/SHOULD/MAY |",
        "| E: Tool-Centric | 177 | Organized by tools |",
        "",
        "---",
        "",
    ])
    
    # Scenarios
    for scenario_id, scenario in TEST_SCENARIOS.items():
        lines.extend([
            f"## {scenario_id}: {scenario['name']}",
            "",
            "**Test Messages:**",
        ])
        for msg in scenario["messages"]:
            lines.append(f"1. \"{msg}\"")
        
        lines.extend([
            "",
            "**Success Criteria:**",
        ])
        for criterion in scenario["success_criteria"]:
            lines.append(f"- [ ] {criterion}")
        
        lines.extend([
            "",
            "**Failure Indicators:**",
        ])
        for indicator in scenario["failure_indicators"]:
            lines.append(f"- [ ] {indicator}")
        
        lines.extend([
            "",
            "**Scores:**",
            "",
            "| Variant | Score (0-10) | Notes |",
            "|---------|--------------|-------|",
            "| Current | | |",
            "| A | | |",
            "| B | | |",
            "| C | | |",
            "| D | | |",
            "| E | | |",
            "",
            "---",
            "",
        ])
    
    # Summary
    lines.extend([
        "## Summary",
        "",
        "| Variant | S1 | S2 | S3 | S4 | S5 | S7 | S9 | S10 | **Total** |",
        "|---------|----|----|----|----|----|----|----|----|-----------|",
        "| Current | | | | | | | | | |",
        "| A | | | | | | | | | |",
        "| B | | | | | | | | | |",
        "| C | | | | | | | | | |",
        "| D | | | | | | | | | |",
        "| E | | | | | | | | | |",
        "",
        "## Winner Selection",
        "",
        "**Best Overall:** ___",
        "",
        "**Best Elements to Keep:**",
        "- From A: ___",
        "- From B: ___",
        "- From C: ___",
        "- From D: ___",
        "- From E: ___",
        "",
        "## Final Prompt Notes",
        "",
        "(Notes for creating the final optimized prompt)",
        "",
    ])
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Evaluate prompt variants")
    parser.add_argument("--variant", "-v", help="Variant to test (a/b/c/d/e/current)")
    parser.add_argument("--all", action="store_true", help="Test all variants")
    parser.add_argument("--scenario", "-s", help="Specific scenario to test")
    parser.add_argument("--generate-sheet", action="store_true", help="Generate evaluation sheet")
    parser.add_argument("--show", action="store_true", help="Show prompt content")
    
    args = parser.parse_args()
    
    if args.generate_sheet:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        sheet = generate_evaluation_sheet()
        output_path = RESULTS_DIR / "evaluation_sheet.md"
        output_path.write_text(sheet)
        print(f"✅ Generated evaluation sheet: {output_path}")
        return
    
    if args.show and args.variant:
        prompt = load_variant(args.variant)
        print(f"=== Variant {args.variant.upper()} ===")
        print(f"Lines: {count_lines(prompt)}")
        print(f"Est. tokens: {count_tokens_estimate(prompt)}")
        print("=" * 50)
        print(prompt)
        return
    
    # Default: show comparison
    print("📊 Prompt Variant Comparison")
    print("=" * 50)
    
    variants = ["current", "a", "b", "c", "d", "e"]
    
    for v in variants:
        try:
            prompt = load_variant(v)
            lines = count_lines(prompt)
            tokens = count_tokens_estimate(prompt)
            
            name = {
                "current": "Current (baseline)",
                "a": "A: Minimal",
                "b": "B: Structured",
                "c": "C: Conversational",
                "d": "D: Rule-Based",
                "e": "E: Tool-Centric",
            }.get(v, v)
            
            reduction = ((324 - lines) / 324 * 100) if v != "current" else 0
            
            print(f"{name:25} | {lines:3} lines | ~{tokens:4} tokens | -{reduction:.0f}%")
        except Exception as e:
            print(f"{v:25} | ERROR: {e}")
    
    print("=" * 50)
    print("\nTo generate evaluation sheet: python scripts/evaluate_prompts.py --generate-sheet")
    print("To show a variant:           python scripts/evaluate_prompts.py --variant a --show")


if __name__ == "__main__":
    main()

