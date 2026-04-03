// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TopologyContextMenu - Custom right-click context menu for topology nodes
 *
 * Phase 61 Plan 02: Provides "Investigate this entity", "Show details",
 * and "Focus neighbors" actions on right-click.
 *
 * Renders as a positioned div with z-index >= 100 (above React Flow controls).
 * Closes on click outside, Escape key, or menu item selection.
 */

import { useEffect, useCallback, useRef } from 'react';
import { Search, Eye, Focus } from 'lucide-react';

interface TopologyContextMenuProps {
  nodeId: string;
  entityName: string;
  entityType: string;
  scope?: Record<string, unknown> | null;
  top?: number;
  left?: number;
  right?: number;
  bottom?: number;
  onClose: () => void;
  onInvestigate: (entityName: string, entityType: string, scope?: string) => void;
  onShowDetails: (nodeId: string) => void;
  onFocusNeighbors: (nodeId: string) => void;
}

export function TopologyContextMenu({
  nodeId,
  entityName,
  entityType,
  scope,
  top,
  left,
  right,
  bottom,
  onClose,
  onInvestigate,
  onShowDetails,
  onFocusNeighbors,
}: Readonly<TopologyContextMenuProps>) {
  const menuRef = useRef<HTMLDivElement>(null);

  // Build scope namespace string for investigation query
  const scopeNamespace = scope
    ? Object.entries(scope).map(([k, v]) => `${k} ${String(v)}`).join(', ')
    : undefined;

  // Close on click outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        onClose();
      }
    };

    // Delay adding listener to avoid immediate close from the right-click event
    const timer = setTimeout(() => {
      document.addEventListener('click', handleClickOutside);
    }, 0);

    return () => {
      clearTimeout(timer);
      document.removeEventListener('click', handleClickOutside);
    };
  }, [onClose]);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const handleInvestigate = useCallback(() => {
    onInvestigate(entityName, entityType, scopeNamespace);
    onClose();
  }, [entityName, entityType, scopeNamespace, onInvestigate, onClose]);

  const handleShowDetails = useCallback(() => {
    onShowDetails(nodeId);
    onClose();
  }, [nodeId, onShowDetails, onClose]);

  const handleFocusNeighbors = useCallback(() => {
    onFocusNeighbors(nodeId);
    onClose();
  }, [nodeId, onFocusNeighbors, onClose]);

  return (
    <div
      ref={menuRef}
      className="fixed bg-gray-800 border border-gray-700 rounded-lg shadow-xl min-w-[200px] py-1 overflow-hidden"
      style={{
        zIndex: 100,
        top: top !== undefined ? `${top}px` : undefined,
        left: left !== undefined ? `${left}px` : undefined,
        right: right !== undefined ? `${right}px` : undefined,
        bottom: bottom !== undefined ? `${bottom}px` : undefined,
      }}
    >
      {/* Header: entity name and type */}
      <div className="px-3 py-2 border-b border-gray-700">
        <div className="text-sm font-medium text-gray-200 truncate">{entityName}</div>
        <div className="text-xs text-gray-400">{entityType}</div>
      </div>

      {/* Menu items */}
      <div className="py-1">
        <button
          onClick={handleInvestigate}
          className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 rounded-sm transition-colors"
        >
          <Search className="w-4 h-4 text-blue-400" />
          Investigate this entity
        </button>

        <button
          onClick={handleShowDetails}
          className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 rounded-sm transition-colors"
        >
          <Eye className="w-4 h-4 text-gray-400" />
          Show details
        </button>

        <button
          onClick={handleFocusNeighbors}
          className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 rounded-sm transition-colors"
        >
          <Focus className="w-4 h-4 text-purple-400" />
          Focus neighbors
        </button>
      </div>
    </div>
  );
}
