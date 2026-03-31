// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * KnowledgeTree
 *
 * Three-tier hierarchical view of all knowledge:
 *   Global Knowledge
 *   > Connector Type (Kubernetes, VMware, ...)
 *     > Connector Instance (prod-k8s, staging-k8s, ...)
 *
 * Each node shows document/chunk counts and an upload action.
 * Max 3 levels deep -- no external tree library needed.
 */
import { useState } from 'react';
import { Loader2, AlertCircle, Plus, ChevronDown } from 'lucide-react';
import { useKnowledgeTree } from '../hooks/useKnowledgeTree';
import { GlobalSection } from './GlobalSection';
import { TypeSection } from './TypeSection';
import { KnowledgeUploadDialog } from './KnowledgeUploadDialog';
import { motion, AnimatePresence } from 'motion/react';

export function KnowledgeTree() {
  const { tree, isLoading, error } = useKnowledgeTree();
  const [addTypeOpen, setAddTypeOpen] = useState(false);
  const [selectedNewType, setSelectedNewType] = useState<string | null>(null);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 text-primary animate-spin" />
        <span className="ml-3 text-text-secondary">Loading knowledge tree...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
        <AlertCircle className="h-5 w-5 flex-shrink-0" />
        <span>Failed to load knowledge tree: {(error as Error).message}</span>
      </div>
    );
  }

  if (!tree) return null;

  // Determine which types are already in the tree
  const existingTypes = new Set(tree.types.map(t => t.connector_type));
  const availableNewTypes = tree.all_connector_types.filter(t => !existingTypes.has(t.value));

  return (
    <div className="space-y-3">
      {/* Global Section -- always first */}
      <GlobalSection
        documentCount={tree.global.document_count}
        chunkCount={tree.global.chunk_count}
      />

      {/* Connector Type Sections */}
      {tree.types.map((typeNode) => (
        <TypeSection
          key={typeNode.connector_type}
          connectorType={typeNode.connector_type}
          displayName={typeNode.display_name}
          documentCount={typeNode.document_count}
          chunkCount={typeNode.chunk_count}
          instances={typeNode.instances}
        />
      ))}

      {/* "Add knowledge for another type" button */}
      {availableNewTypes.length > 0 && (
        <div className="rounded-xl border border-dashed border-white/10 overflow-hidden">
          <button
            onClick={() => setAddTypeOpen(!addTypeOpen)}
            className="w-full flex items-center justify-center gap-2 px-4 py-3 text-sm text-text-secondary hover:text-white hover:bg-white/5 transition-colors"
          >
            <Plus className="h-4 w-4" />
            Add knowledge for another connector type
            {addTypeOpen ? <ChevronDown className="h-3 w-3" /> : null}
          </button>

          <AnimatePresence>
            {addTypeOpen && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden border-t border-white/5"
              >
                <div className="p-4 space-y-3">
                  {/* Type selector */}
                  <div className="space-y-2">
                    <span className="text-xs font-medium text-text-secondary">Select connector type</span>
                    <div className="flex flex-wrap gap-2">
                      {availableNewTypes.map((ct) => (
                        <button
                          key={ct.value}
                          onClick={() => setSelectedNewType(selectedNewType === ct.value ? null : ct.value)}
                          className={
                            selectedNewType === ct.value
                              ? 'px-3 py-1.5 rounded-lg text-xs font-medium bg-primary/20 text-primary border border-primary/30'
                              : 'px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 text-text-secondary border border-white/10 hover:bg-white/10 hover:text-white transition-colors'
                          }
                        >
                          {ct.display_name}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Upload form for selected type */}
                  {selectedNewType && (
                    <KnowledgeUploadDialog
                      scope={{ scope_type: 'type', connector_type_scope: selectedNewType }}
                      onSuccess={() => { setSelectedNewType(null); setAddTypeOpen(false); }}
                      inline
                    />
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}
