// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Data Table Modal Component (03.1-02)
 *
 * Full-screen modal overlay with TanStack Table for exploring raw
 * infrastructure data. Supports sorting, filtering, and pagination.
 * This is the "holy shit" moment -- operators can explore raw data
 * from any connector without leaving the chat.
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table';
import { motion, AnimatePresence } from 'motion/react';
import { X, ChevronUp, ChevronDown, ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { config } from '@/lib/config';
import { useAuth } from '@/contexts/AuthContext';

interface DataTableModalProps {
  sessionId: string;
  table: string;
  onClose: () => void;
}

export function DataTableModal({ sessionId, table, onClose }: DataTableModalProps) {
  const { token } = useAuth();
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState('');

  // Fetch full data (up to 1000 rows, client-side sort/filter)
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['data-table', sessionId, table],
    queryFn: async () => {
      const res = await fetch(
        `${config.apiURL}/api/data/${sessionId}/${table}?page=1&size=1000`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<{
        rows: Record<string, unknown>[];
        total: number;
        columns: string[];
        table: string;
      }>;
    },
    enabled: !!token,
    staleTime: 5 * 60 * 1000,
  });

  // Generate column definitions dynamically from API response
  const columns = useMemo<ColumnDef<Record<string, unknown>>[]>(() => {
    if (!data?.columns) return [];
    return data.columns.map((col) => ({
      accessorKey: col,
      header: col,
      cell: (info) => {
        const value = info.getValue();
        if (value === null || value === undefined) return '-';
        if (typeof value === 'object') return JSON.stringify(value);
        return String(value);
      },
    }));
  }, [data?.columns]);

  // TanStack Table's useReactTable returns unstable function references by design.
  // This is a known limitation: https://github.com/TanStack/table/issues/5567
  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack Table API design, not a bug in our code
  const tableInstance = useReactTable({
    data: data?.rows ?? [],
    columns,
    state: {
      sorting,
      globalFilter,
    },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
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

  // Close on Escape key
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    },
    [onClose],
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    // Prevent body scroll while modal is open
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = '';
    };
  }, [handleKeyDown]);

  const modalContent = (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-[99999] bg-black/85 backdrop-blur-sm flex items-center justify-center"
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.95 }}
          transition={{ duration: 0.15 }}
          className="bg-slate-900 border border-slate-700/50 rounded-xl shadow-2xl w-[90vw] h-[85vh] flex flex-col overflow-hidden"
        >
          {/* Header */}
          <div className="px-6 py-4 border-b border-slate-800 flex items-center gap-4 flex-shrink-0">
            <h2 className="text-lg font-semibold text-slate-100 truncate">{table}</h2>
            {data && (
              <span className="text-xs text-slate-500">
                {data.total} row{data.total !== 1 ? 's' : ''} / {data.columns.length} column{data.columns.length !== 1 ? 's' : ''}
              </span>
            )}

            {/* Global filter */}
            <div className="flex-1 max-w-md ml-auto relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
              <input
                type="text"
                placeholder="Filter all columns..."
                value={globalFilter}
                onChange={(e) => setGlobalFilter(e.target.value)}
                className="w-full text-sm bg-slate-800 border border-slate-700 rounded-md pl-8 pr-3 py-2 text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-600/50"
              />
            </div>

            {/* Close button */}
            <button
              onClick={onClose}
              className="p-1.5 rounded-md text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-auto">
            {isLoading && (
              <div className="flex items-center justify-center h-full">
                <div className="text-slate-500 text-sm">Loading data...</div>
              </div>
            )}

            {isError && (
              <div className="flex items-center justify-center h-full">
                <div className="text-red-400 text-sm">
                  Failed to load data: {error instanceof Error ? error.message : 'Unknown error'}
                </div>
              </div>
            )}

            {data && (
              <table className="w-full">
                <thead className="sticky top-0 z-10">
                  {tableInstance.getHeaderGroups().map((headerGroup) => (
                    <tr key={headerGroup.id}>
                      {headerGroup.headers.map((header) => (
                        <th
                          key={header.id}
                          onClick={header.column.getToggleSortingHandler()}
                          className="px-4 py-3 text-xs text-slate-400 uppercase tracking-wider bg-slate-800/90 backdrop-blur-sm cursor-pointer hover:bg-slate-800 select-none text-left border-b border-slate-700/50"
                        >
                          <div className="flex items-center gap-1">
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            {{
                              asc: <ChevronUp className="w-3 h-3 text-cyan-400" />,
                              desc: <ChevronDown className="w-3 h-3 text-cyan-400" />,
                            }[header.column.getIsSorted() as string] ?? (
                              <span className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody>
                  {tableInstance.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      className="hover:bg-slate-800/30 transition-colors"
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td
                          key={cell.id}
                          className="px-4 py-2 text-sm text-slate-300 border-b border-slate-800/30 truncate max-w-[300px]"
                          title={String(cell.getValue() ?? '')}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Footer: Pagination */}
          {data && (
            <div className="px-6 py-3 border-t border-slate-800 flex items-center justify-between flex-shrink-0">
              <div className="text-xs text-slate-500">
                Showing {tableInstance.getState().pagination.pageIndex * tableInstance.getState().pagination.pageSize + 1}
                {' '}-{' '}
                {Math.min(
                  (tableInstance.getState().pagination.pageIndex + 1) * tableInstance.getState().pagination.pageSize,
                  tableInstance.getFilteredRowModel().rows.length,
                )}
                {' '}of {tableInstance.getFilteredRowModel().rows.length} rows
                {globalFilter && ` (filtered from ${data.total})`}
              </div>

              <div className="flex items-center gap-2">
                <button
                  onClick={() => tableInstance.previousPage()}
                  disabled={!tableInstance.getCanPreviousPage()}
                  className="p-1.5 rounded-md text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <span className="text-xs text-slate-400">
                  Page {tableInstance.getState().pagination.pageIndex + 1} of{' '}
                  {tableInstance.getPageCount()}
                </span>
                <button
                  onClick={() => tableInstance.nextPage()}
                  disabled={!tableInstance.getCanNextPage()}
                  className="p-1.5 rounded-md text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  return createPortal(modalContent, document.body);
}
