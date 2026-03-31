// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenants Table Component
 * 
 * Displays a table of tenants with actions for editing, enabling/disabling,
 * and entering tenant context (TASK-140 Phase 2).
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Settings, Power, PowerOff, Eye } from 'lucide-react';
import { Badge, Button } from '@/shared';
import { useTenantContext } from '@/contexts/TenantContext';
import type { Tenant } from '@/api/types';

interface TenantsTableProps {
  tenants: Tenant[];
  onDisable: (tenantId: string) => Promise<void>;
  onEnable: (tenantId: string) => Promise<void>;
  isDisabling: boolean;
  isEnabling: boolean;
}

export function TenantsTable({
  tenants,
  onDisable,
  onEnable,
  isDisabling,
  isEnabling,
}: TenantsTableProps) {
  const navigate = useNavigate();
  const { enterTenant } = useTenantContext();
  const [actionTenantId, setActionTenantId] = useState<string | null>(null);

  const handleEnterTenant = (tenant: Tenant) => {
    enterTenant(tenant.tenant_id, tenant.display_name || tenant.tenant_id);
  };

  const handleToggleStatus = async (tenant: Tenant) => {
    setActionTenantId(tenant.tenant_id);
    try {
      if (tenant.is_active) {
        await onDisable(tenant.tenant_id);
      } else {
        await onEnable(tenant.tenant_id);
      }
    } finally {
      setActionTenantId(null);
    }
  };

  const getTierBadgeVariant = (tier: string): 'default' | 'success' | 'warning' => {
    switch (tier) {
      case 'enterprise':
        return 'success';
      case 'pro':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  };

  if (tenants.length === 0) {
    return (
      <div className="text-center py-12 text-text-secondary">
        <p>No tenants found.</p>
        <p className="text-sm mt-1">Create your first tenant to get started.</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
              Tenant ID
            </th>
            <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
              Display Name
            </th>
            <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
              Tier
            </th>
            <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
              Status
            </th>
            <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
              Created
            </th>
            <th className="text-right py-3 px-4 text-sm font-medium text-text-secondary">
              Actions
            </th>
          </tr>
        </thead>
        <tbody>
          {tenants.map((tenant) => (
            <tr
              key={tenant.tenant_id}
              className="border-b border-border/50 hover:bg-surface-hover/50 transition-colors cursor-pointer"
              onClick={() => navigate(`/admin/tenants/${tenant.tenant_id}`)}
            >
              <td className="py-3 px-4">
                <span className="font-mono text-sm text-white">
                  {tenant.tenant_id}
                </span>
              </td>
              <td className="py-3 px-4">
                <span className="text-white">
                  {tenant.display_name || '-'}
                </span>
              </td>
              <td className="py-3 px-4">
                <Badge variant={getTierBadgeVariant(tenant.subscription_tier)}>
                  {tenant.subscription_tier}
                </Badge>
              </td>
              <td className="py-3 px-4">
                <Badge variant={tenant.is_active ? 'success' : 'error'}>
                  {tenant.is_active ? 'Active' : 'Disabled'}
                </Badge>
              </td>
              <td className="py-3 px-4 text-text-secondary text-sm">
                {formatDate(tenant.created_at)}
              </td>
              <td className="py-3 px-4 text-right">
                {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- stop propagation container for action buttons */}
                <div className="flex items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                  {/* Enter Tenant Context Button - TASK-140 Phase 2 */}
                  {tenant.is_active && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleEnterTenant(tenant)}
                      title="Enter tenant context"
                      className="text-amber-400 hover:text-amber-300"
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => navigate(`/admin/tenants/${tenant.tenant_id}`)}
                    title="Settings"
                  >
                    <Settings className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleToggleStatus(tenant)}
                    isLoading={actionTenantId === tenant.tenant_id && (isDisabling || isEnabling)}
                    title={tenant.is_active ? 'Disable tenant' : 'Enable tenant'}
                    className={tenant.is_active ? 'text-red-400 hover:text-red-300' : 'text-green-400 hover:text-green-300'}
                  >
                    {tenant.is_active ? (
                      <PowerOff className="h-4 w-4" />
                    ) : (
                      <Power className="h-4 w-4" />
                    )}
                  </Button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

