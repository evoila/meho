// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Create Tenant Modal
 * 
 * Modal form for creating a new tenant with Keycloak realm.
 */
import { useState } from 'react';
import { Modal, Button, Input } from '@/shared';
import type { CreateTenantRequest, SubscriptionTier } from '@/api/types';

interface CreateTenantModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (data: CreateTenantRequest) => Promise<void>;
  isLoading: boolean;
}

const RESERVED_NAMES = ['master', 'admin', 'keycloak', 'meho', 'system'];

export function CreateTenantModal({
  isOpen,
  onClose,
  onSubmit,
  isLoading,
}: CreateTenantModalProps) {
  const [formData, setFormData] = useState<CreateTenantRequest>({
    tenant_id: '',
    display_name: '',
    subscription_tier: 'free',
    create_keycloak_realm: true,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});

  const validateForm = (): boolean => {
    const newErrors: Record<string, string> = {};

    // Validate tenant_id
    if (!formData.tenant_id) {
      newErrors.tenant_id = 'Tenant ID is required';
    } else if (formData.tenant_id.length < 3) {
      newErrors.tenant_id = 'Tenant ID must be at least 3 characters';
    } else if (formData.tenant_id.length > 63) {
      newErrors.tenant_id = 'Tenant ID must be at most 63 characters';
    } else if (!/^[a-z][a-z0-9-]*[a-z0-9]$/.test(formData.tenant_id) && formData.tenant_id.length > 2) {
      newErrors.tenant_id = 'Must start with a letter, contain only lowercase letters, numbers, and hyphens';
    } else if (RESERVED_NAMES.includes(formData.tenant_id.toLowerCase())) {
      newErrors.tenant_id = 'This name is reserved';
    }

    // Validate display_name
    if (!formData.display_name) {
      newErrors.display_name = 'Display name is required';
    } else if (formData.display_name.length > 255) {
      newErrors.display_name = 'Display name must be at most 255 characters';
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!validateForm()) {
      return;
    }

    try {
      await onSubmit(formData);
      // Reset form on success
      setFormData({
        tenant_id: '',
        display_name: '',
        subscription_tier: 'free',
        create_keycloak_realm: true,
      });
      setErrors({});
    } catch {
      // Error is handled by parent
    }
  };

  const handleClose = () => {
    if (!isLoading) {
      setFormData({
        tenant_id: '',
        display_name: '',
        subscription_tier: 'free',
        create_keycloak_realm: true,
      });
      setErrors({});
      onClose();
    }
  };

  const handleTenantIdChange = (value: string) => {
    // Convert to lowercase and replace invalid characters
    const sanitized = value.toLowerCase().replace(/[^a-z0-9-]/g, '-');
    setFormData({ ...formData, tenant_id: sanitized });
    if (errors.tenant_id) {
      setErrors({ ...errors, tenant_id: '' });
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title="Create New Tenant"
      description="Set up a new organization with its own Keycloak realm."
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={handleClose} disabled={isLoading}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={handleSubmit}
            isLoading={isLoading}
          >
            Create Tenant
          </Button>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Tenant ID */}
        <div>
          <Input
            label="Tenant ID"
            value={formData.tenant_id}
            onChange={(e) => handleTenantIdChange(e.target.value)}
            placeholder="acme-corp"
            error={errors.tenant_id}
            hint="Unique identifier (slug). Will be used as the Keycloak realm name."
            disabled={isLoading}
          />
        </div>

        {/* Display Name */}
        <div>
          <Input
            label="Display Name"
            value={formData.display_name}
            onChange={(e) => {
              setFormData({ ...formData, display_name: e.target.value });
              if (errors.display_name) {
                setErrors({ ...errors, display_name: '' });
              }
            }}
            placeholder="Acme Corporation"
            error={errors.display_name}
            hint="Human-readable name for the organization."
            disabled={isLoading}
          />
        </div>

        {/* Subscription Tier */}
        <div>
          <label htmlFor="create-tenant-subscription-tier" className="block text-sm font-medium text-text-primary mb-2">
            Subscription Tier
          </label>
          <select
            id="create-tenant-subscription-tier"
            value={formData.subscription_tier}
            onChange={(e) => setFormData({ ...formData, subscription_tier: e.target.value as SubscriptionTier })}
            className="w-full px-3 py-2 bg-surface border border-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-primary/50"
            disabled={isLoading}
          >
            <option value="free">Free</option>
            <option value="pro">Pro</option>
            <option value="enterprise">Enterprise</option>
          </select>
          <p className="mt-1.5 text-xs text-text-secondary">
            Determines available features and quotas.
          </p>
        </div>

        {/* Quotas Section */}
        <div className="border-t border-border pt-4">
          <h4 className="text-sm font-medium text-text-primary mb-4">Quotas (Optional)</h4>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <Input
                label="Max Connectors"
                type="number"
                value={formData.max_connectors ?? ''}
                onChange={(e) => setFormData({
                  ...formData,
                  max_connectors: e.target.value ? parseInt(e.target.value) : undefined,
                })}
                placeholder="Unlimited"
                min={0}
                disabled={isLoading}
              />
            </div>
            <div>
              <Input
                label="Max Knowledge Chunks"
                type="number"
                value={formData.max_knowledge_chunks ?? ''}
                onChange={(e) => setFormData({
                  ...formData,
                  max_knowledge_chunks: e.target.value ? parseInt(e.target.value) : undefined,
                })}
                placeholder="Unlimited"
                min={0}
                disabled={isLoading}
              />
            </div>
            <div>
              <Input
                label="Max Workflows/Day"
                type="number"
                value={formData.max_workflows_per_day ?? ''}
                onChange={(e) => setFormData({
                  ...formData,
                  max_workflows_per_day: e.target.value ? parseInt(e.target.value) : undefined,
                })}
                placeholder="Unlimited"
                min={0}
                disabled={isLoading}
              />
            </div>
          </div>
        </div>

        {/* Keycloak Realm */}
        <div className="border-t border-border pt-4">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={formData.create_keycloak_realm}
              onChange={(e) => setFormData({ ...formData, create_keycloak_realm: e.target.checked })}
              className="rounded border-border bg-surface text-primary focus:ring-primary focus:ring-offset-0"
              disabled={isLoading}
            />
            <div>
              <span className="text-sm font-medium text-white">Create Keycloak Realm</span>
              <p className="text-xs text-text-secondary">
                Automatically create a Keycloak realm for this tenant with standard roles.
              </p>
            </div>
          </label>
        </div>
      </form>
    </Modal>
  );
}

