// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * License type definitions for edition-aware frontend.
 *
 * Matches the backend /api/v1/license response shape (Plan 80-01).
 */

export interface LicenseInfo {
  edition: 'community' | 'enterprise';
  features: string[];
  org: string | null;
  expires_at: string | null;
  in_grace_period: boolean;
}

/** Default license state: community mode. Used during loading and on fetch failure. */
export const COMMUNITY_DEFAULT: LicenseInfo = {
  edition: 'community',
  features: [],
  org: null,
  expires_at: null,
  in_grace_period: false,
};
