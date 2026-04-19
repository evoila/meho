// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * RelationshipTypeSelect - Dropdown for connector relationship vocabulary
 *
 * Renders a styled select element with 7 relationship type options
 * from the CONNECTOR_RELATIONSHIP_TYPES constant. Maps underscore
 * values to human-readable labels.
 *
 * Phase 76 Plan 05: Connector Map tab components.
 */

import { clsx } from 'clsx';
import { CONNECTOR_RELATIONSHIP_TYPES, type ConnectorRelationshipType } from '../../lib/topologyApi';

const RELATIONSHIP_LABELS: Record<ConnectorRelationshipType, string> = {
  monitors: 'monitors',
  logs_for: 'logs for',
  traces_for: 'traces for',
  deploys_to: 'deploys to',
  manages: 'manages',
  alerts_for: 'alerts for',
  tracks_issues_for: 'tracks issues for',
};

interface RelationshipTypeSelectProps {
  value: ConnectorRelationshipType | '';
  onChange: (value: ConnectorRelationshipType) => void;
  className?: string;
  disabled?: boolean;
}

export function RelationshipTypeSelect({
  value,
  onChange,
  className,
  disabled = false,
}: Readonly<RelationshipTypeSelectProps>) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as ConnectorRelationshipType)}
      disabled={disabled}
      aria-label="Relationship type"
      className={clsx(
        'px-3 py-2 text-sm rounded-lg',
        'bg-[--color-surface] text-[--color-text-primary]',
        'border border-[--color-border]',
        'focus:outline-none focus:ring-2 focus:ring-[--color-primary] focus:border-[--color-primary]',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        className,
      )}
    >
      <option value="" disabled>
        Select relationship...
      </option>
      {CONNECTOR_RELATIONSHIP_TYPES.map((type) => (
        <option key={type} value={type}>
          {RELATIONSHIP_LABELS[type]}
        </option>
      ))}
    </select>
  );
}
