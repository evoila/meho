// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SkillGenerateModal Component
 *
 * Modal for LLM-assisted orchestrator skill generation.
 * User describes what the skill should do, and the backend generates
 * skill content using LLM with context from configured connectors.
 *
 * Phase 52 - Orchestrator Skills Frontend
 */

import { useState } from 'react';
import { Wand2, X, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { getAPIClient } from '../../lib/api-client';

interface SkillGenerateModalProps {
  isOpen: boolean;
  onClose: () => void;
  onGenerated: (content: string) => void;
}

export function SkillGenerateModal({
  isOpen,
  onClose,
  onGenerated,
}: SkillGenerateModalProps) {
  const [userDescription, setUserDescription] = useState('');
  const [generating, setGenerating] = useState(false);

  if (!isOpen) return null;

  const handleGenerate = async () => {
    if (!userDescription.trim()) return;

    setGenerating(true);
    try {
      const api = getAPIClient();
      const result = await api.generateOrchestratorSkill({
        user_description: userDescription.trim(),
      });
      onGenerated(result.content);
      setUserDescription('');
      onClose();
      toast.success('Skill content generated -- review and edit before saving');
    } catch (err: unknown) {
      const errObj = err as { response?: { data?: { detail?: string } }; message?: string } | null;
      const message =
        errObj?.response?.data?.detail ?? errObj?.message ?? 'Failed to generate skill content';
      toast.error(message);
    } finally {
      setGenerating(false);
    }
  };

  const handleClose = () => {
    if (generating) return; // prevent closing while generating
    setUserDescription('');
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- modal backdrop, keyboard close handled by Escape */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={handleClose}
      />

      {/* Modal */}
      <div className="relative w-full max-w-lg bg-zinc-800/90 backdrop-blur-xl border border-zinc-700/50 rounded-2xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-700/50">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
              <Wand2 className="h-4 w-4 text-primary" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-white">Generate with AI</h2>
              <p className="text-xs text-zinc-400">
                Describe what this skill should do
              </p>
            </div>
          </div>
          <button
            onClick={handleClose}
            disabled={generating}
            className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-700/50 transition-colors disabled:opacity-50"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          <p className="text-sm text-zinc-300">
            Describe the investigation pattern, cross-system diagnosis, or operational
            procedure you want. The AI will generate a complete skill grounded in your
            configured connectors.
          </p>

          <textarea
            value={userDescription}
            onChange={(e) => setUserDescription(e.target.value)}
            placeholder="e.g., When a deployment fails in ArgoCD, check the application sync status, then look at the corresponding GitHub Actions workflow runs to find build errors, and cross-reference with Kubernetes pod logs for runtime failures."
            className="w-full min-h-[140px] px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm resize-y leading-relaxed"
            rows={6}
            disabled={generating}
          />

          {generating && (
            <div className="flex items-center gap-3 px-4 py-3 bg-primary/5 border border-primary/20 rounded-xl">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
              <span className="text-sm text-primary">
                Generating skill content... this may take a few seconds
              </span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-zinc-700/50">
          <button
            onClick={handleClose}
            disabled={generating}
            className="px-4 py-2 text-sm text-zinc-400 hover:text-white transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={handleGenerate}
            disabled={!userDescription.trim() || generating}
            className="flex items-center gap-2 px-5 py-2 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium text-sm transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {generating ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Generating...
              </>
            ) : (
              <>
                <Wand2 className="h-4 w-4" />
                Generate
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
