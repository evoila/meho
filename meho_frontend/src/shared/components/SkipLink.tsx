// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { cn } from '../lib/cn';

/**
 * Skip Link Component
 * 
 * Allows keyboard users to skip directly to main content.
 * Visible only when focused.
 */
export function SkipLink() {
  return (
    <a
      href="#main-content"
      className={cn(
        // Hidden by default, shown on focus
        'sr-only focus:not-sr-only',
        // Position and z-index
        'fixed top-4 left-4 z-[100]',
        // Styling
        'bg-primary text-white px-4 py-2 rounded-lg',
        'font-medium text-sm',
        // Focus state
        'focus:outline-none focus:ring-2 focus:ring-white focus:ring-offset-2 focus:ring-offset-background',
        // Animation
        'transition-all'
      )}
    >
      Skip to main content
    </a>
  );
}

