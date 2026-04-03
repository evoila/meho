// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * FollowUpChips (Phase 62)
 *
 * Suggestion pill buttons that appear below the last agent response
 * after synthesis completes. Clicking a chip sends it as a user message.
 *
 * Uses motion/react for a fade-in animation with a 0.5s delay to ensure
 * chips appear after the synthesis message is fully visible.
 */
import { motion } from 'motion/react';

interface FollowUpChipsProps {
  suggestions: string[];
  onSuggestionClick: (suggestion: string) => void;
}

export function FollowUpChips({ suggestions, onSuggestionClick }: Readonly<FollowUpChipsProps>) {
  if (!suggestions || suggestions.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: 0.5 }}
      className="flex items-center gap-2 mt-3 flex-wrap"
    >
      {suggestions.map((suggestion) => (
        <button
          key={`chip-${suggestion}`}
          type="button"
          onClick={() => onSuggestionClick(suggestion)}
          className="px-3 py-1.5 rounded-full text-xs border border-primary/30 text-primary/80 hover:bg-primary/10 hover:border-primary/50 transition-colors"
        >
          {suggestion.length > 100 ? suggestion.slice(0, 100) + '...' : suggestion}
        </button>
      ))}
    </motion.div>
  );
}
