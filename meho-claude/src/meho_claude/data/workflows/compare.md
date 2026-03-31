---
name: compare
description: Cross-environment resource comparison
budget: 10
version: "1.1"
tags:
  - comparison
  - drift-detection
---

# Cross-Environment Resource Comparison Workflow

## Objective
Compare the same resource type across two environments or connectors to detect drift, discrepancies, or configuration differences.

## Prerequisites
- At least two connectors configured: `meho connector list`
- Both target environments accessible

## Visual Input

If the user provides screenshots from two environments (e.g., two dashboards, two configuration pages, two terminal outputs), analyze both images to extract resource names, configuration values, and status indicators. Use the visual differences as starting points for the comparison investigation.

If the user provides architecture diagrams, runbooks, or other documents, read them directly for context on expected configuration and topology differences. Do NOT ingest them into the knowledge base -- they are ad-hoc context for this comparison only.

## Budget
Maximum **10 connector calls** per comparison. Track your count.

## Steps

### Step 1: Identify Comparison Target
Parse the user's request to determine:
1. **Resource type** to compare (e.g., pods, VMs, services, deployments)
2. **Environment A** (first connector/namespace/cluster)
3. **Environment B** (second connector/namespace/cluster)

### Step 2: Query Environment A
Find and execute the appropriate list/get operation:

```bash
meho connector search-ops "list <resource type>" --connector <connector-a>
meho connector call <connector-a> <list-operation>
```

### Step 3: Query Environment B
Run the same operation against the second environment:

```bash
meho connector search-ops "list <resource type>" --connector <connector-b>
meho connector call <connector-b> <list-operation>
```

### Step 4: Compare and Diff
Analyze the results from both environments:

1. **Count differences**: Different number of resources
2. **Missing resources**: Present in one environment but not the other
3. **Configuration drift**: Same resource exists in both but with different settings
4. **State differences**: Same resource, different operational states

### Step 5: Report Discrepancies
Highlight significant differences with context on potential impact.

## Output Format

```json
{
  "status": "comparison_complete",
  "query_budget": {"used": 0, "max": 10},
  "environment_a": {
    "connector": "connector-name",
    "resource_type": "type",
    "count": 0
  },
  "environment_b": {
    "connector": "connector-name",
    "resource_type": "type",
    "count": 0
  },
  "differences": [
    {
      "resource": "name",
      "type": "missing|drift|state",
      "environment_a": "value or 'absent'",
      "environment_b": "value or 'absent'",
      "impact": "description"
    }
  ],
  "summary": "Overall comparison assessment"
}
```

### For Human (narrative mode)
Present a side-by-side comparison table showing:
- Resources only in Environment A
- Resources only in Environment B
- Resources with differences, highlighting the specific fields that differ
