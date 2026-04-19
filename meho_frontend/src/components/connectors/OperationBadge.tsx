// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * OperationBadge
 *
 * Renders an inheritance badge for connector operations.
 * Shows "Inherited from {type}" for type-level ops or "Custom" for instance overrides.
 * Used in TypedOperationsBrowser alongside each operation row.
 */
import { ArrowDownRight, Pencil } from 'lucide-react';
import clsx from 'clsx';

interface OperationBadgeProps {
  /** Operation source: 'type' = inherited from connector type definition, 'custom' = instance-specific */
  source: 'type' | 'custom';
  /** Connector type name for inherited badge display (e.g., "Kubernetes", "VMware") */
  connectorType?: string;
  /** Whether the operation is disabled (adds dimmed styling) */
  isDisabled?: boolean;
}

/** Format connector type for display */
function formatConnectorType(type?: string): string {
  if (!type) return 'Type';
  const typeMap: Record<string, string> = {
    kubernetes: 'Kubernetes',
    vmware: 'VMware',
    proxmox: 'Proxmox',
    gcp: 'GCP',
    prometheus: 'Prometheus',
    loki: 'Loki',
    tempo: 'Tempo',
    alertmanager: 'Alertmanager',
    jira: 'Jira',
    confluence: 'Confluence',
    email: 'Email',
    rest: 'REST',
    soap: 'SOAP',
    graphql: 'GraphQL',
    grpc: 'gRPC',
  };
  return typeMap[type] ?? type.charAt(0).toUpperCase() + type.slice(1);
}

export function OperationBadge({ source, connectorType, isDisabled }: Readonly<OperationBadgeProps>) {
  if (source === 'type') {
    return (
      <span
        className={clsx(
          'inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border font-medium',
          isDisabled
            ? 'bg-gray-500/10 text-gray-500 border-gray-500/20 line-through'
            : 'bg-blue-500/10 text-blue-400 border-blue-500/20'
        )}
      >
        <ArrowDownRight className="w-3 h-3" />
        Inherited from {formatConnectorType(connectorType)}
      </span>
    );
  }

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border font-medium',
        isDisabled
          ? 'bg-gray-500/10 text-gray-500 border-gray-500/20 line-through'
          : 'bg-amber-500/10 text-amber-400 border-amber-500/20'
      )}
    >
      <Pencil className="w-3 h-3" />
      Custom
    </span>
  );
}
