#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test script for intent classifier.

Tests the classifier against various user messages to verify
correct intent detection and tool gating.
"""

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meho_agent.intent_classifier import (
    classify_intent,
    CachedDataSummary,
    Intent,
    get_available_tools,
)


# Test cases: (message, has_data, expected_intent, expected_requires_api)
TEST_CASES = [
    # REFORMAT cases (has data, no API needed)
    ("Show that as a table", True, Intent.REFORMAT, False),
    ("Format as CSV", True, Intent.REFORMAT, False),
    ("Filter to just powered on VMs", True, Intent.REFORMAT, False),
    ("Summarize the results", True, Intent.REFORMAT, False),
    
    # RECALL cases (has data, answer from memory)
    ("What was the first VM?", True, Intent.RECALL, False),
    ("How many VMs were there?", True, Intent.RECALL, False),
    ("What was the name of the third one?", True, Intent.RECALL, False),
    
    # FETCH_BATCH cases (need new data for multiple items)
    ("Get IPs for all of them", True, Intent.FETCH_BATCH, True),
    ("Get details for all VMs", False, Intent.FETCH_BATCH, True),
    ("List all pods", False, Intent.FETCH_BATCH, True),
    
    # FETCH_SINGLE cases
    ("Get details for vm-107", True, Intent.FETCH_SINGLE, True),
    ("Check status of sfo-m01-vc01", True, Intent.FETCH_SINGLE, True),
    
    # CLARIFY cases
    ("Check the system", False, Intent.CLARIFY, False),
    ("What's going on?", False, Intent.CLARIFY, False),
    
    # SWITCH_SYSTEM cases
    ("Now check Kubernetes", True, Intent.SWITCH_SYSTEM, False),
    ("Switch to ArgoCD", True, Intent.SWITCH_SYSTEM, False),
    
    # SEARCH cases
    ("How do I create a VM?", False, Intent.SEARCH, False),
    ("What is a pod?", False, Intent.SEARCH, False),
]


async def run_tests():
    """Run all test cases and report results."""
    print("=" * 60)
    print("Intent Classifier Tests")
    print("=" * 60)
    print()
    
    passed = 0
    failed = 0
    
    for message, has_data, expected_intent, expected_requires_api in TEST_CASES:
        # Build cached data summary
        if has_data:
            cached = CachedDataSummary(
                has_data=True,
                data_type="vm_list",
                item_count=25,
                entity_names=["vidm-primary", "vrava-primary", "sfo-m01-vc01"],
                last_action="Listed 25 VMs from vCenter"
            )
        else:
            cached = CachedDataSummary(has_data=False)
        
        try:
            result = await classify_intent(message, cached)
            
            # Check results
            intent_match = result.intent == expected_intent
            api_match = result.requires_api == expected_requires_api
            
            if intent_match and api_match:
                status = "✅ PASS"
                passed += 1
            else:
                status = "❌ FAIL"
                failed += 1
            
            print(f"{status} | \"{message}\"")
            print(f"       Expected: {expected_intent.value}, requires_api={expected_requires_api}")
            print(f"       Got:      {result.intent.value}, requires_api={result.requires_api}")
            print(f"       Reason:   {result.reasoning[:80]}...")
            print(f"       Tools:    {get_available_tools(result.intent)}")
            print()
            
        except Exception as e:
            print(f"❌ ERROR | \"{message}\"")
            print(f"       Error: {e}")
            print()
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(TEST_CASES)}")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)

