// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Danger Zone Tab
 * 
 * Dangerous actions: disable tenant, delete tenant.
 */
import { useState } from 'react';
import { AlertTriangle, Power, PowerOff } from 'lucide-react';
import { Button, Badge } from '@/shared';
import type { Tenant } from '@/api/types';

interface TenantDangerZoneTabProps {
  tenant: Tenant;
  onDisable: () => Promise<void>;
  onEnable: () => Promise<void>;
  isDisabling: boolean;
  isEnabling: boolean;
}

export function TenantDangerZoneTab({
  tenant,
  onDisable,
  onEnable,
  isDisabling,
  isEnabling,
}: TenantDangerZoneTabProps) {
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [confirmText, setConfirmText] = useState('');

  const handleDisable = async () => {
    if (confirmText === tenant.tenant_id) {
      await onDisable();
      setConfirmDisable(false);
      setConfirmText('');
    }
  };

  const handleEnable = async () => {
    await onEnable();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3 text-red-400">
        <AlertTriangle className="h-5 w-5" />
        <h3 className="text-lg font-medium">Danger Zone</h3>
      </div>

      <p className="text-sm text-text-secondary">
        Actions in this section can have significant impact. Proceed with caution.
      </p>

      {/* Current Status */}
      <div className="p-4 bg-surface/50 border border-border rounded-lg">
        <div className="flex items-center justify-between">
          <div>
            <h4 className="text-sm font-medium text-white">Current Status</h4>
            <p className="text-xs text-text-secondary mt-1">
              {tenant.is_active 
                ? 'This tenant is active and users can access the system.'
                : 'This tenant is disabled. Users cannot log in or access the system.'}
            </p>
          </div>
          <Badge variant={tenant.is_active ? 'success' : 'error'}>
            {tenant.is_active ? 'Active' : 'Disabled'}
          </Badge>
        </div>
      </div>

      {/* Enable/Disable Action */}
      {tenant.is_active ? (
        <div className="p-4 border border-red-500/30 rounded-lg bg-red-500/5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h4 className="text-sm font-medium text-red-400 flex items-center gap-2">
                <PowerOff className="h-4 w-4" />
                Disable Tenant
              </h4>
              <p className="text-sm text-text-secondary mt-1">
                Disabling this tenant will:
              </p>
              <ul className="text-sm text-text-secondary mt-2 list-disc list-inside space-y-1">
                <li>Prevent all users from logging in</li>
                <li>Disable the Keycloak realm (if enabled)</li>
                <li>Keep all data intact for re-enablement</li>
              </ul>
            </div>
            {!confirmDisable ? (
              <Button
                variant="danger"
                onClick={() => setConfirmDisable(true)}
                disabled={isDisabling}
              >
                Disable Tenant
              </Button>
            ) : null}
          </div>

          {confirmDisable && (
            <div className="mt-4 pt-4 border-t border-red-500/30">
              <p className="text-sm text-white mb-3">
                Type <code className="px-2 py-1 bg-red-500/20 rounded font-mono">{tenant.tenant_id}</code> to confirm:
              </p>
              <div className="flex items-center gap-3">
                <input
                  type="text"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={tenant.tenant_id}
                  className="flex-1 px-3 py-2 bg-surface border border-red-500/50 rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50"
                  disabled={isDisabling}
                />
                <Button
                  variant="danger"
                  onClick={handleDisable}
                  isLoading={isDisabling}
                  disabled={confirmText !== tenant.tenant_id}
                >
                  Confirm Disable
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => {
                    setConfirmDisable(false);
                    setConfirmText('');
                  }}
                  disabled={isDisabling}
                >
                  Cancel
                </Button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="p-4 border border-green-500/30 rounded-lg bg-green-500/5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h4 className="text-sm font-medium text-green-400 flex items-center gap-2">
                <Power className="h-4 w-4" />
                Enable Tenant
              </h4>
              <p className="text-sm text-text-secondary mt-1">
                Re-enable this tenant to restore access for all users.
              </p>
              <ul className="text-sm text-text-secondary mt-2 list-disc list-inside space-y-1">
                <li>Users will be able to log in again</li>
                <li>The Keycloak realm will be re-enabled</li>
                <li>All existing data will be accessible</li>
              </ul>
            </div>
            <Button
              variant="primary"
              onClick={handleEnable}
              isLoading={isEnabling}
            >
              <Power className="h-4 w-4 mr-2" />
              Enable Tenant
            </Button>
          </div>
        </div>
      )}

      {/* Delete Tenant (Placeholder) */}
      <div className="p-4 border border-border rounded-lg bg-surface/30 opacity-60">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h4 className="text-sm font-medium text-text-secondary flex items-center gap-2">
              <AlertTriangle className="h-4 w-4" />
              Delete Tenant (Coming Soon)
            </h4>
            <p className="text-sm text-text-secondary mt-1">
              Permanently delete this tenant and all associated data.
              This action cannot be undone.
            </p>
          </div>
          <Button variant="danger" disabled>
            Delete Tenant
          </Button>
        </div>
      </div>
    </div>
  );
}

