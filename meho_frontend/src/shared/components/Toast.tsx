// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Toast Notifications
 * 
 * Wrapper around Sonner for consistent toast notifications.
 * Uses MEHO design tokens for styling.
 */
import { Toaster, toast } from 'sonner';
import type { ReactNode } from 'react';

interface ToastProviderProps {
  children: ReactNode;
}

/**
 * Toast Provider - Wrap your app with this to enable toast notifications
 */
export function ToastProvider({ children }: Readonly<ToastProviderProps>) {
  return (
    <>
      {children}
      <Toaster 
        theme="dark"
        position="bottom-right"
        toastOptions={{
          style: {
            background: 'var(--color-surface, #18181B)',
            border: '1px solid rgba(255, 255, 255, 0.1)',
            color: 'var(--color-text, #FAFAFA)',
          },
          classNames: {
            success: 'border-green-500/30',
            error: 'border-red-500/30',
            warning: 'border-amber-500/30',
            info: 'border-blue-500/30',
          },
        }}
        richColors
        closeButton
      />
    </>
  );
}

/**
 * Toast utility for showing notifications
 * 
 * Usage:
 * ```tsx
 * import { toast } from '@/shared';
 * 
 * toast.success('Connector created!');
 * toast.error('Failed to save');
 * toast.loading('Uploading...');
 * toast.info('This is an informational message');
 * ```
 */
export { toast };

