// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SummarySection (Phase 62)
 *
 * Compact executive summary with inline citation superscripts.
 * Uses direct React element rendering (no react-markdown) since
 * the summary is 2-4 sentences with bold + citation markers only.
 */
import type { ReactNode } from 'react';
import type { CitationData } from '../../types';
import { processCitations } from '../../utils/parseSynthesis';
import { CitationSuperscript } from './CitationSuperscript';

interface SummarySectionProps {
  summary: string;
  citations: Record<string, CitationData>;
  onCitationClick: (citation: CitationData) => void;
}

/**
 * Render summary text as React elements with bold formatting
 * and inline CitationSuperscript components.
 *
 * 1. Process [src:step-N] markers into [^N] footnote markers via processCitations
 * 2. Split on [^N] and **bold** markers using regex capture groups
 * 3. Render text spans, bold spans, and CitationSuperscript components inline
 */
function renderSummaryText(
  text: string,
  citations: Record<string, CitationData>,
  onCitationClick: (citation: CitationData) => void,
): ReactNode[] {
  // Step 1: Process citation markers to get [^N] markers and stepMap
  const { processed, stepMap } = processCitations(text);

  // Step 2: Split on citation references [^N] and bold markers **text**
  const parts = processed.split(/(\[\^\d+\]|\*\*[^*]+\*\*)/);

  return parts.map((part, i) => {
    // Citation reference: [^N]
    const citationMatch = part.match(/^\[\^(\d+)\]$/);
    if (citationMatch) {
      const num = citationMatch[1];
      const stepId = stepMap[num];
      // Find the citation data by matching stepId to citation entries
      const citation = Object.entries(citations).find(
        ([, c]) => c.stepId === stepId,
      )?.[1] || citations[num];

      if (citation) {
        return (
          <CitationSuperscript
            key={`cite-${i}`}
            citationNum={num}
            citation={citation}
            onCitationClick={onCitationClick}
          />
        );
      }
      // Fallback: render as plain superscript if citation data not found
      return <sup key={`cite-${i}`} className="text-slate-400 text-xs ml-0.5">{num}</sup>;
    }

    // Bold text: **text**
    const boldMatch = part.match(/^\*\*(.+)\*\*$/);
    if (boldMatch) {
      return <strong key={`bold-${i}`} className="font-semibold text-text-primary">{boldMatch[1]}</strong>;
    }

    // Plain text
    if (part) {
      return <span key={`text-${i}`}>{part}</span>;
    }
    return null;
  });
}

export function SummarySection({
  summary,
  citations,
  onCitationClick,
}: SummarySectionProps) {
  const elements = renderSummaryText(summary, citations, onCitationClick);

  return (
    <div className="text-sm text-text-primary leading-relaxed">
      {elements}
    </div>
  );
}
