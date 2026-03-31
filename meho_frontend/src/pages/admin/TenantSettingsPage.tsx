// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Settings Page
 * 
 * Configure individual tenant settings with tabbed interface.
 */
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Building2, Settings, Gauge, Cpu, AlertTriangle } from 'lucide-react';
import { motion } from 'motion/react';
import { useTenant, useTenants } from '@/features/tenants';
import { Button, Card, Tabs } from '@/shared';
import { LoadingState, ErrorState } from '@/shared';
import { TenantGeneralTab } from './components/TenantGeneralTab';
import { TenantQuotasTab } from './components/TenantQuotasTab';
import { TenantLLMSettingsTab } from './components/TenantLLMSettingsTab';
import { TenantDangerZoneTab } from './components/TenantDangerZoneTab';
import { toast } from 'sonner';
import type { UpdateTenantRequest } from '@/api/types';

export function TenantSettingsPage() {
  const { tenantId } = useParams<{ tenantId: string }>();
  const navigate = useNavigate();
  
  const { data: tenant, isLoading, error, refetch } = useTenant(tenantId || null);
  const { 
    updateTenant, 
    disableTenant, 
    enableTenant,
    isUpdating, 
    isDisabling, 
    isEnabling 
  } = useTenants();

  const handleUpdate = async (request: UpdateTenantRequest) => {
    if (!tenantId) return;
    try {
      await updateTenant({ tenantId, request });
      toast.success('Settings updated successfully');
      refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to update settings';
      toast.error(message);
      throw err;
    }
  };

  const handleDisable = async () => {
    if (!tenantId) return;
    try {
      await disableTenant(tenantId);
      toast.success('Tenant disabled successfully');
      refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to disable tenant';
      toast.error(message);
    }
  };

  const handleEnable = async () => {
    if (!tenantId) return;
    try {
      await enableTenant(tenantId);
      toast.success('Tenant enabled successfully');
      refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to enable tenant';
      toast.error(message);
    }
  };

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <LoadingState message="Loading tenant settings..." />
      </div>
    );
  }

  if (error || !tenant) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <ErrorState
          title="Failed to load tenant"
          error={error instanceof Error ? error : new Error('Tenant not found')}
          onRetry={refetch}
        />
      </div>
    );
  }

  const tabs = [
    {
      id: 'general',
      label: 'General',
      icon: <Settings className="h-4 w-4" />,
      content: (
        <TenantGeneralTab
          tenant={tenant}
          onUpdate={handleUpdate}
          isUpdating={isUpdating}
        />
      ),
    },
    {
      id: 'quotas',
      label: 'Quotas',
      icon: <Gauge className="h-4 w-4" />,
      content: (
        <TenantQuotasTab
          tenant={tenant}
          onUpdate={handleUpdate}
          isUpdating={isUpdating}
        />
      ),
    },
    {
      id: 'llm',
      label: 'LLM Settings',
      icon: <Cpu className="h-4 w-4" />,
      content: (
        <TenantLLMSettingsTab
          tenant={tenant}
          onUpdate={handleUpdate}
          isUpdating={isUpdating}
        />
      ),
    },
    {
      id: 'danger',
      label: 'Danger Zone',
      icon: <AlertTriangle className="h-4 w-4" />,
      content: (
        <TenantDangerZoneTab
          tenant={tenant}
          onDisable={handleDisable}
          onEnable={handleEnable}
          isDisabling={isDisabling}
          isEnabling={isEnabling}
        />
      ),
    },
  ];

  return (
    <div className="flex flex-col h-full bg-background relative overflow-hidden">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-secondary/5 rounded-full blur-[100px]" />
      </div>

      <div className="flex-1 overflow-y-auto z-10">
        <div className="max-w-4xl mx-auto p-6 lg:p-8">
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2 }}
          >
            {/* Back Button */}
            <Button
              variant="ghost"
              onClick={() => navigate('/admin/tenants')}
              className="mb-4"
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              Back to Tenants
            </Button>

            {/* Header */}
            <div className="flex items-center gap-3 mb-6">
              <div className="p-2 rounded-lg bg-primary/10">
                <Building2 className="h-6 w-6 text-primary" />
              </div>
              <div>
                <h1 className="text-2xl font-bold text-white">
                  {tenant.display_name || tenant.tenant_id}
                </h1>
                <p className="text-text-secondary text-sm font-mono">
                  {tenant.tenant_id}
                </p>
              </div>
            </div>

            {/* Settings Tabs */}
            <Card className="p-6">
              <Tabs tabs={tabs} defaultTab="general" />
            </Card>
          </motion.div>
        </div>
      </div>
    </div>
  );
}

