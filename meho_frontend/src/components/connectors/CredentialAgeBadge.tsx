// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * CredentialAgeBadge
 *
 * Displays an amber warning badge when connector credentials are older
 * than 90 days. Renders nothing if credentials are fresh.
 *
 * Uses `updatedAt` from the connector's `updated_at` field as a
 * reasonable approximation for credential freshness when a more
 * specific credential timestamp is not exposed in the API response.
 */
import { useState } from 'react';
import { AlertTriangle } from 'lucide-react';

interface CredentialAgeBadgeProps {
  updatedAt: string;
}

export function CredentialAgeBadge({ updatedAt }: Readonly<CredentialAgeBadgeProps>) {
  // Snapshot current time via lazy state initializer (purity-safe)
  const [now] = useState(() => Date.now());
  const days = Math.floor((now - new Date(updatedAt).getTime()) / (1000 * 60 * 60 * 24));

  if (days <= 90) return null;

  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
      <AlertTriangle className="w-3 h-3" />
      Credentials {days}d old
    </span>
  );
}
