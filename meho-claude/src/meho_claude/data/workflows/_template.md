---
name: my-workflow
description: Brief description of what this workflow does
budget: 15
version: "1.1"
tags:
  - custom
---

# Workflow Name

## Objective
Describe what this workflow accomplishes and when to use it.

## Prerequisites
List any required setup (connectors, topology data, etc.):
- `meho connector list` should show at least one connector

## Visual Input (Optional)

If the user provides a screenshot, image, or document relevant to this workflow:

1. **Analyze the visual input** to extract information relevant to this workflow's purpose
2. **Extract actionable data**: entity names, error codes, IP addresses, status indicators, metric values
3. **Map extracted information** to meho commands:
   - Look up entities: `meho topology lookup "<entity>"`
   - Search knowledge: `meho knowledge search "<error or topic>"`
   - Check memory: `meho memory search "<pattern>"`

If the user provides architecture diagrams, runbooks, or other documents, read them directly for workflow context. Do NOT ingest them into the knowledge base -- they are ad-hoc context for this workflow execution only.

If no visual input is provided, proceed directly to Step 1.

## Budget
Maximum **15 connector calls** per execution. Adjust as needed.

## Steps

### Step 1: First Step
Describe what to do and why.

```bash
meho connector call <connector> <operation>
```

### Step 2: Second Step
Continue the investigation or process.

### Step 3: Synthesize Results
Combine findings into a conclusion.

## Output Format

```json
{
  "status": "workflow_complete",
  "findings": []
}
```
