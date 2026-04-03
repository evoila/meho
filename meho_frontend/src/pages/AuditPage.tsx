// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * AuditPage
 *
 * Admin audit log page with two tabs:
 * 1. "Audit Trail" -- admin cross-user view (requires admin role)
 * 2. "My Activity" -- personal activity log (any authenticated user)
 *
 * Non-admins only see the "My Activity" tab.
 */
import { useState } from 'react';
import { Shield, User } from 'lucide-react';
import clsx from 'clsx';
import { useAuth } from '../contexts/AuthContext';
import { AuditTable, ActivityLog } from '../features/audit';

type Tab = 'audit-trail' | 'my-activity';

export function AuditPage() {
  const { user } = useAuth();
  const isAdmin = !!user?.isGlobalAdmin;

  const [activeTab, setActiveTab] = useState<Tab>(isAdmin ? 'audit-trail' : 'my-activity');

  const tabs = [
    ...(isAdmin
      ? [{ id: 'audit-trail' as Tab, label: 'Audit Trail', icon: Shield }]
      : []),
    { id: 'my-activity' as Tab, label: 'My Activity', icon: User },
  ];

  return (
    <div className="h-full overflow-hidden flex flex-col bg-background relative">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-secondary/5 rounded-full blur-[100px]" />
      </div>

      {/* Header */}
      <div className="glass border-b border-white/5 px-8 py-6 z-10">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Audit Log</h1>
          <p className="text-sm text-text-secondary mt-1">
            {isAdmin
              ? 'Review security events and user activity across your organization'
              : 'Review your recent activity'}
          </p>
        </div>

        {/* Tabs */}
        {tabs.length > 1 && (
          <div className="flex items-center gap-1 mt-4 bg-surface/50 rounded-lg p-1 w-fit">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={clsx(
                    'flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md transition-all',
                    isActive
                      ? 'bg-surface text-white shadow-sm'
                      : 'text-text-secondary hover:text-white',
                  )}
                >
                  <Icon className="h-4 w-4" />
                  {tab.label}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto z-10 p-8">
        <div className="max-w-7xl mx-auto">
          {activeTab === 'audit-trail' && isAdmin && <AuditTable />}
          {activeTab === 'my-activity' && <ActivityLog />}
        </div>
      </div>
    </div>
  );
}
