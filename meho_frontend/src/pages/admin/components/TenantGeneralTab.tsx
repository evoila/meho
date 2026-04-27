// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant General Tab
 * 
 * General settings: display name, subscription tier, status, email domains.
 * 
 * TASK-139 Phase 8: Added email domains field for tenant discovery.
 */
import { useState, useMemo } from 'react';
import { Input, Button, Badge } from '@/shared';
import { Globe } from 'lucide-react';
import type { Tenant, UpdateTenantRequest, SubscriptionTier } from '@/api/types';

interface TenantGeneralTabProps {
  tenant: Tenant;
  onUpdate: (request: UpdateTenantRequest) => Promise<void>;
  isUpdating: boolean;
}

/**
 * Validate email domain format
 */
function isValidDomain(domain: string): boolean {
  // Basic domain validation: at least one dot, alphanumeric with hyphens
  const domainRegex = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/i;
  return domainRegex.test(domain.trim());
}

/**
 * Parse comma-separated domains into array
 */
function parseDomainsInput(input: string): string[] {
  return input
    .split(',')
    .map(d => d.trim().toLowerCase())
    .filter(d => d.length > 0);
}

export function TenantGeneralTab({ tenant, onUpdate, isUpdating }: Readonly<TenantGeneralTabProps>) {
  // Initialize state from tenant props - component will be re-keyed when tenant changes
  const [displayName, setDisplayName] = useState(tenant.display_name || '');
  const [subscriptionTier, setSubscriptionTier] = useState<SubscriptionTier>(tenant.subscription_tier);
  const [emailDomainsInput, setEmailDomainsInput] = useState(
    (tenant.email_domains || []).join(', ')
  );
  const [domainError, setDomainError] = useState('');

  // Parse domains for validation
  const parsedDomains = useMemo(() => parseDomainsInput(emailDomainsInput), [emailDomainsInput]);
  const invalidDomains = useMemo(
    () => parsedDomains.filter(d => !isValidDomain(d)),
    [parsedDomains]
  );

  // Derive hasChanges from current state vs tenant props
  const hasChanges = useMemo(() => {
    const currentDomains = parsedDomains.sort().join(',');
    const originalDomains = (tenant.email_domains || []).sort().join(',');
    
    return (
      displayName !== (tenant.display_name || '') ||
      subscriptionTier !== tenant.subscription_tier ||
      currentDomains !== originalDomains
    );
  }, [displayName, subscriptionTier, parsedDomains, tenant.display_name, tenant.subscription_tier, tenant.email_domains]);

  const handleDomainsChange = (value: string) => {
    setEmailDomainsInput(value);
    
    // Validate domains
    const domains = parseDomainsInput(value);
    const invalid = domains.filter(d => !isValidDomain(d));
    
    if (invalid.length > 0) {
      setDomainError(`Invalid domain format: ${invalid.join(', ')}`);
    } else {
      setDomainError('');
    }
  };

  const handleSave = async () => {
    // Validate domains before saving
    if (invalidDomains.length > 0) {
      setDomainError(`Invalid domain format: ${invalidDomains.join(', ')}`);
      return;
    }

    await onUpdate({
      display_name: displayName,
      subscription_tier: subscriptionTier,
      email_domains: parsedDomains,
    });
  };

  const canSave = hasChanges && invalidDomains.length === 0;

  return (
    <div className="space-y-6">
      {/* Tenant ID (read-only) */}
      <div>
        <span className="block text-sm font-medium text-text-primary mb-2">
          Tenant ID
        </span>
        <div className="flex items-center gap-3">
          <code className="px-3 py-2 bg-surface border border-border rounded-lg text-white font-mono">
            {tenant.tenant_id}
          </code>
          <Badge variant={tenant.is_active ? 'success' : 'error'}>
            {tenant.is_active ? 'Active' : 'Disabled'}
          </Badge>
        </div>
        <p className="mt-1.5 text-xs text-text-secondary">
          The tenant ID cannot be changed after creation.
        </p>
      </div>

      {/* Display Name */}
      <div>
        <Input
          label="Display Name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="Acme Corporation"
          hint="Human-readable name for the organization."
          disabled={isUpdating}
        />
      </div>

      {/* Email Domains (TASK-139 Phase 8) */}
      <div>
        <label htmlFor="tenant-email-domains" className="block text-sm font-medium text-text-primary mb-2">
          <Globe className="inline-block w-4 h-4 mr-1.5 text-text-secondary" />
          Email Domains
        </label>
        <input
          id="tenant-email-domains"
          type="text"
          value={emailDomainsInput}
          onChange={(e) => handleDomainsChange(e.target.value)}
          placeholder="acme.com, acme.org, acme.co.uk"
          className={`w-full px-3 py-2 bg-surface border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all ${
            domainError ? 'border-red-500/50' : 'border-border'
          }`}
          disabled={isUpdating}
        />
        {domainError ? (
          <p className="mt-1.5 text-xs text-red-400">{domainError}</p>
        ) : (
          <p className="mt-1.5 text-xs text-text-secondary">
            Users with these email domains will be directed to this organization&apos;s SSO login.
            Separate multiple domains with commas.
          </p>
        )}
        {parsedDomains.length > 0 && invalidDomains.length === 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {parsedDomains.map(domain => (
              <span
                key={domain}
                className="px-2 py-0.5 bg-primary/10 text-primary text-xs rounded-md font-mono"
              >
                @{domain}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Subscription Tier */}
      <div>
        <label htmlFor="tenant-subscription-tier" className="block text-sm font-medium text-text-primary mb-2">
          Subscription Tier
        </label>
        <select
          id="tenant-subscription-tier"
          value={subscriptionTier}
          onChange={(e) => setSubscriptionTier(e.target.value as SubscriptionTier)}
          className="w-full max-w-xs px-3 py-2 bg-surface border border-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-primary/50"
          disabled={isUpdating}
        >
          <option value="free">Free</option>
          <option value="pro">Pro</option>
          <option value="enterprise">Enterprise</option>
        </select>
        <p className="mt-1.5 text-xs text-text-secondary">
          Determines available features and default quotas.
        </p>
      </div>

      {/* Keycloak Realm Status */}
      {tenant.keycloak_realm_enabled !== null && (
        <div>
          <span className="block text-sm font-medium text-text-primary mb-2">
            Keycloak Realm
          </span>
          <Badge variant={tenant.keycloak_realm_enabled ? 'success' : 'warning'}>
            {tenant.keycloak_realm_enabled ? 'Enabled' : 'Disabled'}
          </Badge>
          <p className="mt-1.5 text-xs text-text-secondary">
            Keycloak realm status is managed via enable/disable actions.
          </p>
        </div>
      )}

      {/* Metadata */}
      <div className="border-t border-border pt-4">
        <h4 className="text-sm font-medium text-text-primary mb-3">Metadata</h4>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-text-secondary">Created:</span>
            <span className="ml-2 text-white">
              {tenant.created_at ? new Date(tenant.created_at).toLocaleString() : '-'}
            </span>
          </div>
          <div>
            <span className="text-text-secondary">Updated:</span>
            <span className="ml-2 text-white">
              {tenant.updated_at ? new Date(tenant.updated_at).toLocaleString() : '-'}
            </span>
          </div>
          {tenant.updated_by && (
            <div>
              <span className="text-text-secondary">Updated by:</span>
              <span className="ml-2 text-white">{tenant.updated_by}</span>
            </div>
          )}
        </div>
      </div>

      {/* Save Button */}
      <div className="flex justify-end pt-4 border-t border-border">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={!canSave}
          isLoading={isUpdating}
        >
          Save Changes
        </Button>
      </div>
    </div>
  );
}
