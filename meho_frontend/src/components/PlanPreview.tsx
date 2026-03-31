// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Plan Preview Component
 * Shows generated workflow plan with approval buttons (Cursor-like UX)
 */
import { useState } from 'react';
import { CheckCircle, XCircle, AlertTriangle, ChevronRight, ChevronDown, Edit, Copy, RotateCw, Play } from 'lucide-react';
import type { Plan } from '../lib/api-client';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';

interface PlanPreviewProps {
  plan: Plan;
  workflowId: string;
  workflowStatus?: string;
  onApprove: () => void;
  onReject: () => void;
  onEdit?: (editedPlan: Plan) => void;
  onClone?: () => void;
  onRetry?: () => void;
  isApproving?: boolean;
}

const TOOL_ICONS: Record<string, string> = {
  search_knowledge: '🔍',
  list_connectors: '🔌',
  get_endpoint_details: '📋',
  call_endpoint: '🌐',
  interpret_results: '🤔',
};

const TOOL_COLORS: Record<string, string> = {
  search_knowledge: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
  list_connectors: 'bg-purple-500/10 border-purple-500/20 text-purple-400',
  get_endpoint_details: 'bg-yellow-500/10 border-yellow-500/20 text-yellow-400',
  call_endpoint: 'bg-green-500/10 border-green-500/20 text-green-400',
  interpret_results: 'bg-orange-500/10 border-orange-500/20 text-orange-400',
};

export function PlanPreview({
  plan,
  workflowId,
  workflowStatus = 'WAITING_APPROVAL',
  onApprove,
  onReject,
  onEdit,
  onClone,
  onRetry,
  isApproving,
}: PlanPreviewProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedSteps, setEditedSteps] = useState(plan.steps);
  const [isCollapsed, setIsCollapsed] = useState(false);
  const hasSteps = plan.steps && plan.steps.length > 0;

  const isWaitingApproval = workflowStatus === 'WAITING_APPROVAL';
  const isCompleted = workflowStatus === 'COMPLETED';
  const isFailed = workflowStatus === 'FAILED';

  const handleSaveEdit = () => {
    if (onEdit) {
      onEdit({
        ...plan,
        steps: editedSteps,
      });
    }
    setIsEditing(false);
  };

  const handleCancelEdit = () => {
    setEditedSteps(plan.steps);
    setIsEditing(false);
  };

  const displaySteps = isEditing ? editedSteps : plan.steps;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="my-6 glass rounded-xl overflow-hidden shadow-2xl border border-primary/20"
      data-testid="plan-preview"
    >
      {/* Header */}
      <div
        role="button"
        tabIndex={0}
        className="p-4 bg-gradient-to-r from-primary/10 to-transparent border-b border-white/5 cursor-pointer hover:bg-white/5 transition-colors"
        onClick={() => setIsCollapsed(!isCollapsed)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setIsCollapsed(!isCollapsed); } }}
      >
        <div className="flex items-start justify-between">
          <div className="flex items-start gap-3 flex-1">
            <div className="flex-shrink-0 mt-1">
              {isCollapsed ? (
                <ChevronRight className="h-5 w-5 text-text-secondary" />
              ) : (
                <ChevronDown className="h-5 w-5 text-text-secondary" />
              )}
            </div>
            <div className="flex-1">
              <h3 className="text-lg font-semibold text-white flex items-center gap-2">
                {isCompleted ? (
                  <CheckCircle className="h-5 w-5 text-green-400" />
                ) : isFailed ? (
                  <XCircle className="h-5 w-5 text-red-400" />
                ) : (
                  <AlertTriangle className="h-5 w-5 text-yellow-400" />
                )}
                {isCompleted ? 'Plan Completed' : isFailed ? 'Plan Failed' : 'Execution Plan Ready'}
                <span className="text-sm font-normal text-text-secondary ml-2 px-2 py-0.5 rounded-full bg-white/5 border border-white/10">
                  {displaySteps.length} step{displaySteps.length !== 1 ? 's' : ''}
                </span>
              </h3>
              <p className="text-sm text-text-secondary mt-1">
                {plan.goal}
              </p>
            </div>
          </div>

          {/* Edit button (only for waiting approval) */}
          {isWaitingApproval && onEdit && !isEditing && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setIsEditing(true);
              }}
              className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium bg-surface hover:bg-surface-hover text-text-primary border border-border rounded-lg transition-all hover:shadow-lg"
            >
              <Edit className="h-3 w-3" />
              Edit Plan
            </button>
          )}
        </div>
      </div>

      {/* Steps - collapsible */}
      <AnimatePresence>
        {!isCollapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            {hasSteps ? (
              <div className="p-6 space-y-3 bg-surface/30">
                <h4 className="text-xs font-bold text-text-tertiary uppercase tracking-wider mb-3 flex items-center gap-2">
                  Execution Steps
                  {isEditing && <span className="text-primary animate-pulse">(Editing Mode)</span>}
                </h4>
                {displaySteps.map((step, index) => (
                  <div
                    key={step.id}
                    className={clsx(
                      "border rounded-xl p-4 transition-all hover:shadow-md",
                      TOOL_COLORS[step.tool_name] || 'bg-surface border-border text-text-primary'
                    )}
                  >
                    <div className="flex items-start gap-4">
                      <span className="text-2xl filter drop-shadow-lg">{TOOL_ICONS[step.tool_name] || '⚙️'}</span>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="font-mono text-[10px] font-bold uppercase opacity-70 bg-black/20 px-1.5 py-0.5 rounded">
                            Step {index + 1}
                          </span>
                          <ChevronRight className="h-3 w-3 opacity-50" />
                          <span className="text-xs font-bold uppercase tracking-wide opacity-90">{step.tool_name}</span>
                        </div>
                        <p className="text-sm font-medium leading-relaxed opacity-90">{step.description}</p>

                        {/* Tool arguments */}
                        {Object.keys(step.tool_args).length > 0 && (
                          <details className="mt-3 group/details">
                            <summary className="text-xs font-medium opacity-60 cursor-pointer hover:opacity-100 flex items-center gap-1 select-none">
                              <ChevronRight className="h-3 w-3 transition-transform group-open/details:rotate-90" />
                              View arguments
                            </summary>
                            <pre className="mt-2 text-[10px] bg-black/30 p-3 rounded-lg border border-white/5 overflow-x-auto font-mono text-text-secondary">
                              {JSON.stringify(step.tool_args, null, 2)}
                            </pre>
                          </details>
                        )}

                        {/* Dependencies */}
                        {step.depends_on.length > 0 && (
                          <div className="flex items-center gap-2 mt-2 text-[10px] opacity-60">
                            <span className="font-semibold">Depends on:</span>
                            <div className="flex gap-1">
                              {step.depends_on.map(dep => (
                                <span key={dep} className="px-1.5 py-0.5 bg-black/20 rounded border border-white/5 font-mono">
                                  {dep}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="p-6 bg-yellow-500/10 border-b border-yellow-500/20">
                <p className="text-sm text-yellow-200 flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4" />
                  {plan.notes || 'No steps generated for this plan.'}
                </p>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Action buttons */}
      <div className="p-4 bg-surface/50 border-t border-white/5 flex items-center gap-3">
        {isEditing ? (
          <>
            {/* Edit mode buttons */}
            <button
              onClick={handleSaveEdit}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-primary text-white rounded-xl font-medium hover:bg-primary-hover transition-all shadow-lg shadow-primary/20 active:scale-[0.98]"
            >
              <CheckCircle className="h-5 w-5" />
              Save Changes
            </button>
            <button
              onClick={handleCancelEdit}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-surface hover:bg-surface-hover text-text-secondary rounded-xl font-medium border border-border transition-all active:scale-[0.98]"
            >
              <XCircle className="h-5 w-5" />
              Cancel
            </button>
          </>
        ) : isWaitingApproval ? (
          <>
            {/* Approval buttons */}
            <button
              onClick={onApprove}
              disabled={isApproving}
              data-testid="approve-button"
              className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-primary text-white rounded-xl font-medium hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-lg shadow-primary/20 hover:shadow-primary/30 active:scale-[0.98]"
            >
              {isApproving ? (
                'Approving...'
              ) : (
                <>
                  <Play className="h-5 w-5 fill-current" />
                  Approve & Execute
                </>
              )}
            </button>

            <button
              onClick={onReject}
              disabled={isApproving}
              data-testid="reject-button"
              className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-surface hover:bg-surface-hover text-text-secondary rounded-xl font-medium border border-border disabled:opacity-50 disabled:cursor-not-allowed transition-all active:scale-[0.98]"
            >
              <XCircle className="h-5 w-5" />
              Reject
            </button>
          </>
        ) : (isCompleted || isFailed) ? (
          <>
            {/* Repeatability buttons */}
            {onRetry && (
              <button
                onClick={onRetry}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-surface hover:bg-surface-hover text-primary rounded-xl font-medium border border-primary/30 hover:border-primary/50 transition-all active:scale-[0.98]"
              >
                <RotateCw className="h-5 w-5" />
                Run Again
              </button>
            )}
            {onClone && (
              <button
                onClick={onClone}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-surface hover:bg-surface-hover text-accent rounded-xl font-medium border border-accent/30 hover:border-accent/50 transition-all active:scale-[0.98]"
              >
                <Copy className="h-5 w-5" />
                Clone & Edit
              </button>
            )}
          </>
        ) : null}
      </div>

      {/* Plan ID for reference */}
      {import.meta.env.DEV && (
        <div className="px-6 pb-4 text-[10px] text-text-tertiary font-mono opacity-30 text-center">
          ID: {workflowId}
        </div>
      )}
    </motion.div>
  );
}

