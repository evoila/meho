// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Admin Dashboard Page - System Overview (global_admin only)
 * 
 * Features:
 * - System-wide statistics
 * - Recent activity feed
 * - Quick navigation to tenant management
 */
import { 
  LayoutDashboard, 
  Building2, 
  Cable, 
  Database, 
  AlertCircle,
  Activity,
  RefreshCw,
  ArrowRight,
  Clock,
  Plus,
  Zap,
  Users,
} from 'lucide-react';
import { motion } from 'motion/react';
import { Link } from 'react-router-dom';
import { useAdminDashboard } from '@/features/admin';
import { Button, Card, Badge } from '@/shared';
import { LoadingState, ErrorState } from '@/shared';
import type { ActivityItem, ActivityType } from '@/api/types';

/**
 * Stat Card Component
 */
interface StatCardProps {
  title: string;
  value: number | string;
  icon: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'error';
  subtitle?: string;
}

function StatCard({ title, value, icon, variant = 'default', subtitle }: StatCardProps) {
  const variantStyles = {
    default: 'text-primary bg-primary/10',
    success: 'text-emerald-400 bg-emerald-400/10',
    warning: 'text-amber-400 bg-amber-400/10',
    error: 'text-red-400 bg-red-400/10',
  };

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-text-tertiary text-sm font-medium">{title}</p>
          <p className="text-3xl font-bold text-white mt-1">{value}</p>
          {subtitle && (
            <p className="text-text-tertiary text-xs mt-1">{subtitle}</p>
          )}
        </div>
        <div className={`p-2 rounded-lg ${variantStyles[variant]}`}>
          {icon}
        </div>
      </div>
    </Card>
  );
}

/**
 * Activity Icon by Type
 */
function getActivityIcon(type: ActivityType) {
  switch (type) {
    case 'tenant_created':
      return <Plus className="h-4 w-4 text-emerald-400" />;
    case 'connector_added':
      return <Cable className="h-4 w-4 text-primary" />;
    case 'workflow_run':
      return <Zap className="h-4 w-4 text-amber-400" />;
    case 'error':
      return <AlertCircle className="h-4 w-4 text-red-400" />;
    default:
      return <Activity className="h-4 w-4 text-text-tertiary" />;
  }
}

/**
 * Activity Badge Variant
 */
function getActivityBadgeVariant(type: ActivityType): 'default' | 'success' | 'warning' | 'error' {
  switch (type) {
    case 'tenant_created':
      return 'success';
    case 'connector_added':
      return 'default';
    case 'workflow_run':
      return 'warning';
    case 'error':
      return 'error';
    default:
      return 'default';
  }
}

/**
 * Format relative time
 */
function formatRelativeTime(timestamp: string): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 1) return 'Just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

/**
 * Activity Item Component
 */
function ActivityItemRow({ item }: { item: ActivityItem }) {
  return (
    <div className="flex items-start gap-3 py-3 border-b border-border last:border-0">
      <div className="mt-0.5">
        {getActivityIcon(item.type)}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-white truncate">{item.description}</p>
        <div className="flex items-center gap-2 mt-1">
          <Badge 
            variant={getActivityBadgeVariant(item.type)} 
            size="sm"
          >
            {item.type.replace('_', ' ')}
          </Badge>
          {item.tenant_id && (
            <span className="text-xs text-text-tertiary">
              {item.tenant_id}
            </span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1 text-xs text-text-tertiary">
        <Clock className="h-3 w-3" />
        {formatRelativeTime(item.timestamp)}
      </div>
    </div>
  );
}

export function AdminDashboardPage() {
  const {
    stats,
    activity,
    isLoading,
    error,
    refetchAll,
    isLoadingStats,
  } = useAdminDashboard();

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <LoadingState message="Loading dashboard..." />
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <ErrorState
          title="Failed to load dashboard"
          error={error instanceof Error ? error : new Error(String(error))}
          onRetry={refetchAll}
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
                  <LayoutDashboard className="h-6 w-6 text-primary" />
                </div>
                <div>
                  <h1 className="text-2xl font-bold text-white">System Overview</h1>
                  <p className="text-text-secondary text-sm">
                    Monitor your MEHO installation
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={refetchAll}
                  disabled={isLoadingStats}
                  title="Refresh"
                >
                  <RefreshCw className={`h-4 w-4 ${isLoadingStats ? 'animate-spin' : ''}`} />
                </Button>
                <Link to="/admin/tenants">
                  <Button variant="secondary" size="sm">
                    <Building2 className="h-4 w-4 mr-2" />
                    Manage Tenants
                  </Button>
                </Link>
              </div>
            </div>

            {/* Stats Grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1 }}
              >
                <StatCard
                  title="Total Tenants"
                  value={stats?.total_tenants ?? 0}
                  icon={<Building2 className="h-5 w-5" />}
                  subtitle={`${stats?.active_tenants ?? 0} active`}
                />
              </motion.div>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.15 }}
              >
                <StatCard
                  title="Connectors"
                  value={stats?.total_connectors ?? 0}
                  icon={<Cable className="h-5 w-5" />}
                  variant="success"
                  subtitle="Across all tenants"
                />
              </motion.div>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.2 }}
              >
                <StatCard
                  title="Workflows Today"
                  value={stats?.workflows_today ?? 0}
                  icon={<Activity className="h-5 w-5" />}
                  variant="warning"
                  subtitle="Chat sessions started"
                />
              </motion.div>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.25 }}
              >
                <StatCard
                  title="Errors Today"
                  value={stats?.errors_today ?? 0}
                  icon={<AlertCircle className="h-5 w-5" />}
                  variant={stats?.errors_today && stats.errors_today > 0 ? 'error' : 'default'}
                  subtitle="Failed ingestion jobs"
                />
              </motion.div>
            </div>

            {/* Secondary Stats */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.3 }}
              >
                <Card className="p-4">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-indigo-500/10">
                      <Database className="h-5 w-5 text-indigo-400" />
                    </div>
                    <div>
                      <p className="text-text-tertiary text-sm">Knowledge Chunks</p>
                      <p className="text-xl font-bold text-white">
                        {stats?.knowledge_chunks?.toLocaleString() ?? 0}
                      </p>
                    </div>
                  </div>
                </Card>
              </motion.div>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.35 }}
              >
                <Card className="p-4">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-cyan-500/10">
                      <Users className="h-5 w-5 text-cyan-400" />
                    </div>
                    <div>
                      <p className="text-text-tertiary text-sm">Active Tenants</p>
                      <p className="text-xl font-bold text-white">
                        {stats?.active_tenants ?? 0} / {stats?.total_tenants ?? 0}
                      </p>
                    </div>
                  </div>
                </Card>
              </motion.div>
            </div>

            {/* Activity Feed */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.4 }}
            >
              <Card className="overflow-hidden">
                <div className="p-4 border-b border-border flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Activity className="h-5 w-5 text-primary" />
                    <h2 className="font-semibold text-white">Recent Activity</h2>
                  </div>
                  <Badge variant="default" size="sm">
                    {activity.length} events
                  </Badge>
                </div>
                
                <div className="p-4">
                  {activity.length === 0 ? (
                    <div className="text-center py-8 text-text-tertiary">
                      <Activity className="h-8 w-8 mx-auto mb-2 opacity-50" />
                      <p>No recent activity</p>
                    </div>
                  ) : (
                    <div className="space-y-0">
                      {activity.slice(0, 10).map((item) => (
                        <ActivityItemRow key={item.id} item={item} />
                      ))}
                    </div>
                  )}
                </div>

                {activity.length > 10 && (
                  <div className="p-4 border-t border-border">
                    <Link 
                      to="/admin/tenants" 
                      className="text-sm text-primary hover:underline flex items-center gap-1"
                    >
                      View all activity
                      <ArrowRight className="h-3 w-3" />
                    </Link>
                  </div>
                )}
              </Card>
            </motion.div>
          </motion.div>
        </div>
      </div>
    </div>
  );
}

