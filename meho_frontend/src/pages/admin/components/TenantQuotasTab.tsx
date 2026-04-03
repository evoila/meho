// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Quotas Tab
 * 
 * Configure resource limits: connectors, knowledge chunks, workflows.
 */
import { useState, useMemo } from 'react';
import { Input, Button } from '@/shared';
import type { Tenant, UpdateTenantRequest } from '@/api/types';

interface TenantQuotasTabProps {
  tenant: Tenant;
  onUpdate: (request: UpdateTenantRequest) => Promise<void>;
  isUpdating: boolean;
}

export function TenantQuotasTab({ tenant, onUpdate, isUpdating }: Readonly<TenantQuotasTabProps>) {
  // Initialize state from tenant props
  const [maxConnectors, setMaxConnectors] = useState<number | undefined>(tenant.max_connectors ?? undefined);
  const [maxKnowledgeChunks, setMaxKnowledgeChunks] = useState<number | undefined>(tenant.max_knowledge_chunks ?? undefined);
  const [maxWorkflowsPerDay, setMaxWorkflowsPerDay] = useState<number | undefined>(tenant.max_workflows_per_day ?? undefined);

  // Derive hasChanges from current state vs tenant props
  const hasChanges = useMemo(() => {
    return (
      maxConnectors !== (tenant.max_connectors ?? undefined) ||
      maxKnowledgeChunks !== (tenant.max_knowledge_chunks ?? undefined) ||
      maxWorkflowsPerDay !== (tenant.max_workflows_per_day ?? undefined)
    );
  }, [maxConnectors, maxKnowledgeChunks, maxWorkflowsPerDay, tenant.max_connectors, tenant.max_knowledge_chunks, tenant.max_workflows_per_day]);

  const handleSave = async () => {
    await onUpdate({
      max_connectors: maxConnectors,
      max_knowledge_chunks: maxKnowledgeChunks,
      max_workflows_per_day: maxWorkflowsPerDay,
    });
  };

  const parseNumber = (value: string): number | undefined => {
    if (value === '') return undefined;
    const num = parseInt(value, 10);
    return isNaN(num) ? undefined : num;
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-medium text-white mb-2">Resource Quotas</h3>
        <p className="text-sm text-text-secondary">
          Set limits on resources this tenant can use. Leave empty for unlimited.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Max Connectors */}
        <div className="p-4 bg-surface/50 border border-border rounded-lg">
          <Input
            label="Max Connectors"
            type="number"
            value={maxConnectors ?? ''}
            onChange={(e) => setMaxConnectors(parseNumber(e.target.value))}
            placeholder="Unlimited"
            min={0}
            disabled={isUpdating}
          />
          <p className="mt-2 text-xs text-text-secondary">
            Maximum number of API connectors.
          </p>
        </div>

        {/* Max Knowledge Chunks */}
        <div className="p-4 bg-surface/50 border border-border rounded-lg">
          <Input
            label="Max Knowledge Chunks"
            type="number"
            value={maxKnowledgeChunks ?? ''}
            onChange={(e) => setMaxKnowledgeChunks(parseNumber(e.target.value))}
            placeholder="Unlimited"
            min={0}
            disabled={isUpdating}
          />
          <p className="mt-2 text-xs text-text-secondary">
            Maximum knowledge base size.
          </p>
        </div>

        {/* Max Workflows Per Day */}
        <div className="p-4 bg-surface/50 border border-border rounded-lg">
          <Input
            label="Max Workflows/Day"
            type="number"
            value={maxWorkflowsPerDay ?? ''}
            onChange={(e) => setMaxWorkflowsPerDay(parseNumber(e.target.value))}
            placeholder="Unlimited"
            min={0}
            disabled={isUpdating}
          />
          <p className="mt-2 text-xs text-text-secondary">
            Daily workflow execution limit.
          </p>
        </div>
      </div>

      {/* Quota Info */}
      <div className="p-4 bg-surface/30 border border-border rounded-lg">
        <h4 className="text-sm font-medium text-white mb-2">About Quotas</h4>
        <ul className="text-sm text-text-secondary space-y-1 list-disc list-inside">
          <li>Quotas are enforced when users attempt to create new resources.</li>
          <li>Existing resources are not affected when quotas are lowered.</li>
          <li>Default quotas are based on the subscription tier if not set.</li>
        </ul>
      </div>

      {/* Save Button */}
      <div className="flex justify-end pt-4 border-t border-border">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={!hasChanges}
          isLoading={isUpdating}
        >
          Save Quotas
        </Button>
      </div>
    </div>
  );
}
