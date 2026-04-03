// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SkillEditor Component (Phase 7 - Plan 02, updated Phase 89.1)
 *
 * Dual-field skill editor for connectors:
 * - Generated Skill section: read-only collapsible markdown (from pipeline)
 * - Instance-Specific Context section: editable custom_skill (operator-owned)
 * - Save: persists custom_skill via PUT /connectors/{id}/skill
 * - Cancel: discards changes with confirmation when dirty
 * - Regenerate: POST /connectors/{id}/skill/regenerate with confirmation when custom edits exist
 * - Unsaved changes guard via onDirtyChange callback
 *
 * D-01: Show generated_skill and custom_skill as separate fields.
 * Generated skill is read-only (only changeable via Regenerate).
 * Custom skill is the editable operator-provided instance context.
 */

import { useState, useCallback, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { motion, AnimatePresence } from 'motion/react';
import { Edit3, Save, X, RefreshCw, Star, ChevronDown } from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import { Modal } from '../../shared/components/ui';
import type { Connector } from '../../lib/api-client';

interface SkillEditorProps {
  connector: Connector;
  onDirtyChange?: (isDirty: boolean) => void;
}

/** Shared markdown component configuration for both sections. */
const markdownComponents: Components = {
  // Custom code block styling (from Message.tsx)
  code({ className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || '');
    const isInline = !match && !String(children).includes('\n');

    return isInline ? (
      <code className="bg-black/20 px-1.5 py-0.5 rounded text-accent font-mono text-[0.9em]" {...props}>
        {children}
      </code>
    ) : (
      <div className="relative group/code my-4 rounded-lg overflow-hidden border border-white/10 bg-[#0d0d0d]">
        <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/5">
          <span className="text-xs font-mono text-text-tertiary lowercase">
            {match ? match[1] : 'code'}
          </span>
        </div>
        <div className="p-4 overflow-x-auto">
          <code className={clsx("font-mono text-sm", className)} {...props}>
            {children}
          </code>
        </div>
      </div>
    );
  },
  // Custom link styling
  a: ({ node: _node, ...props }) => (
    // eslint-disable-next-line jsx-a11y/anchor-has-content
    <a
      className="text-accent hover:text-accent-hover underline decoration-accent/30 hover:decoration-accent transition-colors"
      target="_blank"
      rel="noopener noreferrer"
      {...props}
    />
  ),
  // Custom list styling
  ul: ({ node: _node, ...props }) => <ul className="list-disc pl-4 my-2 space-y-1 marker:text-text-tertiary" {...props} />,
  ol: ({ node: _node, ...props }) => <ol className="list-decimal pl-4 my-2 space-y-1 marker:text-text-tertiary" {...props} />,
  // Custom heading styling
  // eslint-disable-next-line jsx-a11y/heading-has-content
  h1: ({ node: _node, ...props }) => <h1 className="text-xl font-bold mb-3 mt-4 text-white" {...props} />,
  // eslint-disable-next-line jsx-a11y/heading-has-content
  h2: ({ node: _node, ...props }) => <h2 className="text-lg font-bold mb-2 mt-3 text-white" {...props} />,
  // eslint-disable-next-line jsx-a11y/heading-has-content
  h3: ({ node: _node, ...props }) => <h3 className="text-base font-semibold mb-2 mt-3 text-white" {...props} />,
  // Custom table styling
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto my-4 rounded-lg border border-white/10">
      <table className="min-w-full divide-y divide-white/10" {...props} />
    </div>
  ),
  thead: ({ node: _node, ...props }) => <thead className="bg-white/5" {...props} />,
  th: ({ node: _node, ...props }) => <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider" {...props} />,
  td: ({ node: _node, ...props }) => <td className="px-4 py-3 whitespace-nowrap text-sm text-text-tertiary border-t border-white/5" {...props} />,
  blockquote: ({ node: _node, ...props }) => <blockquote className="border-l-4 border-primary/30 pl-4 italic text-text-secondary my-4" {...props} />,
};

export function SkillEditor({ connector, onDirtyChange }: Readonly<SkillEditorProps>) {
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // State
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState('');
  const [showRegenerateConfirm, setShowRegenerateConfirm] = useState(false);
  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false);
  // Generated skill section: collapsed by default if custom_skill exists
  const [generatedExpanded, setGeneratedExpanded] = useState(!connector.custom_skill);

  // Separate content variables (D-01: no merged displayedContent)
  const generatedContent = connector.generated_skill || '';
  const customContent = connector.custom_skill || '';
  const isDirty = editing && content !== customContent;

  // Notify parent of dirty state changes
  useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);

  // Save mutation
  const saveMutation = useMutation({
    mutationFn: () => apiClient.saveCustomSkill(connector.id, content),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector', connector.id] });
      queryClient.invalidateQueries({ queryKey: ['connectors'] });
      setEditing(false);
      toast.success('Skill saved');
    },
    onError: () => {
      toast.error('Failed to save skill');
    },
  });

  // Regenerate mutation
  const regenerateMutation = useMutation({
    mutationFn: () => apiClient.regenerateSkill(connector.id),
    onMutate: () => {
      const toastId = toast.loading('Regenerating skill...');
      return { toastId };
    },
    onSuccess: (_data, _variables, context) => {
      queryClient.invalidateQueries({ queryKey: ['connector', connector.id] });
      queryClient.invalidateQueries({ queryKey: ['connectors'] });
      toast.success('Skill regenerated successfully', { id: context?.toastId });
    },
    onError: (_error, _variables, context) => {
      toast.error('Failed to regenerate skill', { id: context?.toastId });
    },
  });

  // Handlers
  const handleEdit = useCallback(() => {
    setEditing(true);
    setContent(customContent);
  }, [customContent]);

  const handleSave = useCallback(() => {
    saveMutation.mutate();
  }, [saveMutation]);

  const handleCancel = useCallback(() => {
    if (isDirty) {
      setShowDiscardConfirm(true);
    } else {
      setEditing(false);
    }
  }, [isDirty]);

  const handleRegenerate = useCallback(() => {
    if (connector.custom_skill) {
      setShowRegenerateConfirm(true);
    } else {
      regenerateMutation.mutate();
    }
  }, [connector.custom_skill, regenerateMutation]);

  const handleConfirmRegenerate = useCallback(() => {
    regenerateMutation.mutate();
    setShowRegenerateConfirm(false);
  }, [regenerateMutation]);

  const handleConfirmDiscard = useCallback(() => {
    setEditing(false);
    setContent('');
    setShowDiscardConfirm(false);
  }, []);

  return (
    <div className="space-y-6">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {/* Quality score */}
          {connector.skill_quality_score != null && (
            <span className="inline-flex items-center gap-1 text-xs text-text-secondary">
              <Star className="h-3.5 w-3.5 text-amber-400" />
              {connector.skill_quality_score}/5
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Regenerate button -- regenerates the generated_skill (not custom) */}
          <button
            onClick={handleRegenerate}
            disabled={regenerateMutation.isPending}
            className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-white bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            title="Regenerate the generated skill from connector operations"
          >
            <RefreshCw className={clsx("h-3.5 w-3.5", regenerateMutation.isPending && "animate-spin")} />
            Regenerate
          </button>
        </div>
      </div>

      {/* ================================================================ */}
      {/* Generated Skill section (read-only, collapsible) */}
      {/* ================================================================ */}
      {generatedContent && (
        <div className="rounded-xl border border-white/10 bg-white/[0.02]">
          {/* Collapsible header */}
          <button
            onClick={() => setGeneratedExpanded(!generatedExpanded)}
            className="flex items-center justify-between w-full px-4 py-3 text-left hover:bg-white/5 rounded-t-xl transition-colors"
          >
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium bg-green-500/10 text-green-400 border border-green-500/20">
                Generated
              </span>
              <span className="text-sm text-text-secondary">Generated Skill</span>
            </div>
            <ChevronDown
              className={clsx(
                "h-4 w-4 text-text-tertiary transition-transform duration-200",
                !generatedExpanded && "-rotate-90"
              )}
            />
          </button>

          {/* Collapsible content */}
          <AnimatePresence initial={false}>
            {generatedExpanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="px-4 pb-4 border-t border-white/5">
                  <div className="prose prose-sm max-w-none prose-invert mt-3">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                      {generatedContent}
                    </ReactMarkdown>
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* ================================================================ */}
      {/* Instance-Specific Context section (editable custom_skill) */}
      {/* ================================================================ */}
      <div className="rounded-xl border border-white/10">
        {/* Section header */}
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
              Custom
            </span>
            <span className="text-sm text-text-secondary">Instance-Specific Context</span>
          </div>

          <div className="flex items-center gap-2">
            {/* Unsaved changes indicator */}
            {editing && isDirty && (
              <span className="text-xs text-amber-400 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                Unsaved
              </span>
            )}

            {/* Edit button (preview mode only) */}
            {!editing && (
              <button
                onClick={handleEdit}
                className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-white bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg transition-all"
              >
                <Edit3 className="h-3.5 w-3.5" />
                Edit
              </button>
            )}
          </div>
        </div>

        {/* Content area */}
        <div className="px-4 pb-4 border-t border-white/5">
          {editing ? (
            <div className="space-y-4 mt-3">
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                className="w-full min-h-[300px] px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm resize-y"
                placeholder="Add instance-specific context for this connector (e.g., 'This cluster runs RabbitMQ 3.12')..."
              />

              <AnimatePresence>
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="flex gap-3"
                >
                  <button
                    onClick={handleSave}
                    disabled={saveMutation.isPending}
                    className="flex items-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {saveMutation.isPending ? (
                      <>
                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white" />
                        Saving...
                      </>
                    ) : (
                      <>
                        <Save className="h-4 w-4" />
                        Save
                      </>
                    )}
                  </button>
                  <button
                    onClick={handleCancel}
                    disabled={saveMutation.isPending}
                    className="flex items-center gap-2 px-6 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <X className="h-4 w-4" />
                    Cancel
                  </button>
                </motion.div>
              </AnimatePresence>
            </div>
          ) : (
            <div className="mt-3">
              {customContent ? (
                <div className="prose prose-sm max-w-none prose-invert">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                    {customContent}
                  </ReactMarkdown>
                </div>
              ) : (
                <p className="text-sm text-text-tertiary italic">
                  No instance-specific context. Click Edit to add operator knowledge about this connector.
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Regenerate confirmation modal */}
      <Modal
        isOpen={showRegenerateConfirm}
        onClose={() => setShowRegenerateConfirm(false)}
        title="Replace Custom Skill?"
        description="This will replace your custom edits with a freshly generated skill. Continue?"
        footer={
          <>
            <button
              onClick={() => setShowRegenerateConfirm(false)}
              className="px-4 py-2 text-sm text-text-secondary hover:text-white transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirmRegenerate}
              className="px-4 py-2 text-sm bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors"
            >
              Regenerate
            </button>
          </>
        }
      >
        <p className="text-sm text-text-secondary">
          Your custom skill will be overwritten with a newly generated skill based on the connector's current operations.
        </p>
      </Modal>

      {/* Discard changes confirmation modal */}
      <Modal
        isOpen={showDiscardConfirm}
        onClose={() => setShowDiscardConfirm(false)}
        title="Discard Changes?"
        description="You have unsaved changes that will be lost."
        footer={
          <>
            <button
              onClick={() => setShowDiscardConfirm(false)}
              className="px-4 py-2 text-sm text-text-secondary hover:text-white transition-colors"
            >
              Keep Editing
            </button>
            <button
              onClick={handleConfirmDiscard}
              className="px-4 py-2 text-sm bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors"
            >
              Discard
            </button>
          </>
        }
      >
        <p className="text-sm text-text-secondary">
          Any changes you have made to the skill content will be lost if you discard.
        </p>
      </Modal>
    </div>
  );
}
