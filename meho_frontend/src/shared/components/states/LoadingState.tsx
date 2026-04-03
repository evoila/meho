// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { Spinner } from '../ui/Spinner';

interface LoadingStateProps {
  message?: string;
  size?: 'sm' | 'md' | 'lg';
}

/**
 * Standardized loading state component
 * Use this for consistent loading UI across the app
 */
export function LoadingState({ 
  message = 'Loading...', 
  size = 'md' 
}: Readonly<LoadingStateProps>) {
  return (
    <div className="flex flex-col items-center justify-center py-12">
      <Spinner size={size} />
      <p className="mt-4 text-text-secondary">{message}</p>
    </div>
  );
}

