// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Frontend synthesis parser (Phase 62)
 *
 * Parses structured XML sections from synthesis output.
 * Mirrors backend synthesis_parser.py logic for consistent parsing.
 */
import type { StructuredContent } from '../types';

const SUMMARY_RE = /<summary>([\s\S]*?)<\/summary>/;
const REASONING_RE = /<reasoning>([\s\S]*?)<\/reasoning>/;
const HYPOTHESES_RE = /<hypotheses>([\s\S]*?)<\/hypotheses>/;
const HYPOTHESIS_ITEM_RE = /<hypothesis\s+status="([^"]+)">([\s\S]*?)<\/hypothesis>/g;
const CONNECTOR_SEGMENT_RE = /\[connector:([^\]]+)\]/;

/**
 * Parse structured synthesis XML into typed sections.
 * Returns null if text is not structured (no <summary> tag).
 */
export function parseSynthesis(text: string): StructuredContent | null {
  const summaryMatch = text.match(SUMMARY_RE);
  if (!summaryMatch) return null;

  const reasoningMatch = text.match(REASONING_RE);
  const hypothesesMatch = text.match(HYPOTHESES_RE);

  // Parse hypotheses
  const hypotheses: Array<{ text: string; status: string }> = [];
  if (hypothesesMatch) {
    let match;
    const re = new RegExp(HYPOTHESIS_ITEM_RE.source, 'gs');
    while ((match = re.exec(hypothesesMatch[1])) !== null) {
      hypotheses.push({ status: match[1], text: match[2].trim() });
    }
  }

  // Parse connector segments from reasoning
  const reasoningText = reasoningMatch?.[1]?.trim() || '';
  const connectorSegments: Array<{ connectorName: string; content: string }> = [];
  if (reasoningText) {
    const parts = reasoningText.split(CONNECTOR_SEGMENT_RE);
    // parts: [before, connectorName, content, connectorName, content, ...]
    for (let i = 1; i < parts.length - 1; i += 2) {
      connectorSegments.push({
        connectorName: parts[i].trim(),
        content: (parts[i + 1] || '').trim(),
      });
    }
  }

  return {
    summary: summaryMatch[1].trim(),
    reasoning: reasoningText,
    hypotheses,
    connectorSegments,
  };
}

const STATUS_ICON: Record<string, string> = {
  validated: '\u2713',
  invalidated: '\u2717',
  inconclusive: '?',
  investigating: '...',
};

/**
 * Strip synthesis XML tags from content, converting to clean markdown.
 *
 * Used when parseSynthesis returns null (no <summary> detected) so the
 * plain Message component gets readable markdown instead of raw XML.
 *
 * Conversions:
 *  - <follow_ups>...</follow_ups>  → removed (rendered via SSE event)
 *  - <hypotheses>...</hypotheses>  → markdown bullet list with status icons
 *  - <summary>/<reasoning> wrappers → stripped, content kept
 *  - [connector:Name] markers      → ### Name heading
 *  - Inline <hypothesis> tags      → readable text
 *  - <hypothesis_tracking>         → removed
 */
export function stripSynthesisXml(text: string): string {
  let out = text;

  // Remove <follow_ups> blocks entirely (data arrives via follow_up_suggestions SSE)
  out = out.replace(/<follow_ups>[\s\S]*?<\/follow_ups>/g, '');

  // Convert <hypotheses> block to markdown
  out = out.replace(/<hypotheses>([\s\S]*?)<\/hypotheses>/g, (_match, inner: string) => {
    const items: string[] = [];
    const re = /<hypothesis\s+(?:id="[^"]*"\s+)?status="([^"]+)">([\s\S]*?)<\/hypothesis>/g;
    let m;
    while ((m = re.exec(inner)) !== null) {
      const icon = STATUS_ICON[m[1]] || '-';
      const label = m[1].charAt(0).toUpperCase() + m[1].slice(1);
      items.push(`- **${icon} ${label}:** ${m[2].trim()}`);
    }
    return items.length ? `\n### Key Findings\n${items.join('\n')}\n` : '';
  });

  // Strip standalone inline <hypothesis> tags (specialist reasoning leakage)
  out = out.replace(
    /<hypothesis\s+(?:id="[^"]*"\s+)?status="([^"]+)">([\s\S]*?)<\/hypothesis>/g,
    (_match, status: string, body: string) => {
      const icon = STATUS_ICON[status] || '-';
      const label = status.charAt(0).toUpperCase() + status.slice(1);
      return `**${icon} ${label}:** ${body.trim()}`;
    },
  );

  // Remove <hypothesis_tracking> blocks
  out = out.replace(/<hypothesis_tracking>[\s\S]*?<\/hypothesis_tracking>/g, '');

  // Strip <summary>/<reasoning> wrapper tags (keep content)
  out = out.replace(/<\/?(summary|reasoning)>/g, '');

  // Convert [connector:Name] markers to markdown headings
  out = out.replace(/\[connector:([^\]]+)\]/g, '\n### $1\n');

  // Collapse excessive blank lines left by removals
  out = out.replace(/\n{3,}/g, '\n\n');

  return out.trim();
}

/**
 * Replace [src:step-N] citation markers with numbered superscript markers.
 * Returns the processed text and a mapping of citation numbers to step IDs.
 */
export function processCitations(
  text: string,
): { processed: string; stepMap: Record<string, string> } {
  const stepMap: Record<string, string> = {};
  let citationNum = 0;
  const seenSteps = new Set<string>();

  const processed = text.replace(/\[src:(step-\d+)\]/g, (_match, stepId: string) => {
    if (!seenSteps.has(stepId)) {
      seenSteps.add(stepId);
      citationNum++;
      stepMap[String(citationNum)] = stepId;
    }
    // Find the citation number for this step
    const num = Object.entries(stepMap).find(([, sid]) => sid === stepId)?.[0] || String(citationNum);
    return `[^${num}]`;
  });

  return { processed, stepMap };
}
