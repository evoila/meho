// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * MentionPill (Phase 63)
 *
 * Inline pill badge for @connector mentions in sent messages.
 * Uses CONNECTOR_COLORS and ConnectorIcon for consistent visual
 * language with ConnectorBreadcrumb chips (Phase 62).
 */
import { ConnectorIcon, CONNECTOR_COLORS } from '@/components/topology/ConnectorIcon';

interface MentionPillProps {
  connectorName: string;
  connectorType: string;
}

export function MentionPill({ connectorName, connectorType }: Readonly<MentionPillProps>) {
  const color = CONNECTOR_COLORS[connectorType.toLowerCase()] || CONNECTOR_COLORS.rest;

  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium align-baseline"
      style={{
        backgroundColor: `${color}20`,
        color,
        borderColor: `${color}40`,
        borderWidth: 1,
        borderStyle: 'solid',
      }}
    >
      <ConnectorIcon connectorType={connectorType} size={12} />
      @{connectorName}
    </span>
  );
}
