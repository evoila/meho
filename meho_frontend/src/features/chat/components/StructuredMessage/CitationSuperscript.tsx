// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * CitationSuperscript (Phase 62)
 *
 * Clickable connector-colored superscript that opens DataTableModal
 * to explore raw infrastructure data linked to a citation source.
 */
import type { CitationData } from '../../types';
import { CONNECTOR_COLORS } from '@/components/topology/ConnectorIcon';

interface CitationSuperscriptProps {
  citationNum: string;
  citation: CitationData;
  onCitationClick: (citation: CitationData) => void;
}

export function CitationSuperscript({
  citationNum,
  citation,
  onCitationClick,
}: CitationSuperscriptProps) {
  const color = CONNECTOR_COLORS[citation.connectorType?.toLowerCase()] || '#94a3b8';

  return (
    <button
      type="button"
      className="cursor-pointer font-bold text-xs ml-0.5 hover:underline align-super bg-transparent border-none p-0 leading-none"
      style={{ color, fontSize: '0.75em', verticalAlign: 'super' }}
      title={`Source: ${citation.connectorName}`}
      onClick={(e) => {
        e.stopPropagation();
        onCitationClick(citation);
      }}
    >
      {citationNum}
    </button>
  );
}
