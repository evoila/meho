// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import type { ReactNode } from 'react';
import { Button } from '../ui/Button';
import { Inbox } from 'lucide-react';

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
}

/**
 * Standardized empty state component
 * Use this when a list/section has no content
 */
export function EmptyState({ 
  icon, 
  title, 
  description, 
  action 
}: Readonly<EmptyStateProps>) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-16 h-16 rounded-full bg-surface-hover flex items-center justify-center mb-4 border border-white/10">
        {icon || <Inbox className="w-8 h-8 text-text-tertiary" />}
      </div>
      <h3 className="text-lg font-semibold text-white mb-2">{title}</h3>
      {description && (
        <p className="text-text-secondary max-w-md mb-6">{description}</p>
      )}
      {action && (
        <Button onClick={action.onClick}>{action.label}</Button>
      )}
    </div>
  );
}

