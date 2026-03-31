// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenants Page - Manage tenants (global_admin only)
 * 
 * Features:
 * - List all tenants
 * - Create new tenants
 * - Quick actions (enable/disable)
 * - Navigate to tenant settings
 */
import { useState } from 'react';
import { Building2, Plus, RefreshCw } from 'lucide-react';
import { motion } from 'motion/react';
import { useTenants } from '@/features/tenants';
import { Button, Card, Badge } from '@/shared';
import { LoadingState, ErrorState } from '@/shared';
import { TenantsTable } from './components/TenantsTable';
import { CreateTenantModal } from './components/CreateTenantModal';
import { toast } from 'sonner';

export function TenantsPage() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [includeInactive, setIncludeInactive] = useState(false);
  
  const {
    tenants,
    total,
    isLoading,
    error,
    refetch,
    createTenant,
    disableTenant,
    enableTenant,
    isCreating,
    isDisabling,
    isEnabling,
  } = useTenants(includeInactive);

  const handleCreateTenant = async (data: Parameters<typeof createTenant>[0]) => {
    try {
      await createTenant(data);
      toast.success(`Tenant "${data.display_name}" created successfully`);
      setShowCreateModal(false);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to create tenant';
      toast.error(message);
      throw err;
    }
  };

  const handleDisableTenant = async (tenantId: string) => {
    try {
      await disableTenant(tenantId);
      toast.success(`Tenant "${tenantId}" has been disabled`);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to disable tenant';
      toast.error(message);
    }
  };

  const handleEnableTenant = async (tenantId: string) => {
    try {
      await enableTenant(tenantId);
      toast.success(`Tenant "${tenantId}" has been enabled`);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to enable tenant';
      toast.error(message);
    }
  };

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <LoadingState message="Loading tenants..." />
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <ErrorState
          title="Failed to load tenants"
          error={error instanceof Error ? error : new Error(String(error))}
          onRetry={refetch}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background relative overflow-hidden">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-secondary/5 rounded-full blur-[100px]" />
      </div>

      <div className="flex-1 overflow-y-auto z-10">
        <div className="max-w-7xl mx-auto p-6 lg:p-8">
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2 }}
          >
            {/* Header */}
            <div className="flex items-center justify-between mb-6">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10">
                  <Building2 className="h-6 w-6 text-primary" />
                </div>
                <div>
                  <h1 className="text-2xl font-bold text-white">Tenant Management</h1>
                  <p className="text-text-secondary text-sm">
                    Manage organizations and their settings
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => refetch()}
                  title="Refresh"
                >
                  <RefreshCw className="h-4 w-4" />
                </Button>
                <Button
                  variant="primary"
                  onClick={() => setShowCreateModal(true)}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  New Tenant
                </Button>
              </div>
            </div>

            {/* Filters */}
            <div className="flex items-center gap-4 mb-6">
              <label className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
                <input
                  type="checkbox"
                  checked={includeInactive}
                  onChange={(e) => setIncludeInactive(e.target.checked)}
                  className="rounded border-border bg-surface text-primary focus:ring-primary focus:ring-offset-0"
                />
                Show inactive tenants
              </label>
              <Badge variant="default" size="sm">
                {total} tenant{total !== 1 ? 's' : ''}
              </Badge>
            </div>

            {/* Tenants Table */}
            <Card className="overflow-hidden">
              <TenantsTable
                tenants={tenants}
                onDisable={handleDisableTenant}
                onEnable={handleEnableTenant}
                isDisabling={isDisabling}
                isEnabling={isEnabling}
              />
            </Card>
          </motion.div>
        </div>
      </div>

      {/* Create Tenant Modal */}
      <CreateTenantModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={handleCreateTenant}
        isLoading={isCreating}
      />
    </div>
  );
}

