// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Visibility Confirm Dialog
 *
 * Phase 38: Group Session Foundation
 * Confirmation dialog shown before upgrading session visibility.
 * Per locked decision: "This will make the session visible to all team members. Continue?"
 */
import { useEffect, useCallback } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Users } from 'lucide-react';

interface VisibilityConfirmDialogProps {
  isOpen: boolean;
  targetVisibility: string;
  onConfirm: () => void;
  onCancel: () => void;
  isLoading?: boolean;
}

export function VisibilityConfirmDialog({
  isOpen,
  targetVisibility,
  onConfirm,
  onCancel,
  isLoading = false,
}: Readonly<VisibilityConfirmDialogProps>) {
  // Close on Escape key
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isLoading) {
        onCancel();
      }
    },
    [onCancel, isLoading]
  );

  useEffect(() => {
    if (isOpen) {
      document.addEventListener('keydown', handleKeyDown);
      return () => document.removeEventListener('keydown', handleKeyDown);
    }
  }, [isOpen, handleKeyDown]);

  const visibilityLabel = targetVisibility === 'tenant' ? 'all tenant members' : 'all team members';

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget && !isLoading) {
              onCancel();
            }
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 10 }}
            transition={{ duration: 0.15 }}
            className="w-full max-w-md mx-4 rounded-2xl bg-surface border border-white/10 shadow-2xl overflow-hidden"
          >
            {/* Header */}
            <div className="px-6 pt-6 pb-4">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex items-center justify-center w-10 h-10 rounded-xl bg-primary/10">
                  <Users className="h-5 w-5 text-primary" />
                </div>
                <h3 className="text-lg font-semibold text-white">Share Investigation?</h3>
              </div>
              <p className="text-sm text-text-secondary leading-relaxed">
                This will make the session visible to {visibilityLabel}. Continue?
              </p>
              <p className="mt-2 text-xs text-text-tertiary">
                This action cannot be undone.
              </p>
            </div>

            {/* Actions */}
            <div className="px-6 pb-6 flex items-center gap-3">
              <button
                onClick={onCancel}
                disabled={isLoading}
                className="flex-1 px-4 py-2.5 text-sm font-medium text-text-secondary bg-surface hover:bg-surface-hover border border-white/10 rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed active:scale-[0.98]"
              >
                Cancel
              </button>
              <button
                onClick={onConfirm}
                disabled={isLoading}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-primary hover:bg-primary-hover rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed active:scale-[0.98]"
              >
                {isLoading ? (
                  <>
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    Sharing...
                  </>
                ) : (
                  <>
                    <Users className="h-4 w-4" />
                    Share with Team
                  </>
                )}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
