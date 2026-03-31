// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Data Preview Component (03.1-02)
 *
 * Inline preview of data-bearing responses. Shows first 5 rows of the
 * first data_ref table in a compact table format, with a "View all N items"
 * button that opens the DataTableModal for full exploration.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Table } from 'lucide-react';
import { config } from '@/lib/config';
import { useAuth } from '@/contexts/AuthContext';
import { DataTableModal } from './DataTableModal';

interface DataPreviewProps {
  dataRefs: Array<{ table: string; session_id: string; row_count: number }>;
  sessionId: string;
}

export function DataPreview({ dataRefs, sessionId }: DataPreviewProps) {
  const { token } = useAuth();
  const [modalTable, setModalTable] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState(0);

  const activeRef = dataRefs[activeTab];

  // Fetch first 5 rows for preview
  const { data, isLoading, isError } = useQuery({
    queryKey: ['data-preview', sessionId, activeRef?.table],
    queryFn: async () => {
      const res = await fetch(
        `${config.apiURL}/api/data/${sessionId}/${activeRef.table}?page=1&size=5`,
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
    enabled: !!activeRef && !!token,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  if (!activeRef) return null;

  return (
    <>
      <div className="mt-3 rounded-lg bg-slate-900/80 border border-slate-700/50 overflow-hidden">
        {/* Table tabs (if multiple data_refs) */}
        {dataRefs.length > 1 && (
          <div className="flex gap-1 px-3 pt-2 pb-1 border-b border-slate-800/30">
            {dataRefs.map((ref, idx) => (
              <button
                key={ref.table}
                onClick={() => setActiveTab(idx)}
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                  idx === activeTab
                    ? 'bg-slate-700/60 text-slate-200'
                    : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                {ref.table}
              </button>
            ))}
          </div>
        )}

        {/* Loading skeleton */}
        {isLoading && (
          <div className="p-3 space-y-2">
            {[1, 2, 3].map((i) => (
              <div key={i} className="flex gap-3">
                <div className="h-3 w-24 bg-slate-800/60 rounded animate-pulse" />
                <div className="h-3 w-32 bg-slate-800/60 rounded animate-pulse" />
                <div className="h-3 w-20 bg-slate-800/60 rounded animate-pulse" />
              </div>
            ))}
          </div>
        )}

        {/* Error state */}
        {isError && (
          <div className="px-3 py-2 text-xs text-slate-500">
            Unable to load data preview
          </div>
        )}

        {/* Preview table */}
        {data && data.rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="bg-slate-800/60">
                  {data.columns.slice(0, 6).map((col) => (
                    <th
                      key={col}
                      className="px-3 py-1.5 text-left text-[10px] text-slate-400 uppercase tracking-wider font-medium"
                    >
                      {col}
                    </th>
                  ))}
                  {data.columns.length > 6 && (
                    <th className="px-3 py-1.5 text-left text-[10px] text-slate-500">
                      +{data.columns.length - 6} more
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, rowIdx) => (
                  <tr
                    key={rowIdx}
                    className="border-t border-slate-800/30"
                  >
                    {data.columns.slice(0, 6).map((col) => (
                      <td
                        key={col}
                        className="px-3 py-1 text-xs text-slate-300 truncate max-w-[200px]"
                        title={String(row[col] ?? '')}
                      >
                        {String(row[col] ?? '')}
                      </td>
                    ))}
                    {data.columns.length > 6 && (
                      <td className="px-3 py-1 text-xs text-slate-600">...</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* View all button */}
        <div className="px-3 py-2 border-t border-slate-800/30">
          <button
            onClick={() => setModalTable(activeRef.table)}
            className="text-xs text-cyan-400 hover:text-cyan-300 transition-colors inline-flex items-center gap-1.5"
          >
            <Table className="w-3 h-3" />
            View all {activeRef.row_count} items
          </button>
        </div>
      </div>

      {/* Data Table Modal */}
      {modalTable && (
        <DataTableModal
          sessionId={sessionId}
          table={modalTable}
          onClose={() => setModalTable(null)}
        />
      )}
    </>
  );
}
