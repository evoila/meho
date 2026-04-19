// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Context Provider
 * 
 * Manages tenant context switching for superadmins.
 * Allows global admins to "enter" a tenant and operate within that tenant's context.
 * 
 * TASK-140 Phase 2: Tenant Context Switching
 */
import { 
  createContext, 
  useContext, 
  useState, 
  useCallback, 
  useEffect, 
  type ReactNode 
} from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { getAPIClient } from '../lib/api-client';
import { config } from '../lib/config';

// Session storage keys
const TENANT_CONTEXT_KEY = 'meho:tenant-context';
const TENANT_DISPLAY_NAME_KEY = 'meho:tenant-display-name';

interface TenantContextState {
  tenantId: string;
  displayName: string;
}

interface TenantContextType {
  /** Current tenant ID if in tenant context, null otherwise */
  currentTenant: string | null;
  /** Display name of the current tenant */
  tenantDisplayName: string | null;
  /** Whether the superadmin is currently operating in a tenant context */
  isInTenantContext: boolean;
  /** Enter a tenant's context to operate as that tenant */
  enterTenant: (tenantId: string, displayName: string) => void;
  /** Exit the current tenant context and return to admin view */
  exitTenant: () => void;
}

const TenantContext = createContext<TenantContextType | undefined>(undefined);

/**
 * Get initial state from sessionStorage
 */
function getInitialState(): TenantContextState | null {
  try {
    const tenantId = sessionStorage.getItem(TENANT_CONTEXT_KEY);
    const displayName = sessionStorage.getItem(TENANT_DISPLAY_NAME_KEY);
    
    if (tenantId && displayName) {
      // Restore API client state
      getAPIClient(config.apiURL).setTenantContext(tenantId);
      return { tenantId, displayName };
    }
  } catch {
    // sessionStorage not available or error
  }
  return null;
}

/**
 * Save state to sessionStorage
 */
function saveState(state: TenantContextState | null): void {
  try {
    if (state) {
      sessionStorage.setItem(TENANT_CONTEXT_KEY, state.tenantId);
      sessionStorage.setItem(TENANT_DISPLAY_NAME_KEY, state.displayName);
    } else {
      sessionStorage.removeItem(TENANT_CONTEXT_KEY);
      sessionStorage.removeItem(TENANT_DISPLAY_NAME_KEY);
    }
  } catch {
    // sessionStorage not available
  }
}

interface TenantContextProviderProps {
  children: ReactNode;
}

export function TenantContextProvider({ children }: Readonly<TenantContextProviderProps>) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  
  // Initialize from sessionStorage
  const [state, setState] = useState<TenantContextState | null>(getInitialState);
  
  // Sync state changes to sessionStorage and API client
  useEffect(() => {
    saveState(state);
    
    const client = getAPIClient(config.apiURL);
    if (state) {
      client.setTenantContext(state.tenantId);
    } else {
      client.clearTenantContext();
    }
  }, [state]);
  
  const enterTenant = useCallback((tenantId: string, displayName: string) => {
    setState({ tenantId, displayName });
    // Invalidate all cached queries so they refetch with new tenant context
    // This ensures the UI shows the target tenant's data, not cached superadmin data
    queryClient.invalidateQueries();
    // Navigate to the main app view (chat) when entering tenant context
    navigate('/chat', { replace: true });
  }, [navigate, queryClient]);
  
  const exitTenant = useCallback(() => {
    setState(null);
    // Invalidate all cached queries to refetch superadmin's own data
    queryClient.invalidateQueries();
    // Navigate back to admin dashboard when exiting tenant context
    navigate('/admin', { replace: true });
  }, [navigate, queryClient]);
  
  const value: TenantContextType = {
    currentTenant: state?.tenantId ?? null,
    tenantDisplayName: state?.displayName ?? null,
    isInTenantContext: state !== null,
    enterTenant,
    exitTenant,
  };
  
  return (
    <TenantContext.Provider value={value}>
      {children}
    </TenantContext.Provider>
  );
}

/**
 * Hook to access tenant context
 * 
 * @example
 * ```tsx
 * function MyComponent() {
 *   const { isInTenantContext, currentTenant, exitTenant } = useTenantContext();
 *   
 *   if (isInTenantContext) {
 *     return <div>Viewing as {currentTenant}</div>;
 *   }
 *   return <div>Normal view</div>;
 * }
 * ```
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useTenantContext(): TenantContextType {
  const context = useContext(TenantContext);
  if (context === undefined) {
    throw new Error('useTenantContext must be used within TenantContextProvider');
  }
  return context;
}

