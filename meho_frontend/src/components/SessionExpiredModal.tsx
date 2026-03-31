// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Session Expired Modal
 *
 * Full-screen overlay shown when the Keycloak session has expired and
 * silent token refresh has failed. Offers popup-based re-authentication
 * so the user can continue without losing their work (like Notion's
 * session recovery pattern).
 */
import { useState } from 'react';
import { ShieldAlert, RefreshCw } from 'lucide-react';

interface SessionExpiredModalProps {
  onReAuth: () => Promise<void>;
}

export function SessionExpiredModal({ onReAuth }: SessionExpiredModalProps) {
  const [isReAuthenticating, setIsReAuthenticating] = useState(false);

  const handleReAuth = async () => {
    setIsReAuthenticating(true);
    try {
      await onReAuth();
    } finally {
      setIsReAuthenticating(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[99999] flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-md mx-4 rounded-2xl bg-surface border border-border p-8 shadow-2xl">
        <div className="flex flex-col items-center text-center">
          {/* Icon */}
          <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-amber-500/10 ring-1 ring-amber-500/20">
            <ShieldAlert className="h-7 w-7 text-amber-400" />
          </div>

          {/* Title */}
          <h2 className="text-xl font-semibold text-white">Session Expired</h2>

          {/* Description */}
          <p className="mt-2 text-sm text-text-secondary leading-relaxed">
            Your session has expired. Click below to re-authenticate.
            Your work will not be lost.
          </p>

          {/* Re-auth button */}
          <button
            onClick={handleReAuth}
            disabled={isReAuthenticating}
            className="mt-6 w-full flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-primary to-accent px-6 py-3 text-sm font-medium text-white transition-all hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isReAuthenticating ? (
              <>
                <RefreshCw className="h-4 w-4 animate-spin" />
                Re-authenticating...
              </>
            ) : (
              <>
                <RefreshCw className="h-4 w-4" />
                Re-authenticate
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
