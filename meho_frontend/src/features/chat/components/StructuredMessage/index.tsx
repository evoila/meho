// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * StructuredMessage (Phase 62)
 *
 * Main structured message component for investigation responses.
 * Renders three sections in order:
 *   1. HypothesisSummary -- hypothesis pills (only if hypotheses exist)
 *   2. SummarySection -- compact summary with citation superscripts (always visible)
 *   3. ReasoningSection -- expandable accordion with connector segments (collapsed by default)
 *
 * Non-structured responses (passthrough, conversational) bypass this component entirely.
 */
import { useState, useCallback } from 'react';
import type { StructuredContent, CitationData } from '../../types';
import { DataTableModal } from '../DataTableModal';
import { useChatStore } from '../../stores';
import { SummarySection } from './SummarySection';
import { ReasoningSection } from './ReasoningSection';
import { HypothesisSummary } from './HypothesisSummary';

interface StructuredMessageProps {
  structuredContent: StructuredContent;
  citations?: Record<string, CitationData>;
}

export function StructuredMessage({
  structuredContent,
  citations = {},
}: Readonly<StructuredMessageProps>) {
  const [isReasoningExpanded, setIsReasoningExpanded] = useState(false);
  const [dataTableState, setDataTableState] = useState<{
    table: string;
    sessionId: string;
  } | null>(null);

  const sessionId = useChatStore((s) => s.currentSessionId);

  const handleCitationClick = useCallback((citation: CitationData) => {
    if (citation.dataRef) {
      setDataTableState({
        table: citation.dataRef.table,
        sessionId: citation.dataRef.session_id,
      });
    }
  }, []);

  return (
    <div className="text-text-primary">
      {/* 1. Hypothesis pills */}
      {structuredContent.hypotheses.length > 0 && (
        <HypothesisSummary hypotheses={structuredContent.hypotheses} />
      )}

      {/* 2. Compact summary with citation superscripts */}
      <SummarySection
        summary={structuredContent.summary}
        citations={citations}
        onCitationClick={handleCitationClick}
      />

      {/* 3. Expandable reasoning accordion */}
      {structuredContent.connectorSegments.length > 0 && (
        <ReasoningSection
          segments={structuredContent.connectorSegments}
          isExpanded={isReasoningExpanded}
          onToggle={() => setIsReasoningExpanded((prev) => !prev)}
        />
      )}

      {/* DataTableModal for citation click */}
      {dataTableState && sessionId && (
        <DataTableModal
          sessionId={dataTableState.sessionId}
          table={dataTableState.table}
          onClose={() => setDataTableState(null)}
        />
      )}
    </div>
  );
}
