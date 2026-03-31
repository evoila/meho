// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorBreadcrumb (Phase 62)
 *
 * Horizontal connector chip trail showing the hop sequence for
 * multi-connector investigation responses. Each chip is colored
 * by connector type, with chevron arrows between hops.
 *
 * Single-connector responses show one chip with no arrows.
 * Empty connectors array returns null.
 */
import { Fragment } from 'react';
import { ChevronRight } from 'lucide-react';
import { ConnectorIcon, CONNECTOR_COLORS } from '@/components/topology/ConnectorIcon';

interface ConnectorBreadcrumbProps {
  connectors: Array<{ id: string; name: string; type?: string }>;
  onChipClick?: (connectorName: string) => void;
}

// Detect connector type from name (reuse heuristic from ConnectorSegment)
function inferConnectorType(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes('kubernetes') || lower.includes('k8s')) return 'kubernetes';
  if (lower.includes('vmware') || lower.includes('vsphere') || lower.includes('vcenter')) return 'vmware';
  if (lower.includes('gcp') || lower.includes('google')) return 'gcp';
  if (lower.includes('proxmox')) return 'proxmox';
  if (lower.includes('prometheus')) return 'prometheus';
  if (lower.includes('loki')) return 'loki';
  if (lower.includes('tempo')) return 'tempo';
  if (lower.includes('alertmanager')) return 'alertmanager';
  if (lower.includes('jira')) return 'jira';
  if (lower.includes('confluence')) return 'confluence';
  if (lower.includes('email')) return 'email';
  return 'rest';
}

export function ConnectorBreadcrumb({ connectors, onChipClick }: ConnectorBreadcrumbProps) {
  if (!connectors || connectors.length === 0) return null;

  return (
    <div className="flex items-center gap-1 mb-2 flex-wrap">
      {connectors.map((connector, i) => {
        const connectorType = connector.type || inferConnectorType(connector.name);
        const color = CONNECTOR_COLORS[connectorType] || CONNECTOR_COLORS.rest;

        return (
          <Fragment key={connector.id + '-' + i}>
            {/* Arrow between chips (skip for first chip) */}
            {i > 0 && (
              <ChevronRight className="w-3 h-3 text-text-tertiary flex-shrink-0" />
            )}

            {/* Connector chip */}
            <button
              type="button"
              onClick={() => onChipClick?.(connector.name)}
              className="flex items-center gap-1.5 px-2 py-1 rounded-full text-xs border transition-colors hover:bg-white/5"
              style={{
                borderColor: `${color}40`,
                color: color,
              }}
            >
              <ConnectorIcon connectorType={connectorType} size={14} />
              {connector.name}
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}
