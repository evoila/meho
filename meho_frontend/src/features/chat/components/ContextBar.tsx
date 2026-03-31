// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ContextBar (Phase 63-02)
 *
 * Thin progress bar + warning banner showing context window consumption.
 * Positioned directly above the ChatInput component.
 *
 * - Green (<70%): healthy context usage
 * - Amber (70-89%): context getting full, warning banner appears
 * - Red (>=90%): context nearly exhausted, urgent warning
 *
 * "Start new chat" button creates a summarized handoff session.
 */
import { motion, AnimatePresence } from 'motion/react';
import { useChatStore } from '../stores';

interface ContextBarProps {
  onStartNewChat: () => void;
}

function getBarColor(percentage: number): string {
  if (percentage >= 90) return '#EF4444'; // red
  if (percentage >= 70) return '#F59E0B'; // amber
  return '#22C55E'; // green
}

function getBannerClasses(percentage: number): string {
  if (percentage >= 90) {
    return 'bg-red-500/10 border-red-500/30 text-red-400';
  }
  return 'bg-amber-500/10 border-amber-500/30 text-amber-400';
}

export function ContextBar({ onStartNewChat }: ContextBarProps) {
  const contextUsage = useChatStore((s) => s.contextUsage);

  if (!contextUsage || contextUsage.percentage === 0) {
    return null;
  }

  const { percentage } = contextUsage;
  const barColor = getBarColor(percentage);
  const showWarning = percentage >= 70;

  return (
    <div className="w-full">
      {/* Warning banner (shown at 70%+) */}
      <AnimatePresence>
        {showWarning && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3 }}
            className={`mx-4 mb-1 px-3 py-2 rounded-md border text-xs flex items-center justify-between ${getBannerClasses(percentage)}`}
          >
            <span>
              Context is {percentage}% full. Consider starting a new chat to maintain response quality.
            </span>
            <button
              type="button"
              onClick={onStartNewChat}
              className="ml-3 px-2 py-0.5 rounded text-xs font-medium bg-white/10 hover:bg-white/20 transition-colors whitespace-nowrap"
            >
              Start new chat
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Thin progress bar */}
      <div className="w-full h-0.5 bg-muted/30">
        <div
          className="h-full transition-all duration-500 ease-out"
          style={{
            width: `${percentage}%`,
            backgroundColor: barColor,
          }}
        />
      </div>
    </div>
  );
}
