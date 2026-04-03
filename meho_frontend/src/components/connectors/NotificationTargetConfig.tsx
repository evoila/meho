// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * NotificationTargetConfig
 *
 * Configure 0..N notification targets for an event registration or scheduled task.
 * Each target is a connector + contact identifier pair.
 * Embedded in create/edit forms.
 *
 * Phase 75: CRED-05
 */
import { Plus, Trash2, Info } from 'lucide-react';

interface NotificationTarget {
  connector_id: string;
  contact: string;
}

interface NotificationTargetConfigProps {
  targets: NotificationTarget[];
  onChange: (targets: NotificationTarget[]) => void;
  availableConnectors: Array<{ id: string; name: string; connector_type: string }>;
}

const CONTACT_HINTS: Record<string, string> = {
  email: 'Email address (e.g., admin@company.com)',
  slack: 'Channel name (e.g., #ops-alerts)',
  teams: 'Channel or user ID',
};

function getPlaceholder(connectorType: string): string {
  const key = Object.keys(CONTACT_HINTS).find(k => connectorType.toLowerCase().includes(k));
  return key ? CONTACT_HINTS[key] : 'Contact identifier';
}

export function NotificationTargetConfig({
  targets,
  onChange,
  availableConnectors,
}: Readonly<NotificationTargetConfigProps>) {
  // Filter to messaging-capable connectors (email, slack, teams)
  const messagingConnectors = availableConnectors.filter(c =>
    ['email', 'slack', 'teams', 'whatsapp'].some(t =>
      c.connector_type.toLowerCase().includes(t)
    )
  );

  const addTarget = () => onChange([...targets, { connector_id: '', contact: '' }]);
  const removeTarget = (idx: number) => onChange(targets.filter((_, i) => i !== idx));
  const updateTarget = (idx: number, field: keyof NotificationTarget, value: string) => {
    const updated = [...targets];
    updated[idx] = { ...updated[idx], [field]: value };
    onChange(updated);
  };

  const getSelectedConnectorType = (connectorId: string): string => {
    const connector = availableConnectors.find(c => c.id === connectorId);
    return connector?.connector_type || '';
  };

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-semibold text-text-primary">Notification Targets</h4>

      {targets.map((target, idx) => (
        <div key={`target-${idx}`} className="flex items-center gap-2">
          <select
            value={target.connector_id}
            onChange={e => updateTarget(idx, 'connector_id', e.target.value)}
            className={`input-base flex-[2] ${!target.connector_id && targets.length > 0 ? 'border-red-500/50' : ''}`}
            aria-label={`Notification connector ${idx + 1}`}
          >
            <option value="">Select connector...</option>
            {messagingConnectors.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>

          <input
            type="text"
            value={target.contact}
            onChange={e => updateTarget(idx, 'contact', e.target.value)}
            placeholder={getPlaceholder(getSelectedConnectorType(target.connector_id))}
            className={`input-base flex-[3] ${!target.contact && targets.length > 0 ? 'border-red-500/50' : ''}`}
            aria-label={`Contact for notification ${idx + 1}`}
          />

          <button
            type="button"
            onClick={() => removeTarget(idx)}
            className="text-text-tertiary hover:text-red-400 transition-colors p-1"
            aria-label="Remove notification target"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      ))}

      <button
        type="button"
        onClick={addTarget}
        className="flex items-center gap-1.5 text-xs text-primary hover:text-primary-hover transition-colors"
      >
        <Plus className="h-3.5 w-3.5" />
        Add notification target
      </button>

      {targets.length > 0 && (
        <p className="flex items-center gap-1.5 text-xs text-text-tertiary">
          <Info className="h-3 w-3 shrink-0" />
          Notifications are sent when the automated session needs approval for a write operation.
        </p>
      )}

      {messagingConnectors.length === 0 && targets.length > 0 && (
        <p className="text-xs text-text-tertiary">No email connectors configured</p>
      )}
    </div>
  );
}
