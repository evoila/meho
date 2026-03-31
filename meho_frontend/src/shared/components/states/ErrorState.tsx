// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '../ui/Button';

interface ErrorStateProps {
  error: Error | string;
  title?: string;
  onRetry?: () => void;
}

/**
 * Standardized error state component
 * Use this when data fetching fails or an error occurs
 */
export function ErrorState({ 
  error, 
  title = 'Failed to load', 
  onRetry 
}: ErrorStateProps) {
  const message = error instanceof Error ? error.message : error;
  
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="w-12 h-12 rounded-full bg-red-500/10 flex items-center justify-center mb-4 border border-red-500/20">
        <AlertTriangle className="w-6 h-6 text-red-400" />
      </div>
      <h3 className="text-lg font-semibold text-white mb-2">{title}</h3>
      <p className="text-text-secondary max-w-md mb-6">{message}</p>
      {onRetry && (
        <Button 
          variant="secondary" 
          onClick={onRetry}
          className="gap-2"
        >
          <RefreshCw className="w-4 h-4" />
          Try Again
        </Button>
      )}
    </div>
  );
}

