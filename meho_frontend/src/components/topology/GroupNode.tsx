// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * GroupNode - Collapsible group node for topology visualization
 * 
 * Displays a collapsed group of related entities
 * that can be expanded to show individual entities.
 */

import { memo } from 'react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import { clsx } from 'clsx';
import { ChevronRight, ChevronDown } from 'lucide-react';

// Default styling for group nodes
const GROUP_STYLE = { icon: '📦', color: '#6B7280' };

export interface GroupNodeData extends Record<string, unknown> {
  id: string;
  parentId: string;
  childIds: string[];
  count: number;
  expanded: boolean;
  onToggle?: (groupId: string) => void;
}

export type GroupNodeType = Node<GroupNodeData, 'group'>;

function GroupNodeComponent({ data, selected }: NodeProps<GroupNodeType>) {
  const handleClick = (e?: React.MouseEvent | React.KeyboardEvent) => {
    e?.stopPropagation();
    if (data.onToggle) {
      data.onToggle(data.id);
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      className={clsx(
        'px-4 py-3 rounded-lg border-2 shadow-lg min-w-[120px]',
        'transition-all duration-200 cursor-pointer',
        'bg-gray-800/95 backdrop-blur-sm hover:bg-gray-700/95',
        selected ? 'border-purple-400 ring-2 ring-purple-400/30' : 'border-gray-600',
      )}
      onClick={handleClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleClick(); } }}
    >
      {/* Input handle (top) */}
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-gray-500 !w-3 !h-3 !border-2 !border-gray-800"
      />
      
      {/* Group content */}
      <div className="flex items-center gap-3">
        {/* Expand/collapse indicator */}
        <div className="text-gray-400">
          {data.expanded ? (
            <ChevronDown className="w-5 h-5" />
          ) : (
            <ChevronRight className="w-5 h-5" />
          )}
        </div>
        
        {/* Count badge */}
        <div 
          className="flex items-center justify-center w-10 h-10 rounded-lg text-lg font-bold"
          style={{ 
            backgroundColor: `${GROUP_STYLE.color}20`,
            color: GROUP_STYLE.color,
          }}
        >
          {data.count}
        </div>
        
        {/* Group icon */}
        <div className="flex flex-col">
          <span className="text-lg" role="img" aria-label="Entities">
            {GROUP_STYLE.icon}
          </span>
          <span 
            className="text-xs font-medium uppercase tracking-wide"
            style={{ color: GROUP_STYLE.color }}
          >
            Entities
          </span>
        </div>
      </div>
      
      {/* Hint text */}
      <div className="mt-2 text-xs text-gray-500 text-center">
        {data.expanded ? 'Click to collapse' : 'Click to expand'}
      </div>
      
      {/* Output handle (bottom) */}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-gray-500 !w-3 !h-3 !border-2 !border-gray-800"
      />
    </div>
  );
}

export const GroupNode = memo(GroupNodeComponent);
