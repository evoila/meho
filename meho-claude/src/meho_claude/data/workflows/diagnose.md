---
name: diagnose
description: Cross-system diagnostic investigation
budget: 15
version: "1.1"
tags:
  - diagnosis
  - cross-system
---

# Cross-System Diagnosis Workflow

## Objective
Trace an infrastructure problem across connected systems using topology graph traversal and connector queries. Identify root cause with confidence level.

## Prerequisites
- At least one connector configured: `meho connector list`
- Topology populated via prior connector calls

## Budget
Maximum **15 connector calls** per investigation. Track your count.

## Visual Input

If the user provides a screenshot, image, or document as part of this investigation:

1. **Analyze the visual input** to extract:
   - Entity names or IDs (pod names, VM names, service names, hostnames)
   - Error codes or error messages
   - IP addresses, URLs, or port numbers
   - Status indicators (health states, colors, icons)
   - Metric values showing anomalies (CPU, memory, latency, error rates)
   - Timestamps of events

2. **Map extracted entities** to meho commands:
   ```bash
   meho topology lookup "<extracted entity name or ID>"
   ```

3. **Search for extracted errors** in knowledge base:
   ```bash
   meho knowledge search "<extracted error code or message>"
   ```

4. **Check memory** for similar patterns:
   ```bash
   meho memory search "<extracted error description>"
   ```

If the user provides architecture diagrams, runbooks, or other documents, read them directly for investigation context. Do NOT ingest them into the knowledge base -- they are ad-hoc context for this investigation only.

## Steps

### Step 0: Analyze Visual Input (if provided)

If the user provided visual input (screenshot, image, document) with their problem description, analyze it now using the extraction steps above. Use the extracted information to inform Step 1 (Identify Starting Entity). If no visual input was provided, proceed directly to Step 1.

### Step 1: Identify Starting Entity
Parse the problem description to identify the target entity (pod, VM, service, host, etc.).

```bash
meho topology lookup "<entity name or keyword>"
```

- **Single match**: Proceed to Step 2 with this entity.
- **Multiple matches**: Show candidates to user, ask for disambiguation.
- **No match**: Proceed to Step 1b (cold-start).

#### Step 1b: Cold-Start Sweep
Entity not in topology. Query likely connectors directly:

```bash
meho connector search-ops "list <resource type>"
meho connector call <connector> <list-operation>
```
This populates topology. Retry Step 1.

### Step 1.5: Check Knowledge Base
Search the knowledge base for relevant documentation, runbooks, or architecture details:

```bash
meho knowledge search "<entity type> <problem description>" --connector <connector-name> --limit 3
```

Review matches for:
- Known issues and their documented resolutions
- Architecture details that explain the entity's role
- Operational procedures or runbooks for this type of problem

If knowledge results are relevant, incorporate them into your investigation context.

### Step 1.6: Check Past Investigations
Search memory for previously seen patterns or resolutions:

```bash
meho memory search "<entity type> <problem description>" --connector <connector-name> --limit 3
```

Review matches for:
- Previously seen patterns that match the current symptoms
- Known resolutions from past investigations
- Gotchas or edge cases specific to this entity or connector

If a past investigation closely matches, note it and check whether the same resolution applies.

### Step 2: Gather Local Evidence
Query the entity's home connector for details:

```bash
# Get entity details
meho connector call <connector> <get-operation> --param <id>=<value>

# Get events/logs if available
meho connector search-ops "events <entity type>" --connector <connector>
meho connector call <connector> <events-operation> --param <filter>=<entity>
```

Note key findings: errors, warnings, resource issues, timestamps.

### Step 3: Check Local Relationships
Explore same-system relationships:

```bash
meho topology lookup <entity-id> --depth 2
```

For each related entity with issues, gather evidence (repeat Step 2).

### Step 4: Cross-System Traversal
Check for SAME_AS correlations in the topology lookup output.

For each **confirmed** SAME_AS correlation:
1. Note the correlated entity in the other system
2. Query that system for the correlated entity (Step 2)
3. Check that system's relationships (Step 3)
4. Continue until no more correlations or budget exhausted

### Step 5: Synthesize Findings

#### Root Cause
Identify the most likely root cause with confidence:
- **High**: Direct evidence from multiple systems pointing to same issue
- **Medium**: Strong evidence from one system, corroborated by related entities
- **Low**: Circumstantial evidence or single data point

#### Evidence Chain
Narrate the full investigation path:
"Started at [entity] -> found [issue] -> checked [related] -> traversed SAME_AS to [cross-system entity] -> found [root cause]"

#### Alternative Hypotheses
List other possible causes and what additional data would confirm/deny each.

#### Recommended Actions
Provide exact `meho connector call` commands for remediation.
Mark each as READ, WRITE, or DESTRUCTIVE.
DO NOT execute any WRITE or DESTRUCTIVE commands.

## Output Format

Produce a structured diagnostic report:

```json
{
  "status": "diagnosis_complete",
  "query_budget": {"used": 0, "max": 15},
  "investigation_path": ["entity -> finding -> entity -> finding"],
  "findings": [
    {
      "entity": "name",
      "system": "connector-name",
      "finding": "description",
      "evidence": "key data point",
      "confidence": "high|medium|low",
      "timestamp": "if available"
    }
  ],
  "root_cause": {
    "summary": "one sentence",
    "confidence": "high|medium|low",
    "evidence_chain": ["step 1", "step 2"]
  },
  "alternatives": [
    {"hypothesis": "description", "needed_data": "what to check"}
  ],
  "recommended_actions": [
    {
      "action": "description",
      "command": "meho connector call ...",
      "trust_tier": "READ|WRITE|DESTRUCTIVE"
    }
  ]
}
```

### For Human (narrative mode)
Tell the investigation story:
"Started at X -> found Y -> checked Z -> traversed to W -> root cause is..."
Include the most relevant raw data points (the specific event, the metric value, the timestamp) inline.
