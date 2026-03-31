# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Built-in skill files for SpecialistAgent.

Skills are structured markdown files that become part of the SpecialistAgent's
system prompt. They are loaded by path (not imported as Python modules).

Phase 77: Connector skills are now primarily DB-backed. The seeder reads these
filesystem skill files and stores them in the orchestrator_skill table with
skill_type="connector". The factory resolves DB connector skills first
(db_connector_skill parameter), falling back to these filesystem files only
when DB access is unavailable (e.g., tests, CLI tools without DB session).

Skill files live in this directory:
    skills/kubernetes.md    - Kubernetes domain knowledge
    skills/vmware.md        - VMware vSphere domain knowledge
    skills/proxmox.md       - Proxmox VE domain knowledge
    skills/gcp.md           - Google Cloud Platform domain knowledge
    skills/prometheus.md    - Prometheus metrics querying
    skills/loki.md          - Loki log querying
    skills/tempo.md         - Tempo distributed tracing
    skills/alertmanager.md  - Alertmanager alert management
    skills/jira.md          - Jira issue tracking
    skills/confluence.md    - Confluence wiki search
    skills/email.md         - Email notifications
    skills/argocd.md        - ArgoCD GitOps management
    skills/github.md        - GitHub repository operations
    skills/generic.md       - Generic REST API fallback skill
"""
