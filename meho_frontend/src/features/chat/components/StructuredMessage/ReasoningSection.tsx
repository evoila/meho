// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ReasoningSection (Phase 62)
 *
 * Expandable reasoning accordion with connector-segmented narrative.
 * Uses motion/react AnimatePresence for smooth expand/collapse animation.
 * Renders each connector segment with a ConnectorSegment component.
 */
import type { RefObject } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ChevronDown } from 'lucide-react';
import { ConnectorSegment } from './ConnectorSegment';

interface ReasoningSectionProps {
  segments: Array<{ connectorName: string; content: string }>;
  isExpanded: boolean;
  onToggle: () => void;
  expandedRef?: RefObject<HTMLDivElement>;
}

export function ReasoningSection({
  segments,
  isExpanded,
  onToggle,
  expandedRef,
}: ReasoningSectionProps) {
  if (!segments || segments.length === 0) return null;

  return (
    <div className="mt-3">
      {/* Toggle button */}
      <button
        type="button"
        onClick={onToggle}
        className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
      >
        <ChevronDown
          className={`w-3.5 h-3.5 transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`}
        />
        {isExpanded ? 'Hide reasoning' : 'Show reasoning'}
      </button>

      {/* Expandable content */}
      <AnimatePresence initial={false}>
        {isExpanded && (
          <motion.div
            ref={expandedRef}
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeInOut' }}
            className="overflow-hidden"
          >
            <div className="border-l-2 border-slate-700 pl-4 ml-2 mt-3 text-text-secondary">
              {segments.map((segment, i) => (
                <ConnectorSegment
                  key={`${segment.connectorName}-${i}`}
                  connectorName={segment.connectorName}
                  content={segment.content}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
