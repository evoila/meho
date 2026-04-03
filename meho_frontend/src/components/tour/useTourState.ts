// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useQuery } from '@tanstack/react-query';
import { useLicense } from '../../hooks/useLicense';
import { getAPIClient } from '../../lib/api-client';

const STORAGE_KEY = 'meho_tour_completed';

export function useTourState() {
  const license = useLicense();

  const { data: connectors, isSuccess, isError } = useQuery({
    queryKey: ['connectors'],
    queryFn: () => getAPIClient().listConnectors(),
    staleTime: 30_000,
    retry: 1,
  });

  const completed = typeof globalThis.window !== 'undefined'
    && localStorage.getItem(STORAGE_KEY) === 'true';

  const hasConnectors = isSuccess && (connectors?.length ?? 0) > 0;

  // Tour shows when: community edition, not completed, AND either:
  // - connectors loaded and empty, OR
  // - connectors query failed (show tour anyway -- better UX than silent nothing)
  const dataReady = isSuccess || isError;
  const shouldShowTour =
    license.edition === 'community'
    && !completed
    && !hasConnectors
    && dataReady;

  const completeTour = () => {
    localStorage.setItem(STORAGE_KEY, 'true');
  };

  return { shouldShowTour, completeTour };
}
