// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Context Banner
 * 
 * A sticky warning banner that displays when a superadmin is operating
 * within a tenant's context. Provides clear visual indication and a way
 * to exit the tenant context.
 * 
 * TASK-140 Phase 2: Tenant Context Switching
 */
import { AlertTriangle, X, Eye } from 'lucide-react';
import { useTenantContext } from '../../contexts/TenantContext';

export function TenantContextBanner() {
  const { isInTenantContext, tenantDisplayName, currentTenant, exitTenant } = useTenantContext();
  
  // Don't render anything if not in tenant context
  if (!isInTenantContext) {
    return null;
  }
  
  return (
    <div 
      className="fixed top-0 left-0 right-0 z-50 bg-amber-500 text-amber-950 shadow-lg"
      role="alert"
      aria-live="polite"
    >
      <div className="max-w-screen-2xl mx-auto px-4 py-2 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Eye className="h-5 w-5" aria-hidden="true" />
            <AlertTriangle className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="flex flex-col sm:flex-row sm:items-center sm:gap-2">
            <span className="font-semibold">
              Viewing as: <strong>{currentTenant}</strong>
              {tenantDisplayName && tenantDisplayName !== currentTenant && (
                <span className="font-normal text-amber-800"> ({tenantDisplayName})</span>
              )}
            </span>
            <span className="text-amber-800 text-sm hidden sm:inline">
              — Actions are logged. Credentials are hidden.
            </span>
          </div>
        </div>
        
        <button
          onClick={exitTenant}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-lg transition-colors font-medium text-sm"
          aria-label="Exit tenant context"
        >
          <span>Exit Context</span>
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

