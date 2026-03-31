# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Orchestrator skill seeder.

Seeds orchestrator and connector skills into the database for each tenant.

Orchestrator skills (cross-system investigation patterns):
- "Pipeline Deployment Trace": traces deployments end-to-end across GitHub,
  Cloud Build, ArgoCD, and Kubernetes.
- "Change Correlation": correlates deployment events with incidents.
- "Infrastructure Performance Cascade": performance diagnosis from app to infra.
- "Service Dependency Failure": dependency chain investigation for service failures.
- "Incident-to-Change Correlation": broader change correlation including infra changes.
- "Log-Driven Error Investigation": error log-first diagnosis pattern.

Connector skills (per-connector domain knowledge):
- All 14 connector types from the filesystem skills directory are seeded
  as connector skills, enabling DB-first skill resolution (Phase 77).

Seeder behavior (D-04 fix):
- Creates skills if missing (is_customized=False by default).
- Updates content/summary if existing AND not is_customized.
- Skips existing skills where is_customized=True (admin edits preserved).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from meho_app.core.otel import get_logger
from meho_app.database import get_session_maker
from meho_app.modules.orchestrator_skills.models import OrchestratorSkillModel

logger = get_logger(__name__)

# ── Filesystem skill directory ───────────────────────────────────────────
SKILLS_DIR = Path(__file__).resolve().parent.parent / "agents" / "skills"


def _read_skill_file(filename: str) -> str:
    """Read a skill markdown file from the skills directory.

    Returns empty string if file not found (logged as warning).
    """
    path = SKILLS_DIR / filename
    if not path.exists():
        logger.warning(f"Skill file not found during seeding: {path}")
        return ""
    return path.read_text()


# ── Shared seeder helper ────────────────────────────────────────────────


async def _seed_skill(
    tenant_id: str,
    name: str,
    description: str,
    content: str,
    summary: str,
    skill_type: str = "orchestrator",
    connector_type: str | None = None,
) -> None:
    """Seed a single skill into the database.

    Behavior (D-04 fix):
    - If skill does not exist: create with is_customized=False.
    - If skill exists and is NOT customized: update content, summary,
      description, and updated_at.
    - If skill exists and IS customized: skip entirely (admin edits preserved).

    Args:
        tenant_id: Tenant to seed the skill for.
        name: Human-readable skill name.
        description: Short description of the skill.
        content: Full skill markdown content.
        summary: 2-4 sentence summary for system prompt injection.
        skill_type: "orchestrator" or "connector".
        connector_type: Connector type (e.g., "kubernetes"), None for orchestrator.
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(
            select(OrchestratorSkillModel).where(
                OrchestratorSkillModel.tenant_id == tenant_id,
                OrchestratorSkillModel.name == name,
                OrchestratorSkillModel.skill_type == skill_type,
            )
        )
        existing = result.scalar_one_or_none()

        now = datetime.now(UTC)

        if existing is None:
            # Create new skill
            skill = OrchestratorSkillModel(
                tenant_id=tenant_id,
                name=name,
                description=description,
                content=content,
                summary=summary,
                is_customized=False,
                skill_type=skill_type,
                connector_type=connector_type,
                created_at=now,
                updated_at=now,
            )
            session.add(skill)
            logger.info(f"Created {skill_type} skill '{name}' for tenant {tenant_id}")
        elif not existing.is_customized:
            # Update non-customized skill (safe seeder update)
            existing.content = content
            existing.summary = summary
            existing.description = description
            existing.updated_at = now
            logger.debug(f"Updated {skill_type} skill '{name}' for tenant {tenant_id}")
        else:
            # Skip customized skill (D-04: admin edits preserved)
            logger.debug(f"Skipping customized skill '{name}' for tenant {tenant_id}")

        await session.commit()


# ── Orchestrator skill content ───────────────────────────────────────────

PIPELINE_TRACE_SKILL_NAME = "Pipeline Deployment Trace"

PIPELINE_TRACE_SKILL_DESCRIPTION = (
    "Cross-system deployment tracing across GitHub, Cloud Build, ArgoCD, and Kubernetes"
)

PIPELINE_TRACE_SKILL_SUMMARY = (
    "When an operator asks about deployments, traces, 'what changed', broken pods, "
    "or deployment investigation, this skill provides the complete investigation chain. "
    "Covers: GitHub (commits, Actions), GCP Cloud Build (builds, artifacts), ArgoCD "
    "(sync, history), and Kubernetes (pods, deployments). Provides forward trace, "
    "backward trace, and change window synthesis patterns with intent classification "
    "and dead end handling."
)

PIPELINE_TRACE_SKILL_CONTENT = """\
# Pipeline Deployment Trace

## Intent Classification

Match operator intent to investigation pattern:

| Intent | Keywords | Pattern |
|--------|----------|---------|
| Forward Trace | "where did commit X end up", "trace this deployment", "follow this change", "what happened to this PR" | Forward Trace |
| Backward Trace | "why is pod X broken", "what caused this", "what deployed to namespace Y", "who changed this service" | Backward Trace |
| Change Window | "what changed", "what happened in the last N hours", "recent deployments", "what deployed recently" | Change Window Synthesis |

## Forward Trace Pattern

Starting from a code change, follow the deployment chain:

1. **GitHub**: Find the commit via `list_commits` or `get_commit_status`
   - Note the SHA, author, message, timestamp
   - If starting from a PR, use `get_pull_request` to get the merge commit SHA
2. **GitHub Actions**: Find workflow runs triggered by this commit via `list_workflow_runs`
   - Filter by head_sha matching the commit
   - If the workflow failed, use `list_workflow_jobs` + `get_workflow_logs` to diagnose
3. **GCP Cloud Build**: Find builds triggered by the workflow via `list_builds`
   - Match by source commit SHA or timestamp correlation
   - If build failed, use `get_build` for step-level details + `get_build_logs` for logs
4. **Artifact Registry**: Find the image produced via `list_docker_images`
   - Match by build ID in image tag or by timestamp proximity
   - Cross-reference image tag with `list_builds` (filter by source commit SHA or time window) to link image -> build
5. **ArgoCD**: Find the sync using this image via `get_sync_history` + `get_revision_metadata`
   - ArgoCD revision SHA = GitHub commit SHA -- use this to query across systems
   - Check `get_application` for current sync status
6. **Kubernetes**: Verify the rollout
   - Use SAME_AS topology edges to find K8s entities from ArgoCD resource tree
   - Check pod status, deployment replicas, container image tags

## Backward Trace Pattern

Starting from a symptomatic K8s resource, trace back to the code change:

1. **Topology First**: Check MANAGED_BY edges for the pod/deployment
   - If MANAGED_BY edge exists to an ArgoCD Server: the resource is ArgoCD-managed
   - If no edge exists: query ArgoCD `list_applications` and filter by destination namespace
2. **ArgoCD**: Get the application managing this resource
   - `get_application` for current sync status and revision
   - `get_sync_history` for recent sync operations and their revision SHAs
   - `get_revision_metadata` for commit message, author, date
3. **GitHub**: Use the revision SHA from ArgoCD as a commit SHA
   - `list_commits` or `compare_refs` to find the commit details
   - If SHA doesn't match any commit: it may be a Helm chart version, not a git SHA
   - Check `get_pull_request` if the commit was part of a PR
4. **Cloud Build**: Find the build that produced the deployed image
   - Check pod container image tag, correlate with Artifact Registry
   - Cross-reference image tag with `list_builds` filtered by time window around ArgoCD sync
   - `list_builds` filtered by time window around the ArgoCD sync timestamp

### Dead End Handling

If a trace reaches a dead end:
- **SHA doesn't match GitHub commits**: May be a Helm chart version, external repo, or a release tag. Report the SHA, suggest checking ArgoCD application source config (`get_application` -> spec.source) for the actual git repo URL.
- **No Cloud Build for the image**: Image may be built by an external CI system, a third-party image, or built locally. Report the image tag and registry.
- **No ArgoCD app for the namespace**: The deployment may be managed by Helm directly, kubectl apply, or another GitOps tool. Report what was found and suggest checking K8s deployment annotations for management hints.
- **Do NOT widen search automatically** -- be transparent about where the chain breaks and suggest specific alternatives the operator can pursue.

## Change Window Synthesis

When operator asks "what changed in the last N hours" (default: 2 hours if not specified):

1. **ArgoCD**: `get_sync_history` for all known applications, filter by time window
2. **GitHub**: `list_commits` for relevant repos, filter by time window
3. **Cloud Build**: `list_builds`, filter by time window
4. **Correlate**: Interleave events by timestamp into a single chronological timeline

Present as chronological timeline narrative:
```
[TIME] [SYSTEM] [EVENT]
14:30 GitHub: commit abc123 by user@example.com -- "fix memory leak in auth service"
14:32 Cloud Build: build #456 started for commit abc123
14:35 Cloud Build: build #456 completed (SUCCESS, 3m12s)
14:36 ArgoCD: app auth-service synced to revision abc123 (Synced/Healthy)
14:38 GitHub: commit def456 by dev@example.com -- "update frontend assets"
14:40 GitHub Actions: workflow run #789 started for commit def456
```

## Cross-System Identifiers

These identifiers connect systems -- use them to jump between connectors:

| Identifier | Found In | Use To Query |
|------------|----------|--------------|
| Commit SHA | GitHub commits, ArgoCD revision, Cloud Build source | Any system that tracks git SHAs |
| Image tag | Cloud Build output, Artifact Registry, ArgoCD app spec, K8s pod spec | Trace image across build -> registry -> deployment |
| Container image | K8s pod containers, ArgoCD resource tree | Match to Artifact Registry images |
| Namespace + name | K8s entities, ArgoCD resource tree | SAME_AS topology traversal |
| Workflow run ID | GitHub Actions | Get jobs, logs for that specific run |

## Rate Limit Awareness

When tracing across GitHub, be mindful of rate limits:
- Check `_rate_limit_warning` in GitHub responses -- if present, minimize GitHub calls
- In conservative mode: skip GitHub Actions log downloads, focus on metadata-only queries
- Forward traces can skip GitHub if you already have the commit SHA from another source
"""


CHANGE_CORRELATION_SKILL_NAME = "Change Correlation"

CHANGE_CORRELATION_SKILL_DESCRIPTION = (
    "Correlates deployment events with incidents by reasoning about timing, "
    "causality, and observability signals"
)

CHANGE_CORRELATION_SKILL_SUMMARY = (
    "When an operator asks 'what caused this alert', 'why did this break', "
    "or needs incident-to-deployment correlation, this skill provides the "
    "investigation pattern. Correlates deployment timestamps from CI/CD "
    "connectors (GitHub, Cloud Build, ArgoCD) with symptom onset from "
    "observability connectors (Prometheus, Alertmanager, Loki). Handles "
    "multi-deployment disambiguation and produces causal chain narratives "
    "with confidence indicators."
)

CHANGE_CORRELATION_SKILL_CONTENT = """\
# Change Correlation

## Intent Classification

Load this skill when the operator's query starts from a **symptom or incident** \
and needs to find the **causal deployment**. This is the inverse of the Pipeline \
Deployment Trace skill, which starts from a deployment and traces it forward.

| Intent | Keywords | Use This Skill? |
|--------|----------|-----------------|
| Incident-to-deployment | "what caused this alert", "why did this break", "which deployment caused", "what changed before this started" | YES |
| Incident correlation | "incident correlation", "deployment-to-symptom", "root cause of this outage" | YES |
| Alert investigation | "correlate this alert with deployments", "did a deploy cause this" | YES |
| Forward deployment trace | "trace this deployment", "where did commit X end up", "follow this change" | NO -- use Pipeline Deployment Trace |
| Recent changes overview | "what changed recently", "recent deployments", "what deployed today" | NO -- use Pipeline Deployment Trace (Change Window) |

## Correlation Investigation Pattern

Follow these steps when correlating an incident with a deployment:

### Step 1: Identify the Symptom

Get the precise symptom details and affected entity:
- **Alertmanager**: `get_firing_alerts` or `get_alert_detail` -- extract alertname, \
affected service/namespace, severity, and the `startsAt` timestamp (this is symptom \
onset).
- **Prometheus**: If starting from a metric anomaly, identify the metric inflection \
point timestamp and the affected service/pod labels.
- **Loki**: If starting from error logs, identify when the new error pattern first \
appeared and which service/namespace is affected.
- **Kubernetes**: If starting from a broken pod, check pod events for \
CrashLoopBackOff, OOMKilled, or ImagePullBackOff and note when the pod entered \
the failed state.

Extract two critical pieces of information:
1. **Affected entity**: service name, namespace, pod name -- whatever identifies \
the symptomatic workload.
2. **Symptom onset timestamp**: when the problem started (alertmanager `startsAt`, \
metric inflection, first error log).

### Step 2: Establish the Time Window

Starting from symptom onset, look **back up to 2 hours** for deployment events. \
Weight by proximity:
- Deployments **0-15 minutes** before symptom onset: highly suspect
- Deployments **15-60 minutes** before: moderately suspect
- Deployments **60-120 minutes** before: possible but less likely
- Deployments **>2 hours** before: unlikely unless slow-burn issue (mention but \
deprioritize)

### Step 3: Find Deployments in the Window

Query all CI/CD connectors for deployment events within the time window:
- **ArgoCD**: `get_sync_history` for applications in the affected namespace. \
Each sync has a revision SHA and timestamp.
- **GitHub**: `list_commits` for repositories related to the affected services. \
Check commit timestamps and authors.
- **Cloud Build**: `list_builds` filtered by the time window. Match builds to \
the affected services by project/repo association.

Collect all deployment events with their timestamps, target services, and commit \
references.

### Step 4: Score Each Deployment (Dual-Signal Correlation)

For each deployment found in the window, evaluate on three criteria (in priority \
order):

1. **Entity overlap** (highest weight): Does the deployment target the **same \
service or namespace** as the symptom? A deployment to `payment-service` when \
`payment-service` is alerting is far more suspect than a deployment to \
`frontend-service`.

2. **Timing proximity** (second weight): How close to symptom onset? A deployment \
5 minutes before the alert fires is more suspect than one 90 minutes before. \
Consider that symptoms can lag deployment by seconds to minutes depending on \
rollout speed, cache warmup, and traffic patterns.

3. **Change scope / blast radius** (third weight): How many files or services \
were touched in the commit/PR? A commit touching 30 files across 3 services is \
more suspect than a single-file documentation change. Check GitHub commit details \
for file count and affected paths.

Rank deployments by these criteria. Entity overlap is the strongest signal -- a \
deployment to an unrelated namespace is unlikely causal even if the timing is close.

### Step 4b: Classify by Causal Plausibility

After scoring, classify each change into one of three categories:

| Category | Examples | Plausibility Weight |
|----------|----------|-------------------|
| **Application Change** | ArgoCD sync event, GitHub merge to deploy branch, ConfigMap/Secret update, container image update, Helm chart value change | HIGH when entity overlap matches symptomatic service |
| **Infrastructure Change** | Node cordon/drain, VM migration (vMotion), node scaling, storage operation, network/firewall change, resource pool adjustment | HIGH when affected entity runs on the changed infrastructure |
| **Non-Functional Change** | Documentation update, CI/CD pipeline YAML change, monitoring rule change, dashboard update, test-only change, code comment/formatting | LOW -- list but deprioritize unless specific evidence links to symptom |

Present changes grouped by plausibility category, not chronologically. Lead with \
the most plausible cause and explain WHY it is plausible (entity overlap + timing \
+ scope).

Priority order for presentation:
1. Application changes with HIGH entity overlap (most likely cause)
2. Infrastructure changes with HIGH entity overlap
3. Application changes with lower entity overlap
4. Infrastructure changes with lower entity overlap
5. Non-functional changes (mentioned but deprioritized)

For each change include: timestamp, source system, classification category, entity \
overlap assessment, and a one-sentence plausibility rationale.

### Step 5: Investigate Top Candidates

For the **top 2-3 highest-scoring deployments**, build the full causal chain. \
Cross-reference the Pipeline Deployment Trace skill's Forward Trace pattern to \
trace each candidate deployment end-to-end:
- What commit triggered it?
- What did the build produce?
- When did ArgoCD sync it?
- What changed in the running service?

Do NOT duplicate the Forward Trace steps here -- follow the Pipeline Deployment \
Trace skill's pattern for the detailed trace.

### Step 6: Graduated Observability Cross-Check

For each top candidate deployment, verify correlation with observability signals \
in this order (graduated depth, not parallel fan-out):

1. **Alertmanager** (highest signal-to-noise): `get_firing_alerts` -- are there \
active alerts matching the affected service around the deployment time? Check \
`startsAt` timestamps. If an alert started within minutes of a deployment sync, \
that is strong correlation.

2. **Prometheus** (metrics confirmation): Query resource metrics (CPU, memory, \
error rate, request latency) for the affected service. Look for anomalies that \
correlate with the deployment timestamp -- a spike in error rate starting exactly \
when a deployment completed is strong evidence.

3. **Loki** (error detail): Search error logs in the affected namespace/service \
around the deployment time. New error patterns (stack traces, connection failures, \
nil pointer exceptions) appearing after a deployment are diagnostic gold.

Stop deepening when you have enough evidence for a confident assessment. If \
Alertmanager alone confirms the correlation, Loki log analysis is optional.

## Multi-Deployment Disambiguation

When **2 or more deployments** exist in the time window:

1. **Score and rank** all deployments using the Step 4 criteria (entity overlap > \
timing proximity > change scope).
2. **Present the top 2-3** with full causal chains and confidence assessments.
3. **List remaining deployments** as "also deployed in the window but lower \
correlation" with a brief reason for each:
   - "Deployed to `frontend` namespace, symptom is in `backend` namespace"
   - "Completed 95 minutes before symptom onset"
   - "Only changed documentation files (README.md, CHANGELOG.md)"
   - "Deployed to a different cluster entirely"
4. **Elimination reasoning**: For each non-causal deployment, state WHY it is \
less likely. This builds operator confidence in the assessment.

## Proactive Observability Cross-Check

When tracing any deployment forward (even from the Pipeline Deployment Trace \
skill), ALWAYS also check observability signals for anomalies around the deploy \
time. This turns every deployment trace into a mini health-check:

1. After identifying the ArgoCD sync event and K8s rollout, query Alertmanager \
for alerts on that service near the sync time.
2. Check Prometheus for metric changes (error rate, latency, resource usage) \
around the deployment timestamp.
3. Search Loki for new error patterns after the deployment.

If anomalies are found, flag them even if the operator did not ask about them. \
A deployment trace that also surfaces "by the way, error rate increased 3x after \
this deploy" is significantly more valuable.

## Causal Chain Narrative Format

Present correlation findings using this structure:

### Visual Causal Chain

Use the established path diagram pattern:
```
commit abc123 (GitHub: "update payment cache config") -> build #456 (Cloud Build: SUCCESS)
-> payment-service (ArgoCD: synced at 14:36) -> payment-service (Prometheus: error rate spike at 14:41)
```

### Confidence Indicators

Assign a confidence level with explicit justification:

- **High confidence**: Same service/namespace as symptom + deployed within ~15 \
minutes of symptom onset + entity overlap confirmed. Example: "High confidence: \
payment-service deployed at 14:36, error rate spike at 14:41 on payment-service, \
same namespace."
- **Medium confidence**: Same namespace but different service, OR timing 15-60 \
minutes before symptom, OR entity overlap uncertain (e.g., shared library update). \
Example: "Medium confidence: auth-library updated at 14:20, payment-service \
errors started at 14:41 -- possible transitive dependency."
- **Low confidence**: Different namespace, OR timing >60 minutes before symptom, \
OR only temporal proximity without entity overlap. Example: "Low confidence: \
frontend-service deployed at 13:15, 86 minutes before payment-service errors -- \
timing is weak and services are in different namespaces."

Always include a brief reasoning sentence explaining WHY the confidence level \
was assigned.

## Cross-Reference to Pipeline Trace

The Pipeline Deployment Trace skill provides the detailed Forward Trace and \
Backward Trace patterns for investigating individual deployments end-to-end. \
The two skills are complementary:

- **Change Correlation** identifies WHICH deployment to investigate (starting \
from a symptom, finding the causal deployment).
- **Pipeline Deployment Trace** shows HOW to trace a deployment (starting from \
a commit or deployment, following it through the pipeline).

When investigating candidate deployments in Step 5, use the Pipeline Deployment \
Trace skill's Forward Trace pattern to build the full commit -> build -> deploy \
-> runtime chain for each candidate.

## Examples

### Example 1: Single Causal Deployment

Operator: "What caused the payment-service alerts that started at 14:41?"

1. Alertmanager `get_firing_alerts` -> HighErrorRate on payment-service, \
startsAt=14:41 UTC
2. Time window: 12:41 - 14:41 UTC (2 hours back)
3. ArgoCD `get_sync_history` -> payment-service synced at 14:36, revision abc123
4. GitHub `list_commits` -> commit abc123: "update payment cache TTL from 5m to 0" \
by dev@example.com at 14:30
5. Prometheus: payment-service error rate jumped from 0.1% to 12% at 14:41
6. Result: **High confidence** -- payment-service deployed 5 minutes before \
symptom onset, same service, commit disables caching causing cache miss storm.

### Example 2: Multiple Deployments, Disambiguation

Operator: "Why is checkout broken since ~15:00?"

1. Alertmanager -> HighLatency on checkout-service, startsAt=15:02 UTC
2. Time window: 13:02 - 15:02 UTC
3. Found 3 deployments: (a) checkout-service at 14:55, (b) auth-service at \
14:30, (c) monitoring-dashboard at 13:45
4. Scoring: (a) entity overlap + 7min proximity = highest; (b) different service \
but same namespace + 32min = medium; (c) different namespace + 77min = lowest
5. Investigate (a): commit def456 "add new payment provider integration" -- 15 \
files changed, new external API call added
6. Prometheus: checkout-service p99 latency jumped from 200ms to 4.2s at 15:01
7. Result: **High confidence** on (a). Auth-service deploy (b) listed as "same \
namespace but different service, 32 minutes before -- possible but less likely." \
Monitoring-dashboard (c) eliminated: "different namespace, only UI changes."

## Entity-Scoped Change Queries

When investigating changes as part of a broader investigation:

1. **If prior findings exist**: Use entity names, namespaces, and service names \
from prior specialist findings to scope your queries. If the K8s specialist found \
issues with `payment-service` in namespace `production`, query ArgoCD for \
`payment-service` application sync history and GitHub for commits to the \
`payment-service` repository.

2. **If this is the first investigation round**: Use the operator's query to \
extract entity names. If the query mentions "payment-service", scope to that. If \
the query is vague ("why are things slow?"), start broad but prioritize the \
namespace/service mentioned in any active alerts.

3. **Time window**: Always scope to 2 hours before symptom onset (or current time \
if no specific symptom timestamp). Do not expand beyond 2 hours unless explicitly \
asked.

4. **Avoid noise**: Do NOT list all deployments across all applications. Only show \
changes to entities related to the investigation. If investigating a pod crash in \
`checkout-service`, a deployment to `monitoring-dashboard` is noise unless there \
is specific evidence it is related.
"""

# ── Investigation skill content (Phase 77: INV-01) ──────────────────────

INFRASTRUCTURE_PERF_SKILL_NAME = "Infrastructure Performance Cascade"

INFRASTRUCTURE_PERF_SKILL_DESCRIPTION = (
    "Cross-system diagnosis pattern for performance issues from application layer down to infrastructure"
)

INFRASTRUCTURE_PERF_SKILL_SUMMARY = (
    "When investigating performance issues (high latency, CPU throttling, OOMKilled, "
    "slow response times, resource pressure), this skill provides the infrastructure "
    "cascade pattern: Application metric (Prometheus) -> Pod state (K8s) -> Node "
    "resources (K8s) -> VM/Host resources (VMware/Proxmox/GCP). Teaches the agent to "
    "systematically descend through infrastructure layers to find the root cause of "
    "resource-related issues."
)

INFRASTRUCTURE_PERF_SKILL_CONTENT = """\
# Infrastructure Performance Cascade

## Trigger Patterns

Load this skill when the investigation involves:
- High latency or slow response times
- CPU throttling or CPU saturation
- OOMKilled pods or memory pressure
- Resource pressure or resource limits hit
- Disk I/O bottlenecks or network saturation
- Pod evictions or scheduling failures

## Investigation Ladder

Systematically descend through infrastructure layers. At each layer, check \
whether the root cause lives there before going deeper.

### Layer 1: Application Metrics (Prometheus)

Start with the symptoms visible in application metrics:
- **Latency**: `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))`
- **Error rate**: `rate(http_requests_total{{status=~"5.."}}[5m])`
- **CPU throttling**: `rate(container_cpu_cfs_throttled_periods_total[5m])`
- **Memory usage**: `container_memory_working_set_bytes` vs `container_spec_memory_limit_bytes`

Identify the affected service/pod and the symptom type (CPU, memory, I/O, network).

### Layer 2: Pod State (Kubernetes)

Check the pod's current state and recent events:
- Pod status: Running, CrashLoopBackOff, OOMKilled, Pending
- Container resource limits vs actual usage
- Pod events: `kubectl describe pod` equivalent -- look for Evicted, \
FailedScheduling, OOMKilled, BackOff
- Resource requests vs limits: is the pod under-provisioned?

**Key identifiers for next layer**: Node name from `pod.spec.nodeName`

### Layer 3: Node Resources (Kubernetes)

Check the node the pod runs on:
- Node allocatable vs capacity: how much headroom remains?
- Node conditions: MemoryPressure, DiskPressure, PIDPressure
- All pods on the node: is the node overcommitted?
- Node events: cordoned, tainted, NotReady

**Key identifiers for next layer**: Node hostname or providerID for SAME_AS \
topology traversal to VM/Host

### Layer 4: VM/Host Resources (VMware/Proxmox/GCP)

Use SAME_AS topology edges to find the underlying VM or host:
- **VMware**: VM CPU ready time, memory ballooning, disk latency, vMotion history
- **Proxmox**: Container/VM resource usage, storage pool IOPS, migration events
- **GCP**: Instance CPU utilization, disk throughput, network egress, \
live migration events

Check whether the physical host is the bottleneck (e.g., all VMs on the \
host are contending for resources).

## Cross-System Identifiers

Use these to traverse between layers:
| From | To | Identifier |
|------|----|------------|
| Prometheus metric | K8s Pod | `pod` label, `namespace` label |
| K8s Pod | K8s Node | `pod.spec.nodeName` |
| K8s Node | VM/Host | Node hostname or providerID (SAME_AS topology) |
| VM/Host | Physical Host | VMware host association, Proxmox node membership |

## When to Stop

Stop descending when you find the root cause at any layer:
- **Layer 1**: Application bug (error rate spike without resource pressure)
- **Layer 2**: Pod misconfiguration (limits too low, wrong resource requests)
- **Layer 3**: Node overcommitment (too many pods scheduled, need more nodes)
- **Layer 4**: Infrastructure constraint (host CPU capped, storage IOPS maxed)

## Example

"API latency spike" investigation:
1. Prometheus: p99 latency jumped from 200ms to 4s on payment-service
2. K8s: payment-pod-xyz showing CPU throttling, limits at 500m but requesting 800m
3. K8s Node: worker-03 at 94% CPU allocation, 47 pods scheduled
4. VMware: vm-worker-03 CPU ready time at 12% (normal < 5%), host overcommitted
5. Root cause: Host-level CPU contention causing VM CPU ready time, cascading \
to pod throttling and application latency
"""


SERVICE_DEPENDENCY_SKILL_NAME = "Service Dependency Failure"

SERVICE_DEPENDENCY_SKILL_DESCRIPTION = (
    "Cross-system diagnosis pattern for service failures, cascading errors, "
    "and dependency chain investigation"
)

SERVICE_DEPENDENCY_SKILL_SUMMARY = (
    "When investigating service failures (connection refused, timeouts, 5xx errors, "
    "circuit breaker open, service unavailable), this skill provides the dependency "
    "chain pattern: Failing service (K8s) -> Upstream dependencies (K8s/Prometheus) -> "
    "Network/DNS (K8s) -> Logs (Loki) -> Alerts (Alertmanager). Teaches the agent to "
    "trace the dependency chain to find which service actually failed first."
)

SERVICE_DEPENDENCY_SKILL_CONTENT = """\
# Service Dependency Failure

## Trigger Patterns

Load this skill when the investigation involves:
- Connection refused or connection timeout errors
- HTTP 5xx errors (502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout)
- Circuit breaker open or tripped
- Service unavailable or service not found
- Cascading failures across multiple services
- Upstream dependency failures

## Investigation Approach

The goal is to find which service ACTUALLY failed first. Cascading failures \
create noise -- many services report errors, but only one is the root cause.

### Step 1: Identify the Failing Service

Start from the reported symptom:
- Which service is the user/alert reporting as broken?
- What is the exact error? (connection refused vs timeout vs 5xx)
- When did it start? (precise timestamp for correlation)

### Step 2: Check the Failing Service Directly (Kubernetes)

Before looking at dependencies, verify the service itself:
- Pod status: is it running? CrashLoopBackOff? OOMKilled?
- Endpoints: does the K8s Service have healthy endpoints?
- Recent restarts: has the pod been restarting?
- Resource usage: is it resource-starved?

If the pod itself is unhealthy, the root cause may be internal (not a dependency). \
Check pod logs (Loki) for startup errors or crashes.

### Step 3: Trace Upstream Dependencies

If the failing service is healthy but returning errors, the problem is upstream:
- **Connection refused**: The upstream service is down or its port is not open
- **Timeout**: The upstream service is overloaded or network is degraded
- **5xx**: The upstream service is returning errors itself

For each upstream dependency:
1. Check K8s Service and endpoints (are pods backing the service?)
2. Check pod health and readiness
3. Check Prometheus error rates for that upstream service
4. If the upstream is also failing, recurse -- trace ITS dependencies

### Step 4: Check Network and DNS (Kubernetes)

If services appear healthy but cannot communicate:
- CoreDNS: Are DNS lookups resolving correctly? Check CoreDNS pod health
- NetworkPolicy: Are there network policies blocking traffic?
- Service mesh: If using Istio/Linkerd, check sidecar proxy health
- K8s events: Look for NetworkNotReady, DNS resolution failures

### Step 5: Correlate with Logs (Loki)

Search error logs around the failure timestamp:
- Filter by the failing service's labels
- Look for the FIRST error occurrence (not the cascade of subsequent errors)
- Common patterns: "connection refused to X:port", "dial tcp: lookup X failed", \
"context deadline exceeded"
- Stack traces often reveal which specific call failed

### Step 6: Check Alerts (Alertmanager)

Cross-reference with active alerts:
- Are there alerts on the upstream services?
- Which alert fired FIRST? (chronological order reveals the cascade direction)
- Do alerts match the dependency chain you traced?

## Cascading Failure Detection

The key insight: find the FIRST service that failed.

1. Collect failure timestamps from all affected services (Prometheus error rate \
onset, Alertmanager alert `startsAt`, Loki first error log)
2. Sort by timestamp -- the earliest failure is likely the root cause
3. Verify: does the earliest failure explain the cascade? (e.g., if database \
failed first, and all services depending on it failed after, that is the root cause)

## Topology-Driven Discovery

Use topology to discover dependencies you might not know about:
- SAME_AS edges: find the same entity across different connectors
- Connector relationships: "monitors", "logs_for" edges point to related systems
- K8s Service -> Pod -> Node chain reveals infrastructure dependencies

## Example

"checkout 503" investigation:
1. K8s: checkout-service pods are Running and Ready
2. Prometheus: checkout-service returning 503 to 40% of requests since 14:30
3. Loki: checkout-service logs show "connection refused to payment-service:8080"
4. K8s: payment-service has 0/3 endpoints ready (all pods CrashLoopBackOff)
5. Loki: payment-service logs show "FATAL: could not connect to database at \
payment-db:5432: connection refused"
6. K8s: payment-db StatefulSet pod is OOMKilled
7. Root cause: payment-db OOMKilled -> payment-service cannot connect to DB -> \
CrashLoopBackOff -> checkout-service gets connection refused -> 503 to users
"""


INCIDENT_CHANGE_SKILL_NAME = "Incident-to-Change Correlation"

INCIDENT_CHANGE_SKILL_DESCRIPTION = (
    "Broader change correlation covering infrastructure changes alongside "
    "CI/CD deployments"
)

INCIDENT_CHANGE_SKILL_SUMMARY = (
    "When investigating incidents triggered by alerts or anomalies, this skill "
    "extends the existing Change Correlation skill to include infrastructure "
    "changes. Covers: Alert detail (Alertmanager) -> Affected entity "
    "(K8s/Prometheus) -> Recent changes across ALL systems including infrastructure "
    "(VM migrations, node scaling, config changes via K8s events, in addition to "
    "ArgoCD/GitHub/Cloud Build deployments). Teaches the agent that 'what changed?' "
    "includes infrastructure, not just code."
)

INCIDENT_CHANGE_SKILL_CONTENT = """\
# Incident-to-Change Correlation

## Trigger Patterns

Load this skill when the investigation involves:
- Alert firing (Alertmanager) with unknown cause
- Metric anomaly (Prometheus) -- sudden spike or drop
- Sudden degradation in service performance
- "What changed?" questions after an incident
- Post-incident review needing change timeline

## Relationship to Change Correlation Skill

The existing Change Correlation skill focuses on CI/CD deployment correlation \
(ArgoCD, GitHub, Cloud Build). This skill EXTENDS that pattern to include \
infrastructure changes that are NOT code deployments:
- VM migrations (vMotion, live migration)
- Node scaling events (new nodes added, nodes cordoned/drained)
- Configuration changes (ConfigMap updates, K8s RBAC changes)
- Infrastructure operations (storage expansion, network policy changes)

Use BOTH skills together for comprehensive change correlation.

## Infrastructure Change Detection

### Kubernetes Events (K8s Connector)

K8s events capture infrastructure-level changes:
- **Node conditions**: NodeNotReady, MemoryPressure, DiskPressure
- **Node lifecycle**: node cordoned, node drained, node added to cluster
- **Taints**: NoSchedule, NoExecute taints added or removed
- **ConfigMap/Secret updates**: changes to configuration that affect running pods
- **RBAC changes**: ClusterRoleBinding, RoleBinding modifications
- **PDB violations**: PodDisruptionBudget events during voluntary disruptions

Time window: look back **2 hours** from symptom onset.

### VMware Events (VMware Connector)

VMware tasks and events capture infrastructure operations:
- **vMotion**: VM migrated between hosts (can cause brief performance dip)
- **Snapshots**: Snapshot creation/deletion (disk I/O impact)
- **Resource pool changes**: CPU/memory limits or reservations modified
- **Host maintenance**: Host entered maintenance mode, VMs evacuated
- **Storage operations**: Datastore expansion, storage vMotion

### GCP Operations (GCP Connector)

GCP audit log events:
- **Instance operations**: VM stop/start, resize, live migration
- **GKE operations**: Node pool resize, cluster upgrade, node auto-repair
- **Network changes**: Firewall rule changes, VPC modifications
- **IAM changes**: Permission grants or revocations

## Correlation Approach

### Step 1: Establish Symptom Timeline

From the alert or anomaly:
1. Extract the precise symptom onset timestamp
2. Identify the affected entity (service, pod, node, VM)
3. Define the lookback window: symptom onset minus 2 hours

### Step 2: Gather ALL Changes in the Window

Query ALL available connectors for changes:
1. **ArgoCD**: sync events (code deployments)
2. **GitHub**: commits and merges
3. **Cloud Build**: build completions
4. **Kubernetes**: events on affected entities AND their nodes
5. **VMware**: tasks on VMs matching SAME_AS topology
6. **GCP**: operations on related resources

### Step 3: Score Changes

Rank changes by correlation likelihood:
1. **Entity overlap** (highest): Change directly affects the symptomatic entity
2. **Timing proximity** (second): Change occurred close to symptom onset
3. **Change scope** (third): Broad changes (node drain, storage migration) \
affect more services

Infrastructure changes score differently than code deployments:
- Node cordon 10 minutes before pod failures: VERY HIGH correlation
- vMotion 5 minutes before latency spike: HIGH correlation
- ConfigMap update in same namespace: MEDIUM-HIGH correlation
- Firewall rule change: MEDIUM correlation (may not be related)

### Step 4: Present Findings

Present a unified timeline of ALL changes (code + infrastructure):
```
[TIME] [SYSTEM] [CHANGE TYPE] [DETAIL]
14:20 VMware: vMotion -- vm-worker-03 migrated from host-a to host-b
14:25 K8s: Node Event -- worker-03 NotReady for 15 seconds
14:28 K8s: Pod Event -- payment-pod-xyz restarted (node recovery)
14:30 Alertmanager: HighErrorRate on payment-service
```

### Classify Each Change

After gathering the unified timeline, classify each change:

| Category | Examples | Plausibility Weight |
|----------|----------|-------------------|
| **Application Change** | ArgoCD sync, GitHub merge, ConfigMap update, image update | HIGH when targeting symptomatic service |
| **Infrastructure Change** | Node cordon/drain, vMotion, node scaling, storage op, firewall change | HIGH when affected entity runs on changed infra |
| **Non-Functional Change** | Doc update, CI config change, monitoring rule, test change | LOW -- list but deprioritize |

Present the classified timeline with the most plausible cause first. Group by \
category rather than pure chronological order when multiple changes exist.

## Difference from Change Correlation Skill

| Aspect | Change Correlation | This Skill |
|--------|-------------------|------------|
| Focus | CI/CD deployments | ALL changes including infrastructure |
| Sources | ArgoCD, GitHub, Cloud Build | + K8s events, VMware tasks, GCP ops |
| Best for | "Which deploy broke this?" | "What changed? (anything)" |
| Use when | Suspecting code change | Unknown cause, need broad search |

## Example

"High CPU alert on worker-03" investigation:
1. Alertmanager: HighCPU alert on node worker-03, startsAt=14:30
2. Time window: 12:30 - 14:30
3. ArgoCD: No syncs to this namespace in 6 hours (not a code change)
4. K8s events: worker-04 was cordoned at 14:15 for maintenance
5. K8s: Pods from worker-04 rescheduled to worker-03 at 14:16
6. K8s: worker-03 now running 52 pods (was 31 before cordon)
7. Root cause: Node cordon triggered pod rescheduling, overloading the remaining node

## Entity-Scoped Change Queries

When this skill is loaded during a broader investigation (not standalone):

1. **Use prior findings**: If earlier specialists identified specific entities \
(pods, services, nodes, VMs), scope change queries to those entities. Do not \
search for changes across the entire cluster.

2. **Topology neighbors**: If topology context is available, include infrastructure \
neighbors (e.g., if investigating a pod, also check changes on its host node and \
the VM backing that node).

3. **Time window**: 2 hours before symptom onset. Do not expand without explicit \
reason.

4. **Filter noise**: Only present changes affecting entities related to the \
investigation. A node drain on a different cluster is irrelevant unless the \
investigation entity was scheduled there.
"""


LOG_DRIVEN_SKILL_NAME = "Log-Driven Error Investigation"

LOG_DRIVEN_SKILL_DESCRIPTION = (
    "Diagnosis pattern starting from error logs and tracing to root cause across systems"
)

LOG_DRIVEN_SKILL_SUMMARY = (
    "When investigating error patterns in logs (stack traces, crash reports, log "
    "volume spikes, new error patterns), this skill provides the log-first "
    "investigation pattern: Error log pattern (Loki) -> Affected pod/service (K8s) -> "
    "Resource state (K8s/Prometheus) -> Recent deployments (ArgoCD) -> Code changes "
    "(GitHub). Teaches the agent to start from the error message and work outward "
    "to find what caused it."
)

LOG_DRIVEN_SKILL_CONTENT = """\
# Log-Driven Error Investigation

## Trigger Patterns

Load this skill when the investigation involves:
- Error logs or stack traces in application logs
- Crash reports or panic/fatal messages
- Log volume spikes (sudden increase in log output)
- New error patterns (errors not seen before)
- "What are these errors?" or "Why is this crashing?"

## Investigation Approach

Start from the error message and work outward. Logs are the most detailed \
signal -- they contain the actual error, not just a metric abstraction.

### Step 1: Identify the Error Pattern (Loki)

Query Loki for the error details:
- Search for error-level logs in the affected service/namespace
- Filter by time window around the reported issue
- Look for patterns: repeated error messages, stack traces, panic output

Key information to extract:
1. **Error message**: The exact error string (e.g., "NullPointerException", \
"connection refused", "permission denied")
2. **Affected service**: Which service/pod is emitting the errors
3. **First occurrence**: When did this error pattern FIRST appear? (not when \
it was reported -- when it started)
4. **Frequency**: Is it every request? Intermittent? Increasing?

### Step 2: Check the Affected Pod/Service (Kubernetes)

Verify the service health:
- Pod status and restart count
- Container logs (if not already in Loki)
- Pod events (OOMKilled, CrashLoopBackOff, readiness probe failures)
- Resource usage vs limits

If the pod is crash-looping, the error from Step 1 is likely the crash cause.

### Step 3: Check Resource State (Kubernetes + Prometheus)

Determine if the error is resource-related:
- CPU throttling: is the pod hitting CPU limits?
- Memory pressure: is the pod approaching memory limits?
- Disk: is the pod running out of ephemeral storage?
- Prometheus metrics: error rate trend, latency changes around the \
first error occurrence

Resource-related errors often manifest as timeouts, slow queries, or \
OOM crashes -- the log message may not directly say "out of memory" but \
the timing correlation reveals it.

### Step 4: Correlate with Recent Deployments (ArgoCD)

Check if the error pattern started after a deployment:
- ArgoCD sync history for the affected service
- Compare first error timestamp with deployment completion timestamp
- If error started within minutes of a deploy: HIGH correlation

### Step 5: Review Code Changes (GitHub)

If a deployment correlates with the error onset:
- Find the commit(s) in the deployment
- Check file changes: did the changed code touch the area mentioned in \
the stack trace?
- Look for obvious issues: null handling, exception handling, connection \
pool configuration

## Log Query Patterns

Effective Loki queries for error investigation:

### Error Rate by Service
```
sum(rate({{namespace="NAMESPACE", container="SERVICE"}} |= "ERROR" [5m])) by (container)
```

### New Error Strings (not seen in previous 24h)
Compare error messages in the recent window vs the prior 24 hours. \
New strings that appear only after the incident onset are the most relevant.

### Stack Trace Grouping
Search for common stack trace prefixes to group related errors:
- Java: "at com.example.service.ClassName.methodName"
- Python: "File \"/app/module.py\", line N, in function_name"
- Go: "goroutine N [running]: package.Function()"

### Log Volume Anomaly
A sudden spike in log volume often indicates a new error pattern flooding \
the logs. Compare current rate with baseline:
```
sum(rate({{namespace="NAMESPACE"}} [1m])) vs avg_over_time(sum(rate({{namespace="NAMESPACE"}} [1m]))[24h:1h])
```

## Timestamp Correlation

The most powerful diagnostic signal is correlating the FIRST error occurrence \
with other events:

| Error first seen | Recent deploy? | Resource spike? | Likely cause |
|------------------|---------------|-----------------|--------------|
| Exactly at deploy | Yes | No | Code bug |
| Exactly at deploy | Yes | Yes | Code + resource issue |
| No recent deploy | No | Yes | Resource exhaustion |
| No recent deploy | No | No | External dependency |
| Gradually increasing | Any | Maybe | Slow-burn issue |

## Example

"NullPointerException in payment-service" investigation:
1. Loki: NullPointerException at PaymentProcessor.process(line 142) -- \
first seen at 14:35, 200+ occurrences since
2. K8s: payment-service-7d4f pods running, no restarts, readiness OK
3. Prometheus: error rate jumped from 0.1% to 8% at 14:35
4. ArgoCD: payment-service synced at 14:33, revision abc123
5. GitHub: commit abc123 "refactor payment validation logic" -- changed \
PaymentProcessor.java, removed null check on line 138
6. Root cause: Deployment at 14:33 introduced a null pointer bug in \
PaymentProcessor, errors started 2 minutes later when first affected \
request hit the new code path
"""


# ── Investigation skill seeders ─────────────────────────────────────────


async def seed_infrastructure_perf_skill(tenant_id: str) -> None:
    """Seed the infrastructure performance cascade skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=INFRASTRUCTURE_PERF_SKILL_NAME,
        description=INFRASTRUCTURE_PERF_SKILL_DESCRIPTION,
        content=INFRASTRUCTURE_PERF_SKILL_CONTENT,
        summary=INFRASTRUCTURE_PERF_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


async def seed_service_dependency_skill(tenant_id: str) -> None:
    """Seed the service dependency failure skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=SERVICE_DEPENDENCY_SKILL_NAME,
        description=SERVICE_DEPENDENCY_SKILL_DESCRIPTION,
        content=SERVICE_DEPENDENCY_SKILL_CONTENT,
        summary=SERVICE_DEPENDENCY_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


async def seed_incident_change_skill(tenant_id: str) -> None:
    """Seed the incident-to-change correlation skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=INCIDENT_CHANGE_SKILL_NAME,
        description=INCIDENT_CHANGE_SKILL_DESCRIPTION,
        content=INCIDENT_CHANGE_SKILL_CONTENT,
        summary=INCIDENT_CHANGE_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


async def seed_log_driven_skill(tenant_id: str) -> None:
    """Seed the log-driven error investigation skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=LOG_DRIVEN_SKILL_NAME,
        description=LOG_DRIVEN_SKILL_DESCRIPTION,
        content=LOG_DRIVEN_SKILL_CONTENT,
        summary=LOG_DRIVEN_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


# ── Connector skill definitions ──────────────────────────────────────────
# Each tuple: (name, description, summary, filename, connector_type)

CONNECTOR_SKILLS: list[tuple[str, str, str, str, str]] = [
    (
        "Kubernetes",
        "Kubernetes cluster diagnostics including pods, deployments, services, nodes, and events",
        "Teaches the specialist agent to diagnose Kubernetes clusters: pod lifecycle issues "
        "(CrashLoopBackOff, OOMKilled, ImagePullBackOff), deployment rollout failures, "
        "service networking, node resource pressure, and event-driven debugging patterns.",
        "kubernetes.md",
        "kubernetes",
    ),
    (
        "VMware",
        "VMware vSphere diagnostics including VMs, hosts, datastores, and clusters",
        "Teaches the specialist agent to diagnose VMware vSphere environments: VM performance "
        "issues, host resource contention, datastore capacity, DRS/HA cluster behavior, "
        "and vMotion event correlation.",
        "vmware.md",
        "vmware",
    ),
    (
        "Proxmox",
        "Proxmox VE diagnostics including VMs, containers, storage, and cluster nodes",
        "Teaches the specialist agent to diagnose Proxmox VE environments: VM and LXC container "
        "health, storage pool capacity, cluster node status, and migration event analysis.",
        "proxmox.md",
        "proxmox",
    ),
    (
        "GCP",
        "Google Cloud Platform diagnostics including Compute, GKE, Cloud Build, and Artifact Registry",
        "Teaches the specialist agent to diagnose GCP environments: Compute Engine instances, "
        "GKE cluster health, Cloud Build pipelines, Artifact Registry images, and IAM permission issues.",
        "gcp.md",
        "gcp",
    ),
    (
        "Prometheus",
        "Prometheus metrics querying and anomaly detection patterns",
        "Teaches the specialist agent to query Prometheus for infrastructure and application metrics: "
        "resource utilization (CPU, memory, disk), error rates, latency percentiles, and "
        "anomaly detection via rate-of-change and threshold comparison.",
        "prometheus.md",
        "prometheus",
    ),
    (
        "Loki",
        "Loki log querying and error pattern analysis",
        "Teaches the specialist agent to query Loki for application and system logs: "
        "error log extraction, log stream filtering by labels, time-windowed searches, "
        "and new error pattern detection around deployment events.",
        "loki.md",
        "loki",
    ),
    (
        "Tempo",
        "Tempo distributed trace querying and latency analysis",
        "Teaches the specialist agent to query Tempo for distributed traces: trace ID lookups, "
        "service-to-service latency analysis, span error detection, and request flow "
        "visualization across microservices.",
        "tempo.md",
        "tempo",
    ),
    (
        "Alertmanager",
        "Alertmanager alert management, silencing, and routing diagnostics",
        "Teaches the specialist agent to query Alertmanager for active alerts: firing alert "
        "enumeration, alert grouping analysis, silence management, and correlation of alert "
        "start times with deployment events.",
        "alertmanager.md",
        "alertmanager",
    ),
    (
        "Jira",
        "Jira issue tracking, project management, and search operations",
        "Teaches the specialist agent to interact with Jira: issue creation and updates, "
        "JQL-based search for related incidents, project status queries, and linking "
        "infrastructure events to tracked issues.",
        "jira.md",
        "jira",
    ),
    (
        "Confluence",
        "Confluence wiki search and content management",
        "Teaches the specialist agent to search Confluence for runbooks and documentation: "
        "CQL-based content search, space navigation, page content retrieval, and "
        "knowledge lookup for troubleshooting procedures.",
        "confluence.md",
        "confluence",
    ),
    (
        "Email",
        "Email notification sending for alert escalation and reporting",
        "Teaches the specialist agent to send email notifications: composing alert "
        "escalation emails, formatting investigation summaries, and delivering "
        "status reports to stakeholders.",
        "email.md",
        "email",
    ),
    (
        "ArgoCD",
        "ArgoCD GitOps application management and sync diagnostics",
        "Teaches the specialist agent to diagnose ArgoCD environments: application sync "
        "status, sync history with revision tracking, resource tree analysis, and "
        "rollback identification for GitOps-managed deployments.",
        "argocd.md",
        "argocd",
    ),
    (
        "GitHub",
        "GitHub repository operations including commits, PRs, Actions, and releases",
        "Teaches the specialist agent to query GitHub repositories: commit history, "
        "pull request details, workflow run status, job logs, and repository "
        "metadata for deployment tracing.",
        "github.md",
        "github",
    ),
    (
        "Generic REST",
        "Generic REST API interaction via OpenAPI spec-driven operations",
        "Teaches the specialist agent to interact with generic REST APIs using "
        "OpenAPI-generated operations: dynamic endpoint discovery, parameter "
        "construction, response parsing, and error handling for arbitrary APIs.",
        "generic.md",
        "generic",
    ),
]


# ── Orchestrator skill seeders ───────────────────────────────────────────


async def seed_pipeline_trace_skill(tenant_id: str) -> None:
    """Seed the pipeline trace orchestrator skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=PIPELINE_TRACE_SKILL_NAME,
        description=PIPELINE_TRACE_SKILL_DESCRIPTION,
        content=PIPELINE_TRACE_SKILL_CONTENT,
        summary=PIPELINE_TRACE_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


async def seed_change_correlation_skill(tenant_id: str) -> None:
    """Seed the change correlation orchestrator skill for a tenant."""
    await _seed_skill(
        tenant_id=tenant_id,
        name=CHANGE_CORRELATION_SKILL_NAME,
        description=CHANGE_CORRELATION_SKILL_DESCRIPTION,
        content=CHANGE_CORRELATION_SKILL_CONTENT,
        summary=CHANGE_CORRELATION_SKILL_SUMMARY,
        skill_type="orchestrator",
    )


# ── Connector skill seeders ─────────────────────────────────────────────


async def seed_connector_skills(tenant_id: str) -> None:
    """Seed all 14 connector skills for a tenant.

    Reads skill content from the filesystem skills directory and seeds
    each as a connector skill in the DB. Existing customized skills
    are not overwritten.
    """
    for name, description, summary, filename, connector_type in CONNECTOR_SKILLS:
        content = _read_skill_file(filename)
        if not content:
            logger.warning(f"Skipping connector skill '{name}': no content from {filename}")
            continue
        await _seed_skill(
            tenant_id=tenant_id,
            name=name,
            description=description,
            content=content,
            summary=summary,
            skill_type="connector",
            connector_type=connector_type,
        )


# ── Master seeder ────────────────────────────────────────────────────────


async def seed_all_skills(tenant_id: str) -> None:
    """Seed all orchestrator and connector skills for a tenant.

    Call this on application startup to ensure all skills exist.
    Respects is_customized flag -- admin-edited skills are never overwritten.
    """
    # Orchestrator skills
    await seed_pipeline_trace_skill(tenant_id)
    await seed_change_correlation_skill(tenant_id)

    # Investigation skills (Phase 77: INV-01 cross-system diagnosis patterns)
    await seed_infrastructure_perf_skill(tenant_id)
    await seed_service_dependency_skill(tenant_id)
    await seed_incident_change_skill(tenant_id)
    await seed_log_driven_skill(tenant_id)

    # Connector skills (Phase 77: DB-backed connector skills)
    await seed_connector_skills(tenant_id)

    logger.info(f"Skill seeding complete for tenant {tenant_id}")


# ── Convenience aliases (backward compatibility) ─────────────────────────


async def ensure_pipeline_trace_skill(tenant_id: str) -> None:
    """Convenience alias for seed_pipeline_trace_skill."""
    await seed_pipeline_trace_skill(tenant_id)


async def ensure_change_correlation_skill(tenant_id: str) -> None:
    """Convenience alias for seed_change_correlation_skill."""
    await seed_change_correlation_skill(tenant_id)
