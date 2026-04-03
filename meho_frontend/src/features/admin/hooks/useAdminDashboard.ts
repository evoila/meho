// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Admin Dashboard Hook
 * 
 * Provides data fetching for the superadmin dashboard.
 * Fetches system-wide statistics and recent activity.
 * 
 * Uses manual refresh (no auto-refresh interval) for better UX -
 * user controls when data is refreshed via the refresh button.
 */
import { useQuery } from '@tanstack/react-query';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import type { DashboardStats, ActivityItem } from '@/api/types';

const QUERY_KEYS = {
  stats: 'admin-dashboard-stats',
  activity: 'admin-dashboard-activity',
} as const;

/**
 * Hook for admin dashboard data
 */
export function useAdminDashboard() {
  const apiClient = getAPIClient(config.apiURL);

  // Fetch dashboard stats (manual refresh only)
  const stats = useQuery<DashboardStats>({
    queryKey: [QUERY_KEYS.stats],
    queryFn: () => apiClient.getDashboardStats(),
    staleTime: 60000, // Consider data stale after 1 minute
  });

  // Fetch activity feed (manual refresh only)
  const activity = useQuery<ActivityItem[]>({
    queryKey: [QUERY_KEYS.activity],
    queryFn: () => apiClient.getDashboardActivity(20),
    staleTime: 60000,
  });

  return {
    // Stats data
    stats: stats.data,
    isLoadingStats: stats.isLoading,
    statsError: stats.error,
    refetchStats: stats.refetch,
    
    // Activity data
    activity: activity.data ?? [],
    isLoadingActivity: activity.isLoading,
    activityError: activity.error,
    refetchActivity: activity.refetch,
    
    // Combined state
    isLoading: stats.isLoading || activity.isLoading,
    error: stats.error || activity.error,
    refetchAll: () => {
      stats.refetch();
      activity.refetch();
    },
  };
}

