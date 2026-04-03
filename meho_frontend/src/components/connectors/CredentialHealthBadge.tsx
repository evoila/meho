// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * CredentialHealthBadge
 *
 * Displays credential health state on connector list and detail pages.
 * Extends the existing CredentialAgeBadge pattern with unhealthy/expired states.
 *
 * Phase 75: CRED-07
 */
import { useState } from 'react';
import { AlertTriangle, XCircle } from 'lucide-react';

interface CredentialHealthBadgeProps {
  health: 'healthy' | 'unhealthy' | 'expired' | null;
  healthMessage?: string;
  updatedAt?: string;
}

export function CredentialHealthBadge({ health, healthMessage, updatedAt }: Readonly<CredentialHealthBadgeProps>) {
  const [now] = useState(() => Date.now());

  // Priority 1: Unhealthy -- red badge
  if (health === 'unhealthy') {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold bg-red-500/10 text-red-400 border border-red-500/20"
        title={healthMessage}
      >
        <XCircle className="w-3 h-3" />
        Unhealthy
      </span>
    );
  }

  // Priority 2: Expired -- amber badge
  if (health === 'expired') {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20"
        title={healthMessage}
      >
        <AlertTriangle className="w-3 h-3" />
        Expired
      </span>
    );
  }

  // Priority 3: Age-based warning for healthy/unknown credentials >90 days old
  if (updatedAt) {
    const days = Math.floor((now - new Date(updatedAt).getTime()) / (1000 * 60 * 60 * 24));
    if (days > 90) {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20">
          <AlertTriangle className="w-3 h-3" />
          Credentials {days}d old
        </span>
      );
    }
  }

  // Healthy or unknown with fresh credentials -- render nothing
  return null;
}
