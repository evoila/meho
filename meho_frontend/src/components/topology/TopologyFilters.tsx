// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TopologyFilters - Simplified filter bar for table-first topology (Phase 76)
 *
 * Features:
 * - Search input with magnifying glass icon (debounced 300ms)
 * - Connector filter: multi-select pill toggles
 * - Entity type filter: multi-select pill toggles
 * - Show stale toggle checkbox
 */

import { useState, useEffect, useRef } from 'react';
import { Search, X } from 'lucide-react';

interface TopologyFiltersProps {
  search: string;
  onSearchChange: (search: string) => void;
  selectedConnectors: string[];
  onConnectorsChange: (connectors: string[]) => void;
  selectedTypes: string[];
  onTypesChange: (types: string[]) => void;
  showStale: boolean;
  onShowStaleChange: (show: boolean) => void;
  availableConnectors: { id: string; name: string }[];
  availableTypes: string[];
}

export function TopologyFilters({
  search,
  onSearchChange,
  selectedConnectors,
  onConnectorsChange,
  selectedTypes,
  onTypesChange,
  showStale,
  onShowStaleChange,
  availableConnectors,
  availableTypes,
}: TopologyFiltersProps) {
  const [localSearch, setLocalSearch] = useState(search);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Debounce search input by 300ms
  useEffect(() => {
    debounceRef.current = setTimeout(() => {
      onSearchChange(localSearch);
    }, 300);
    return () => clearTimeout(debounceRef.current);
  }, [localSearch, onSearchChange]);

  // Sync external search prop changes
  useEffect(() => {
    setLocalSearch(search);
  }, [search]);

  const toggleConnector = (connectorId: string) => {
    if (selectedConnectors.includes(connectorId)) {
      onConnectorsChange(selectedConnectors.filter((c) => c !== connectorId));
    } else {
      onConnectorsChange([...selectedConnectors, connectorId]);
    }
  };

  const toggleType = (type: string) => {
    if (selectedTypes.includes(type)) {
      onTypesChange(selectedTypes.filter((t) => t !== type));
    } else {
      onTypesChange([...selectedTypes, type]);
    }
  };

  const hasFilters = search || selectedConnectors.length > 0 || selectedTypes.length > 0;

  const clearFilters = () => {
    setLocalSearch('');
    onSearchChange('');
    onConnectorsChange([]);
    onTypesChange([]);
  };

  return (
    <div className="bg-[--color-surface] border-b border-[--color-border] px-6 py-4 space-y-3">
      {/* Search + toggles row */}
      <div className="flex items-center gap-4">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[--color-text-tertiary]" />
          <input
            type="text"
            placeholder="Search entities..."
            value={localSearch}
            onChange={(e) => setLocalSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2 bg-[--color-background] border border-[--color-border] rounded-lg text-[--color-text-primary] placeholder-[--color-text-tertiary] focus:outline-none focus:border-[--color-primary] text-sm"
          />
          {localSearch && (
            <button
              onClick={() => {
                setLocalSearch('');
                onSearchChange('');
              }}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-[--color-text-tertiary] hover:text-[--color-text-primary]"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Show Stale Toggle */}
        <label className="flex items-center gap-2 text-sm text-[--color-text-secondary] cursor-pointer">
          <input
            type="checkbox"
            checked={showStale}
            onChange={(e) => onShowStaleChange(e.target.checked)}
            className="w-4 h-4 rounded border-[--color-border] bg-[--color-background] text-[--color-primary] focus:ring-[--color-primary] focus:ring-offset-[--color-background]"
          />
          Show stale
        </label>

        {/* Clear Filters */}
        {hasFilters && (
          <button
            onClick={clearFilters}
            className="text-sm text-[--color-primary] hover:text-[--color-primary-hover]"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Connector + Type filter pills */}
      {(availableConnectors.length > 0 || availableTypes.length > 0) && (
        <div className="flex items-center gap-4 flex-wrap">
          {/* Connector pills */}
          {availableConnectors.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-[--color-text-tertiary] uppercase tracking-wide">
                Connector:
              </span>
              {availableConnectors.map((connector) => {
                const isSelected = selectedConnectors.includes(connector.id);
                return (
                  <button
                    key={connector.id}
                    onClick={() => toggleConnector(connector.id)}
                    className={`px-2 py-1 text-xs rounded-full transition-colors border ${
                      isSelected
                        ? 'bg-[--color-primary]/15 text-[--color-primary] border-[--color-primary]/30'
                        : 'bg-[--color-background] text-[--color-text-secondary] border-[--color-border] hover:border-[--color-border-hover]'
                    }`}
                  >
                    {connector.name}
                  </button>
                );
              })}
            </div>
          )}

          {/* Type pills */}
          {availableTypes.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-[--color-text-tertiary] uppercase tracking-wide">
                Type:
              </span>
              {availableTypes.map((type) => {
                const isSelected = selectedTypes.includes(type);
                return (
                  <button
                    key={type}
                    onClick={() => toggleType(type)}
                    className={`px-2 py-1 text-xs rounded-full transition-colors border ${
                      isSelected
                        ? 'bg-[--color-primary]/15 text-[--color-primary] border-[--color-primary]/30'
                        : 'bg-[--color-background] text-[--color-text-secondary] border-[--color-border] hover:border-[--color-border-hover]'
                    }`}
                  >
                    {type}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
