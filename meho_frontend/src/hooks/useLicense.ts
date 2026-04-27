// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Hook for fetching edition status from the backend license endpoint.
 *
 * Returns LicenseInfo directly (not a query result) -- consumers only need
 * the data, not loading/error states. The loading state IS community mode:
 * defaults to COMMUNITY_DEFAULT until the fetch confirms enterprise edition.
 * This prevents a flash of enterprise UI items (Pitfall 5, UI-SPEC Loading
 * State Contract).
 *
 * On fetch failure, silently returns community defaults. No error toast,
 * no error banner (UI-SPEC Copywriting Contract).
 */
import { useQuery } from '@tanstack/react-query';
import { config } from '../lib/config';
import type { LicenseInfo } from '../lib/license';
import { COMMUNITY_DEFAULT } from '../lib/license';

export function useLicense(): LicenseInfo {
  const { data } = useQuery<LicenseInfo>({
    queryKey: ['license'],
    queryFn: async () => {
      const res = await fetch(`${config.apiURL}/api/v1/license`);
      if (!res.ok) return COMMUNITY_DEFAULT;
      return res.json();
    },
    staleTime: Infinity,
    retry: false,
    // No refetch -- edition does not change at runtime (per D-12)
    refetchOnWindowFocus: false,
    refetchOnMount: false,
    refetchOnReconnect: false,
  });

  return data ?? COMMUNITY_DEFAULT;
}
