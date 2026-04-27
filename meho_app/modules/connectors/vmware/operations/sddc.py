# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SDDC Manager operation definitions for VMware connector.

All operations are read-only (D-18) and target the SDDC Manager REST API
at /v1/.  Covers VCF lifecycle visibility: workload domains, hosts,
clusters, update compliance, and certificate health (D-17, D-19).
"""

from meho_app.modules.connectors.base import OperationDefinition

SDDC_OPERATIONS = [
    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="get_sddc_system_info",
        name="Get SDDC Manager System Info",
        description=(
            "Get SDDC Manager version, build, hostname, and FQDN. "
            "Use to verify which VCF version is running in the environment."
        ),
        category="system",
        parameters=[],
        example="get_sddc_system_info()",
        response_entity_type="SddcSystem",
        response_identifier_field="id",
        response_display_name_field="fqdn",
    ),
    # ------------------------------------------------------------------
    # Workload Domains
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_sddc_workload_domains",
        name="List VCF Workload Domains",
        description=(
            "List VCF workload domains to understand infrastructure topology. "
            "Shows domain type (MANAGEMENT or VI), status, vCenter FQDNs, "
            "and cluster/host counts."
        ),
        category="inventory",
        parameters=[],
        example="list_sddc_workload_domains()",
        response_entity_type="SddcWorkloadDomain",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_sddc_workload_domain",
        name="Get VCF Workload Domain Details",
        description=(
            "Get full details of a specific VCF workload domain including "
            "NSX cluster info, vCenter details, network pools, and SSO domain."
        ),
        category="inventory",
        parameters=[
            {
                "name": "domain_id",
                "type": "string",
                "required": True,
                "description": "Workload domain ID from list_sddc_workload_domains",
            }
        ],
        example="get_sddc_workload_domain(domain_id='abc123-def456')",
        response_entity_type="SddcWorkloadDomain",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # ------------------------------------------------------------------
    # Hosts
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_sddc_hosts",
        name="List VCF-Managed Hosts",
        description=(
            "List VCF-managed hosts with assignment status and hardware info. "
            "Shows whether each host is ASSIGNED or UNASSIGNED_USEABLE, "
            "which domain and cluster it belongs to, and hardware model/vendor."
        ),
        category="inventory",
        parameters=[],
        example="list_sddc_hosts()",
        response_entity_type="SddcHost",
        response_identifier_field="id",
        response_display_name_field="fqdn",
    ),
    # ------------------------------------------------------------------
    # Clusters
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_sddc_clusters",
        name="List VCF Clusters",
        description=(
            "List VCF clusters with host counts and datastore configuration. "
            "Shows primary datastore name/type, stretched cluster status, "
            "and default cluster flag."
        ),
        category="inventory",
        parameters=[],
        example="list_sddc_clusters()",
        response_entity_type="SddcCluster",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # ------------------------------------------------------------------
    # Update History
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="get_sddc_update_history",
        name="Get VCF Update History",
        description=(
            "Get VCF upgrade/update history to check what versions have been applied. "
            "Shows upgrade status, release version, and timestamps. "
            "Use to answer 'Is this host up to date?' investigations."
        ),
        category="system",
        parameters=[],
        example="get_sddc_update_history()",
        response_entity_type="SddcUpgrade",
        response_identifier_field="id",
        response_display_name_field="release_version",
    ),
    # ------------------------------------------------------------------
    # Certificates
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="get_sddc_certificates",
        name="Get VCF Certificate Health",
        description=(
            "Check certificate health to diagnose TLS issues in the VCF environment. "
            "Shows subject, issuer, validity period, and thumbprint for each certificate. "
            "Use to investigate certificate expiration problems."
        ),
        category="system",
        parameters=[],
        example="get_sddc_certificates()",
        response_entity_type="SddcCertificate",
        response_identifier_field="thumbprint",
        response_display_name_field="subject",
    ),
    # ------------------------------------------------------------------
    # Prechecks
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="get_sddc_prechecks",
        name="Get VCF Precheck Results",
        description=(
            "Get compliance/precheck results to verify upgrade readiness. "
            "Shows precheck status, type, description, and result. "
            "Use to answer 'Is this environment healthy enough for an upgrade?'"
        ),
        category="system",
        parameters=[],
        example="get_sddc_prechecks()",
        response_entity_type="SddcPrecheck",
        response_identifier_field="precheck_type",
        response_display_name_field="description",
    ),
]
