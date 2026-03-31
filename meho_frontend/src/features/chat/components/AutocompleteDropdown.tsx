// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * AutocompleteDropdown (Phase 63)
 *
 * Shared dropdown for both @ (connector mentions) and / (slash commands).
 * Positioned absolutely above the ChatInput. Uses AnimatePresence for
 * smooth enter/exit animations. Uses onMouseDown (not onClick) to prevent
 * textarea blur race condition (research pitfall 4).
 *
 * Max 5 items visible with overflow scroll if more.
 */
import { useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { ConnectorIcon } from '@/components/topology/ConnectorIcon';
import type { AutocompleteItem } from '../hooks/useAutocomplete';

interface AutocompleteDropdownProps {
  items: AutocompleteItem[];
  selectedIndex: number;
  onSelect: (item: AutocompleteItem) => void;
  visible: boolean;
  triggerType: '@' | '/' | null;
}

export function AutocompleteDropdown({
  items,
  selectedIndex,
  onSelect,
  visible,
  triggerType,
}: AutocompleteDropdownProps) {
  const listRef = useRef<HTMLDivElement>(null);

  // Auto-scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const selected = listRef.current.children[selectedIndex] as HTMLElement | undefined;
    selected?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  return (
    <AnimatePresence>
      {visible && items.length > 0 && (
        <motion.div
          ref={listRef}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 4 }}
          transition={{ duration: 0.15 }}
          className="absolute bottom-full mb-2 left-4 right-4 max-h-[240px] overflow-y-auto
                     bg-surface border border-white/10 rounded-xl shadow-2xl z-30"
          role="listbox"
          aria-label={triggerType === '@' ? 'Connector mentions' : 'Slash commands'}
        >
          {items.map((item, i) => (
            <button
              key={item.id}
              type="button"
              role="option"
              aria-selected={i === selectedIndex}
              onMouseDown={(e) => {
                e.preventDefault();
                onSelect(item);
              }}
              className={clsx(
                'w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors',
                i === selectedIndex
                  ? 'bg-primary/15 text-white'
                  : 'text-text-secondary hover:bg-white/5',
              )}
            >
              {/* Icon: ConnectorIcon for @ items, generic for / items */}
              {triggerType === '@' && item.connectorType ? (
                <ConnectorIcon connectorType={item.connectorType} size={20} />
              ) : (
                item.icon
              )}
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">{item.label}</div>
                {item.description && (
                  <div className="text-xs text-text-tertiary truncate">
                    {item.description}
                  </div>
                )}
              </div>
            </button>
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
