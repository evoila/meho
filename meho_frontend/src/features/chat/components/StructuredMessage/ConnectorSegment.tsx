// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorSegment (Phase 62)
 *
 * Single connector section in the reasoning accordion.
 * Shows a connector badge header followed by full markdown content.
 * Citation markers are stripped from reasoning since they are
 * already shown in the summary section.
 */
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { common } from 'lowlight';
import { ConnectorIcon, CONNECTOR_COLORS } from '@/components/topology/ConnectorIcon';

interface ConnectorSegmentProps {
  connectorName: string;
  content: string;
}

// Detect connector type from name (best-effort heuristic)
function inferConnectorType(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes('kubernetes') || lower.includes('k8s')) return 'kubernetes';
  if (lower.includes('vmware') || lower.includes('vsphere') || lower.includes('vcenter')) return 'vmware';
  if (lower.includes('gcp') || lower.includes('google')) return 'gcp';
  if (lower.includes('proxmox')) return 'proxmox';
  if (lower.includes('prometheus')) return 'prometheus';
  if (lower.includes('loki')) return 'loki';
  if (lower.includes('tempo')) return 'tempo';
  if (lower.includes('alertmanager')) return 'alertmanager';
  if (lower.includes('jira')) return 'jira';
  if (lower.includes('confluence')) return 'confluence';
  if (lower.includes('email')) return 'email';
  return 'rest';
}

export function ConnectorSegment({ connectorName, content }: Readonly<ConnectorSegmentProps>) {
  const connectorType = inferConnectorType(connectorName);
  const badgeColor = CONNECTOR_COLORS[connectorType] || CONNECTOR_COLORS.rest;

  // Strip citation markers from reasoning content (already shown in summary)
  const cleanContent = content.replaceAll(/\[src:step-\d+\]/g, '');

  return (
    <div className="mb-4 last:mb-0" data-connector={connectorName}>
      {/* Connector badge */}
      <div
        className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md mb-2 text-xs font-medium border"
        style={{
          borderColor: `${badgeColor}40`,
          color: badgeColor,
          backgroundColor: `${badgeColor}10`,
        }}
      >
        <ConnectorIcon connectorType={connectorType} size={14} />
        {connectorName}
      </div>

      {/* Markdown content */}
      <div className="prose prose-sm prose-invert max-w-none text-text-secondary leading-relaxed">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[[rehypeHighlight, { detect: false, languages: common }]]}
          components={{
            a: ({ ...props }) => (
              // eslint-disable-next-line jsx-a11y/anchor-has-content -- content provided via react-markdown spread props
              <a
                className="text-accent hover:text-accent-hover underline decoration-accent/30 hover:decoration-accent transition-colors"
                target="_blank"
                rel="noopener noreferrer"
                {...props}
              />
            ),
            table: ({ ...props }) => (
              <div className="overflow-x-auto my-3 rounded-lg border border-white/10">
                <table className="min-w-full divide-y divide-white/10" {...props} />
              </div>
            ),
            thead: ({ ...props }) => <thead className="bg-white/5" {...props} />,
            th: ({ ...props }) => (
              <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary uppercase tracking-wider" {...props} />
            ),
            td: ({ ...props }) => (
              <td className="px-3 py-2 whitespace-nowrap text-sm text-text-tertiary border-t border-white/5" {...props} />
            ),
          }}
        >
          {cleanContent}
        </ReactMarkdown>
      </div>
    </div>
  );
}
