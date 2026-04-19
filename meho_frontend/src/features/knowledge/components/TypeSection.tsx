// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TypeSection
 *
 * Expandable section for a connector type in the knowledge tree.
 * Shows type-level doc count, upload button for type-level docs,
 * and list of connector instances as children.
 */
import { useState } from 'react';
import { ChevronRight, ChevronDown, Plus, FileText, Globe, Cpu } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { KnowledgeUploadDialog } from './KnowledgeUploadDialog';
import { InstanceNode } from './InstanceNode';
import type { KnowledgeTreeInstanceNode } from '../../../api/types/knowledge';

interface TypeSectionProps {
  connectorType: string;
  displayName: string;
  documentCount: number;
  chunkCount: number;
  instances: KnowledgeTreeInstanceNode[];
}

/** Get icon and colors for connector type */
function getTypeStyle(ct: string) {
  switch (ct) {
    case 'kubernetes':
      return { color: 'blue', label: 'K8s' };
    case 'vmware':
      return { color: 'emerald', label: 'VMw' };
    case 'proxmox':
      return { color: 'orange', label: 'PVE' };
    case 'graphql':
      return { color: 'pink', label: 'GQL' };
    case 'grpc':
      return { color: 'indigo', label: 'gRPC' };
    case 'soap':
      return { color: 'amber', label: 'SOAP' };
    case 'email':
      return { color: 'green', label: 'Email' };
    default:
      return { color: 'sky', label: 'REST' };
  }
}

const colorClasses: Record<string, { bg: string; border: string; text: string }> = {
  blue: { bg: 'bg-blue-500/10', border: 'border-blue-500/20', text: 'text-blue-400' },
  emerald: { bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', text: 'text-emerald-400' },
  orange: { bg: 'bg-orange-500/10', border: 'border-orange-500/20', text: 'text-orange-400' },
  pink: { bg: 'bg-pink-500/10', border: 'border-pink-500/20', text: 'text-pink-400' },
  indigo: { bg: 'bg-indigo-500/10', border: 'border-indigo-500/20', text: 'text-indigo-400' },
  amber: { bg: 'bg-amber-500/10', border: 'border-amber-500/20', text: 'text-amber-400' },
  green: { bg: 'bg-green-500/10', border: 'border-green-500/20', text: 'text-green-400' },
  sky: { bg: 'bg-sky-500/10', border: 'border-sky-500/20', text: 'text-sky-400' },
};

export function TypeSection({
  connectorType,
  displayName,
  documentCount,
  chunkCount: _chunkCount,
  instances,
}: Readonly<TypeSectionProps>) {
  const [expanded, setExpanded] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const style = getTypeStyle(connectorType);
  const colors = colorClasses[style.color] || colorClasses.sky;

  const totalInstanceDocs = instances.reduce((sum, inst) => sum + inst.document_count, 0);

  return (
    <div className="rounded-xl border border-white/10 overflow-hidden">
      {/* Header */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/5 transition-colors cursor-pointer"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-text-tertiary flex-shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 text-text-tertiary flex-shrink-0" />
        )}
        <div className={clsx('w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 border', colors.bg, colors.border)}>
          <Cpu className={clsx('h-4 w-4', colors.text)} />
        </div>
        <div className="flex-1 text-left">
          <span className="text-white font-medium text-sm">{displayName}</span>
          <span className="text-xs text-text-tertiary ml-2">
            {instances.length} instance{instances.length !== 1 ? 's' : ''}
          </span>
        </div>

        {/* Type-level doc count badge */}
        {documentCount > 0 && (
          <span className={clsx('flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border', colors.bg, colors.border, colors.text)}>
            <Globe className="h-3 w-3" />
            {documentCount} type
          </span>
        )}

        {/* Total instance docs badge */}
        {totalInstanceDocs > 0 && (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-md bg-white/5 border border-white/10 text-text-secondary text-xs">
            <FileText className="h-3 w-3" />
            {totalInstanceDocs} instance
          </span>
        )}

        {/* Upload type-level docs button */}
        <button
          onClick={(e) => { e.stopPropagation(); setShowUpload(!showUpload); setExpanded(true); }}
          className={clsx(
            'p-1.5 rounded-lg transition-colors',
            showUpload
              ? 'bg-primary/20 text-primary'
              : 'hover:bg-white/10 text-text-tertiary hover:text-white'
          )}
          title={`Upload type-level docs for ${displayName}`}
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>

      {/* Expanded content */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            {/* Type-level upload form */}
            {showUpload && (
              <div className="border-t border-white/5 p-4">
                <KnowledgeUploadDialog
                  scope={{ scope_type: 'type', connector_type_scope: connectorType }}
                  onSuccess={() => setShowUpload(false)}
                  inline
                />
              </div>
            )}

            {/* Instance list */}
            {instances.length > 0 && (
              <div className="px-4 pb-3 pl-14 space-y-1.5">
                {instances.map((inst) => (
                  <InstanceNode
                    key={inst.connector_id}
                    connectorId={inst.connector_id}
                    connectorName={inst.connector_name}
                    documentCount={inst.document_count}
                    chunkCount={inst.chunk_count}
                  />
                ))}
              </div>
            )}

            {instances.length === 0 && !showUpload && (
              <div className="px-4 pb-3 pl-14">
                <p className="text-xs text-text-tertiary">
                  No connector instances of this type. Type-level docs will apply when instances are added.
                </p>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
