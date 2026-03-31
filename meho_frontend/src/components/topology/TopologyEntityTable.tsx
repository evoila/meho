// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TopologyEntityTable - TanStack React Table for entity browsing
 *
 * Primary view for the Topology Explorer Entities tab.
 * Sortable, filterable, paginated table with 6 columns:
 * Name, Type, Connector, Last Seen, SAME_AS count, Relationships count.
 *
 * Phase 76 Plan 04: Table-first topology UI (D-14, D-15, D-16).
 */

import { useState, useMemo, useEffect } from 'react';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  flexRender,
  createColumnHelper,
  type SortingState,
  type ColumnFiltersState,
} from '@tanstack/react-table';
import { ArrowUpDown, ChevronLeft, ChevronRight, Network } from 'lucide-react';
import { clsx } from 'clsx';
import type { TopologyEntity, TopologyRelationship, TopologySameAs } from '../../lib/topologyApi';
import { FreshnessLabel } from './FreshnessLabel';
import { ConnectorIcon } from './ConnectorIcon';

// ============================================================================
// Types
// ============================================================================

interface EntityRow {
  entity: TopologyEntity;
  sameAsCount: number;
  relationshipCount: number;
}

interface TopologyEntityTableProps {
  entities: TopologyEntity[];
  relationships: TopologyRelationship[];
  sameAs: TopologySameAs[];
  selectedEntityId?: string | null;
  onSelectEntity: (entity: TopologyEntity) => void;
  searchTerm?: string;
  connectorFilter?: string[];
  typeFilter?: string[];
  showStale?: boolean;
}

// ============================================================================
// Column definitions
// ============================================================================

const columnHelper = createColumnHelper<EntityRow>();

const columns = [
  columnHelper.accessor((row) => row.entity.name, {
    id: 'name',
    header: 'Name',
    cell: (info) => {
      const entity = info.row.original.entity;
      const isStale = !!entity.stale_at;
      return (
        <span
          className={clsx(
            'text-sm font-semibold',
            isStale
              ? 'text-[--color-text-tertiary] line-through'
              : 'text-[--color-text-primary]',
          )}
          title={entity.name}
        >
          {entity.name}
        </span>
      );
    },
    sortingFn: 'alphanumeric',
  }),

  columnHelper.accessor((row) => row.entity.entity_type, {
    id: 'type',
    header: 'Type',
    cell: (info) => (
      <span className="inline-flex px-2 py-0.5 text-xs rounded bg-[--color-surface] text-[--color-text-secondary] border border-[--color-border]">
        {info.getValue()}
      </span>
    ),
    sortingFn: 'alphanumeric',
  }),

  columnHelper.accessor((row) => row.entity.connector_type, {
    id: 'connector',
    header: 'Connector',
    cell: (info) => {
      const entity = info.row.original.entity;
      return (
        <div className="flex items-center gap-2">
          {entity.connector_type && (
            <ConnectorIcon connectorType={entity.connector_type} size={16} />
          )}
          <span className="text-sm text-[--color-text-secondary]">
            {entity.connector_type || 'N/A'}
          </span>
        </div>
      );
    },
    sortingFn: 'alphanumeric',
  }),

  columnHelper.accessor(
    (row) => row.entity.last_verified_at || row.entity.discovered_at,
    {
      id: 'lastSeen',
      header: 'Last Seen',
      cell: (info) => (
        <FreshnessLabel timestamp={info.getValue()} />
      ),
      sortingFn: 'datetime',
    },
  ),

  columnHelper.accessor((row) => row.sameAsCount, {
    id: 'sameAs',
    header: 'SAME_AS',
    cell: (info) => {
      const count = info.getValue();
      return count > 0 ? (
        <span className="inline-flex items-center justify-center min-w-[24px] px-1.5 py-0.5 text-xs rounded-full bg-amber-500/15 text-amber-400 font-medium">
          {count}
        </span>
      ) : (
        <span className="text-xs text-[--color-text-tertiary]">0</span>
      );
    },
    sortingFn: 'basic',
  }),

  columnHelper.accessor((row) => row.relationshipCount, {
    id: 'relationships',
    header: 'Relationships',
    cell: (info) => {
      const count = info.getValue();
      return count > 0 ? (
        <span className="text-sm text-[--color-text-secondary] font-medium">
          {count}
        </span>
      ) : (
        <span className="text-xs text-[--color-text-tertiary]">0</span>
      );
    },
    sortingFn: 'basic',
  }),
];

// ============================================================================
// Component
// ============================================================================

export function TopologyEntityTable({
  entities,
  relationships,
  sameAs,
  selectedEntityId,
  onSelectEntity,
  searchTerm = '',
  connectorFilter = [],
  typeFilter = [],
  showStale = false,
}: TopologyEntityTableProps) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'name', desc: false },
  ]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);

  // Build enriched rows with counts
  const data = useMemo<EntityRow[]>(() => {
    // Pre-compute SAME_AS counts per entity
    const sameAsCountMap = new Map<string, number>();
    for (const sa of sameAs) {
      sameAsCountMap.set(sa.entity_a_id, (sameAsCountMap.get(sa.entity_a_id) ?? 0) + 1);
      sameAsCountMap.set(sa.entity_b_id, (sameAsCountMap.get(sa.entity_b_id) ?? 0) + 1);
    }

    // Pre-compute relationship counts per entity
    const relCountMap = new Map<string, number>();
    for (const rel of relationships) {
      relCountMap.set(rel.from_entity_id, (relCountMap.get(rel.from_entity_id) ?? 0) + 1);
      relCountMap.set(rel.to_entity_id, (relCountMap.get(rel.to_entity_id) ?? 0) + 1);
    }

    let filtered = entities;

    // Filter out stale unless showStale is on
    if (!showStale) {
      filtered = filtered.filter((e) => !e.stale_at);
    }

    // Connector filter
    if (connectorFilter.length > 0) {
      filtered = filtered.filter(
        (e) => e.connector_id && connectorFilter.includes(e.connector_id),
      );
    }

    // Type filter
    if (typeFilter.length > 0) {
      filtered = filtered.filter((e) => typeFilter.includes(e.entity_type));
    }

    return filtered.map((entity) => ({
      entity,
      sameAsCount: sameAsCountMap.get(entity.id) ?? 0,
      relationshipCount: relCountMap.get(entity.id) ?? 0,
    }));
  }, [entities, relationships, sameAs, showStale, connectorFilter, typeFilter]);

  // Apply connector column filter when prop changes
  useEffect(() => {
    setColumnFilters((prev) => {
      const without = prev.filter((f) => f.id !== 'connector' && f.id !== 'type');
      return without;
    });
  }, [connectorFilter, typeFilter]);

  // TanStack Table's useReactTable returns unstable function references by design.
  // This is a known limitation: https://github.com/TanStack/table/issues/5567
  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack Table API design, not a bug in our code
  const table = useReactTable({
    data,
    columns,
    state: {
      sorting,
      globalFilter: searchTerm,
      columnFilters,
    },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const name = row.original.entity.name.toLowerCase();
      const description = row.original.entity.description?.toLowerCase() ?? '';
      const search = filterValue.toLowerCase();
      return name.includes(search) || description.includes(search);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: {
      pagination: {
        pageSize: 50,
      },
    },
  });

  // Empty state
  if (entities.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <Network className="w-12 h-12 text-[--color-text-tertiary] mb-4" />
        <h3 className="text-base font-semibold text-[--color-text-primary] mb-2">
          No topology data yet
        </h3>
        <p className="text-sm text-[--color-text-secondary] max-w-md">
          Topology builds automatically as MEHO investigates your infrastructure.
          Configure connectors and start an investigation to see your systems mapped here.
        </p>
      </div>
    );
  }

  const filteredRowCount = table.getFilteredRowModel().rows.length;
  const pageIndex = table.getState().pagination.pageIndex;
  const pageSize = table.getState().pagination.pageSize;
  const startRow = pageIndex * pageSize + 1;
  const endRow = Math.min((pageIndex + 1) * pageSize, filteredRowCount);

  return (
    <div className="flex flex-col h-full">
      {/* Table */}
      <div className="flex-1 overflow-auto">
        <table className="w-full">
          <thead className="sticky top-0 z-10">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    onClick={header.column.getToggleSortingHandler()}
                    className="px-4 py-3 text-xs uppercase tracking-wide text-[--color-text-secondary] bg-[--color-surface] cursor-pointer hover:bg-[--color-surface-hover] select-none text-left border-b border-[--color-border]"
                  >
                    <div className="flex items-center gap-1">
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                      <ArrowUpDown
                        className={clsx(
                          'w-3 h-3',
                          header.column.getIsSorted()
                            ? 'text-[--color-primary]'
                            : 'text-[--color-text-tertiary]',
                        )}
                      />
                    </div>
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => {
              const isSelected = row.original.entity.id === selectedEntityId;
              return (
                <tr
                  key={row.id}
                  onClick={() => onSelectEntity(row.original.entity)}
                  className={clsx(
                    'transition-colors cursor-pointer border-b border-[--color-border]',
                    isSelected
                      ? 'bg-[--color-surface-active] border-l-4 border-l-[--color-primary]'
                      : 'bg-[--color-background] hover:bg-[--color-surface-hover]',
                  )}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="px-4 py-3">
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {filteredRowCount > pageSize && (
        <div className="flex items-center justify-between px-4 py-3 border-t border-[--color-border] bg-[--color-surface]">
          <span className="text-xs text-[--color-text-secondary]">
            Showing {startRow}-{endRow} of {filteredRowCount} entities
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
              className="p-1.5 rounded text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-surface-hover] transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-xs text-[--color-text-secondary]">
              Page {pageIndex + 1} of {table.getPageCount()}
            </span>
            <button
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
              className="p-1.5 rounded text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-surface-hover] transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
