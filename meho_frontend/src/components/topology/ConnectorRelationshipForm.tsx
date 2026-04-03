// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorRelationshipForm - Inline form to create/edit connector relationships
 *
 * Three dropdowns in a row: source connector, relationship type, target connector.
 * Plus "Save Relationship" primary button and "Cancel" text button.
 * Validates source != target. Pre-populates for edit mode via initialData prop.
 *
 * Phase 76 Plan 05: Connector Map tab components.
 */

import { useState, useCallback } from 'react';
import { RelationshipTypeSelect } from './RelationshipTypeSelect';
import type {
  ConnectorRelationshipType,
  ConnectorRelationshipCreateRequest,
} from '../../lib/topologyApi';

interface Connector {
  id: string;
  name: string;
}

interface ConnectorRelationshipFormProps {
  connectors: Connector[];
  onSubmit: (data: ConnectorRelationshipCreateRequest) => void;
  onCancel: () => void;
  initialData?: {
    from_connector_id: string;
    to_connector_id: string;
    relationship_type: ConnectorRelationshipType;
  };
  isSubmitting?: boolean;
}

/**
 * Wrapper that forces remount when initialData changes (React key-based reset pattern).
 * This avoids refs-during-render and setState-in-effects for prop→state sync.
 */
export function ConnectorRelationshipForm(props: ConnectorRelationshipFormProps) {
  const key = props.initialData
    ? `${props.initialData.from_connector_id}-${props.initialData.to_connector_id}`
    : '__create__';
  return <ConnectorRelationshipFormInner key={key} {...props} />;
}

function ConnectorRelationshipFormInner({
  connectors,
  onSubmit,
  onCancel,
  initialData,
  isSubmitting = false,
}: Readonly<ConnectorRelationshipFormProps>) {
  const [fromConnectorId, setFromConnectorId] = useState(initialData?.from_connector_id ?? '');
  const [toConnectorId, setToConnectorId] = useState(initialData?.to_connector_id ?? '');
  const [relationshipType, setRelationshipType] = useState<ConnectorRelationshipType | ''>(
    initialData?.relationship_type ?? ''
  );
  const [validationError, setValidationError] = useState<string | null>(null);

  const clearValidation = useCallback(() => setValidationError(null), []);

  const handleSubmit = useCallback(() => {
    if (!fromConnectorId || !toConnectorId || !relationshipType) {
      setValidationError('All fields are required.');
      return;
    }
    if (fromConnectorId === toConnectorId) {
      setValidationError('Source and target connectors must be different.');
      return;
    }
    onSubmit({
      from_connector_id: fromConnectorId,
      to_connector_id: toConnectorId,
      relationship_type: relationshipType as ConnectorRelationshipType,
    });
  }, [fromConnectorId, toConnectorId, relationshipType, onSubmit]);

  const isEdit = !!initialData;

  return (
    <div className="bg-[--color-surface] rounded-lg p-4 border border-[--color-border]">
      <div className="flex flex-wrap items-end gap-3">
        {/* Source connector */}
        <div className="flex-1 min-w-[160px]">
          <label htmlFor="rel-source" className="block text-xs text-[--color-text-secondary] mb-1">
            Source
          </label>
          <select
            id="rel-source"
            value={fromConnectorId}
            onChange={(e) => { setFromConnectorId(e.target.value); clearValidation(); }}
            disabled={isSubmitting}
            aria-label="Source connector"
            className="w-full px-3 py-2 text-sm rounded-lg bg-[--color-surface] text-[--color-text-primary] border border-[--color-border] focus:outline-none focus:ring-2 focus:ring-[--color-primary] focus:border-[--color-primary] disabled:opacity-50"
          >
            <option value="" disabled>
              Select source...
            </option>
            {connectors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        {/* Relationship type */}
        <div className="flex-1 min-w-[160px]">
          {/* eslint-disable-next-line jsx-a11y/label-has-associated-control -- RelationshipTypeSelect is a custom component wrapping a native select */}
          <label className="block text-xs text-[--color-text-secondary] mb-1">
            Relationship
          </label>
          <RelationshipTypeSelect
            value={relationshipType}
            onChange={(v) => { setRelationshipType(v); clearValidation(); }}
            disabled={isSubmitting}
            className="w-full"
          />
        </div>

        {/* Target connector */}
        <div className="flex-1 min-w-[160px]">
          <label htmlFor="rel-target" className="block text-xs text-[--color-text-secondary] mb-1">
            Target
          </label>
          <select
            id="rel-target"
            value={toConnectorId}
            onChange={(e) => { setToConnectorId(e.target.value); clearValidation(); }}
            disabled={isSubmitting}
            aria-label="Target connector"
            className="w-full px-3 py-2 text-sm rounded-lg bg-[--color-surface] text-[--color-text-primary] border border-[--color-border] focus:outline-none focus:ring-2 focus:ring-[--color-primary] focus:border-[--color-primary] disabled:opacity-50"
          >
            <option value="" disabled>
              Select target...
            </option>
            {connectors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleSubmit}
            disabled={isSubmitting}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-[--color-primary] text-white hover:bg-[--color-primary-hover] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {(() => { if (isSubmitting) return 'Saving...'; if (isEdit) return 'Update Relationship'; return 'Save Relationship'; })()}
          </button>
          <button
            onClick={onCancel}
            disabled={isSubmitting}
            className="px-4 py-2 text-sm text-[--color-text-secondary] hover:text-[--color-text-primary] transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      </div>

      {/* Validation error */}
      {validationError && (
        <p className="mt-2 text-xs text-red-400">{validationError}</p>
      )}
    </div>
  );
}
