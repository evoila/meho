// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Auth Loading Screen
 *
 * Full-screen loading indicator shown during the silent SSO check
 * on page refresh. Displays the MEHO logo and a spinner.
 */
import mehoLogo from '@/assets/meho-logo.svg';

export function AuthLoadingScreen() {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-background gap-6">
      <img src={mehoLogo} alt="MEHO" className="h-16 w-16 animate-pulse" />
      <div className="flex items-center gap-3">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        <span className="text-sm text-text-secondary">Authenticating...</span>
      </div>
    </div>
  );
}
