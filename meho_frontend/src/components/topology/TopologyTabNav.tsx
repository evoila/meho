// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TopologyTabNav - Tab navigation for Topology Explorer page
 *
 * Three tabs: Entities, Connector Map, Suggestions.
 * Each tab displays a count badge. Suggestions tab uses amber
 * badge color when count > 0 to draw attention.
 */

import { clsx } from 'clsx';

export type TopologyTab = 'entities' | 'connector-map' | 'suggestions';

interface TopologyTabNavProps {
  activeTab: TopologyTab;
  onTabChange: (tab: TopologyTab) => void;
  entityCount: number;
  relationshipCount: number;
  suggestionCount: number;
}

const TABS: { id: TopologyTab; label: string; countKey: 'entityCount' | 'relationshipCount' | 'suggestionCount' }[] = [
  { id: 'entities', label: 'Entities', countKey: 'entityCount' },
  { id: 'connector-map', label: 'Connector Map', countKey: 'relationshipCount' },
  { id: 'suggestions', label: 'Suggestions', countKey: 'suggestionCount' },
];

export function TopologyTabNav({ activeTab, onTabChange, entityCount, relationshipCount, suggestionCount }: Readonly<TopologyTabNavProps>) {
  const counts = { entityCount, relationshipCount, suggestionCount };

  return (
    <div className="flex items-center gap-1 border-b border-[--color-border]">
      {TABS.map(tab => {
        const count = counts[tab.countKey];
        const isActive = activeTab === tab.id;
        const isSuggestion = tab.id === 'suggestions';

        return (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={clsx(
              'flex items-center gap-2 px-4 py-3 text-sm font-semibold transition-colors border-b-2 -mb-px',
              isActive
                ? 'text-[--color-text-primary] border-[--color-primary]'
                : 'text-[--color-text-secondary] border-transparent hover:text-[--color-text-primary] hover:border-[--color-border-hover]',
            )}
          >
            {tab.label}
            <span
              className={clsx(
                'px-1.5 py-0.5 text-xs rounded-full',
                (() => {
                  if (isActive) return 'bg-[--color-primary]/20 text-[--color-primary]';
                  if (isSuggestion && count > 0) return 'bg-amber-500/20 text-amber-400';
                  return 'bg-[--color-surface] text-[--color-text-secondary]';
                })(),
              )}
            >
              {count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
