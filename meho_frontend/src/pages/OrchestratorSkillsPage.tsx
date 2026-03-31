// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator Skills Page
 *
 * Two-panel layout for managing orchestrator skills:
 * - Left panel (1/3): skill list with active/inactive toggle and delete
 * - Right panel (2/3): skill editor with markdown preview
 *
 * Supports full CRUD, active/inactive toggle, and LLM-assisted generation.
 *
 * Phase 52 - Orchestrator Skills Frontend
 */

import { useState, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Wand2, Brain, Loader2 } from 'lucide-react';
import { motion } from 'motion/react';
import { toast } from 'sonner';
import { getAPIClient } from '../lib/api-client';
import type {
  OrchestratorSkillSummary,
  OrchestratorSkill,
} from '../api/orchestratorSkills';
import { SkillList } from '../components/orchestrator-skills/SkillList';
import { SkillEditor } from '../components/orchestrator-skills/SkillEditor';
import { SkillGenerateModal } from '../components/orchestrator-skills/SkillGenerateModal';

export function OrchestratorSkillsPage() {
  const api = getAPIClient();
  const queryClient = useQueryClient();

  // UI state
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [generateModalOpen, setGenerateModalOpen] = useState(false);
  const [generatedContent, setGeneratedContent] = useState<string | null>(null);

  // ---- Queries ----
  const {
    data: skills = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ['orchestrator-skills'],
    queryFn: () => api.listOrchestratorSkills(),
  });

  const {
    data: selectedSkill,
    isLoading: isLoadingSkill,
  } = useQuery({
    queryKey: ['orchestrator-skill', selectedId],
    queryFn: () => api.getOrchestratorSkill(selectedId ?? ''),
    enabled: !!selectedId && !isCreating,
  });

  // ---- Mutations ----
  const createMutation = useMutation({
    mutationFn: (data: { name: string; description: string; content: string }) =>
      api.createOrchestratorSkill({
        name: data.name,
        description: data.description || undefined,
        content: data.content,
      }),
    onSuccess: (created) => {
      toast.success('Skill created');
      queryClient.invalidateQueries({ queryKey: ['orchestrator-skills'] });
      setIsCreating(false);
      setGeneratedContent(null);
      setSelectedId(created.id);
    },
    onError: (err: unknown) => {
      const errObj = err as { response?: { data?: { detail?: string } } } | null;
      const detail = errObj?.response?.data?.detail ?? 'Failed to create skill';
      toast.error(detail);
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { name: string; description: string; content: string } }) =>
      api.updateOrchestratorSkill(id, {
        name: data.name,
        description: data.description || undefined,
        content: data.content,
      }),
    onSuccess: (updated) => {
      toast.success('Skill updated');
      queryClient.invalidateQueries({ queryKey: ['orchestrator-skills'] });
      queryClient.invalidateQueries({ queryKey: ['orchestrator-skill', updated.id] });
    },
    onError: (err: unknown) => {
      const errObj = err as { response?: { data?: { detail?: string } } } | null;
      const detail = errObj?.response?.data?.detail ?? 'Failed to update skill';
      toast.error(detail);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteOrchestratorSkill(id),
    onSuccess: (_data, deletedId) => {
      toast.success('Skill deleted');
      queryClient.invalidateQueries({ queryKey: ['orchestrator-skills'] });
      if (selectedId === deletedId) {
        setSelectedId(null);
      }
    },
    onError: () => {
      toast.error('Failed to delete skill');
    },
  });

  const toggleActiveMutation = useMutation({
    mutationFn: ({ id, currentActive }: { id: string; currentActive: boolean }) =>
      api.updateOrchestratorSkill(id, { is_active: !currentActive }),
    onMutate: async ({ id, currentActive }) => {
      await queryClient.cancelQueries({ queryKey: ['orchestrator-skills'] });
      const prev = queryClient.getQueryData<OrchestratorSkillSummary[]>(['orchestrator-skills']);
      queryClient.setQueryData<OrchestratorSkillSummary[]>(
        ['orchestrator-skills'],
        (old) =>
          old?.map((s) =>
            s.id === id ? { ...s, is_active: !currentActive } : s
          ) ?? []
      );
      return { prev };
    },
    onError: (_err, _vars, context) => {
      if (context?.prev) {
        queryClient.setQueryData(['orchestrator-skills'], context.prev);
      }
      toast.error('Failed to toggle skill');
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['orchestrator-skills'] });
    },
  });

  // ---- Handlers ----
  const handleSelect = useCallback((id: string) => {
    setSelectedId(id);
    setIsCreating(false);
    setGeneratedContent(null);
  }, []);

  const handleNewSkill = useCallback(() => {
    setSelectedId(null);
    setIsCreating(true);
    setGeneratedContent(null);
  }, []);

  const handleCancel = useCallback(() => {
    setIsCreating(false);
    setGeneratedContent(null);
    // Keep selectedId as-is if editing an existing skill
    if (isCreating) {
      setSelectedId(null);
    }
  }, [isCreating]);

  const handleSave = useCallback(
    (data: { name: string; description: string; content: string }) => {
      if (isCreating) {
        createMutation.mutate(data);
      } else if (selectedId) {
        updateMutation.mutate({ id: selectedId, data });
      }
    },
    [isCreating, selectedId, createMutation, updateMutation]
  );

  const handleDelete = useCallback(
    (id: string) => {
      deleteMutation.mutate(id);
    },
    [deleteMutation]
  );

  const handleToggleActive = useCallback(
    (id: string, currentActive: boolean) => {
      toggleActiveMutation.mutate({ id, currentActive });
    },
    [toggleActiveMutation]
  );

  const handleGenerated = useCallback(
    (content: string) => {
      // Open editor in create mode with generated content pre-filled
      setSelectedId(null);
      setIsCreating(true);
      setGeneratedContent(content);
    },
    []
  );

  // Build a "virtual" skill for the editor when creating with generated content
  const editorSkill: OrchestratorSkill | null = isCreating && generatedContent
    ? {
        id: '',
        tenant_id: '',
        name: '',
        description: '',
        content: generatedContent,
        summary: '',
        is_active: true,
        created_at: '',
        updated_at: '',
      }
    : isCreating
    ? null
    : selectedSkill ?? null;

  // ---- Loading / Error states ----
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-red-400">Failed to load orchestrator skills</p>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-6 shrink-0">
        <div>
          <h1 className="text-2xl font-bold text-white">Orchestrator Skills</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Cross-system investigation patterns for the orchestrator agent
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setGenerateModalOpen(true)}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-white font-medium text-sm transition-colors"
          >
            <Wand2 className="h-4 w-4" />
            Generate with AI
          </button>
          <button
            onClick={handleNewSkill}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white font-medium text-sm hover:bg-primary/90 transition-colors"
          >
            <Plus className="h-4 w-4" />
            New Skill
          </button>
        </div>
      </div>

      {/* Two-panel layout */}
      {skills.length === 0 && !isCreating ? (
        /* Empty state */
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex flex-col items-center justify-center py-20 text-center"
        >
          <div className="w-16 h-16 rounded-2xl bg-zinc-800/50 border border-zinc-700/50 flex items-center justify-center mb-4">
            <Brain className="h-8 w-8 text-zinc-500" />
          </div>
          <h3 className="text-lg font-medium text-white mb-2">
            No orchestrator skills yet
          </h3>
          <p className="text-sm text-zinc-400 max-w-sm mb-6">
            Create custom investigation patterns that guide the orchestrator agent.
            Skills define cross-system diagnosis workflows across your connectors.
          </p>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setGenerateModalOpen(true)}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-white font-medium text-sm transition-colors"
            >
              <Wand2 className="h-4 w-4" />
              Generate with AI
            </button>
            <button
              onClick={handleNewSkill}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white font-medium text-sm hover:bg-primary/90 transition-colors"
            >
              <Plus className="h-4 w-4" />
              Create your first skill
            </button>
          </div>
        </motion.div>
      ) : (
        /* Two-panel layout */
        <div className="flex-1 flex gap-6 min-h-0">
          {/* Left panel - skill list */}
          <div className="w-1/3 flex flex-col min-h-0">
            <div className="bg-zinc-800/50 border border-zinc-700/50 rounded-xl p-3 flex-1 overflow-y-auto">
              <SkillList
                skills={skills}
                selectedId={selectedId}
                onSelect={handleSelect}
                onDelete={handleDelete}
                onToggleActive={handleToggleActive}
              />
            </div>
          </div>

          {/* Right panel - editor */}
          <div className="w-2/3 flex flex-col min-h-0">
            <div className="bg-zinc-800/50 border border-zinc-700/50 rounded-xl p-6 flex-1 overflow-y-auto">
              {isCreating || selectedId ? (
                isLoadingSkill && selectedId && !isCreating ? (
                  <div className="flex items-center justify-center h-full">
                    <Loader2 className="h-6 w-6 animate-spin text-zinc-400" />
                  </div>
                ) : (
                  <SkillEditor
                    skill={editorSkill}
                    isNew={isCreating}
                    onSave={handleSave}
                    onCancel={handleCancel}
                    saving={createMutation.isPending || updateMutation.isPending}
                  />
                )
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-center">
                  <Brain className="h-10 w-10 text-zinc-600 mb-3" />
                  <p className="text-sm text-zinc-500">
                    Select a skill from the list or create a new one
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Generate modal */}
      <SkillGenerateModal
        isOpen={generateModalOpen}
        onClose={() => setGenerateModalOpen(false)}
        onGenerated={handleGenerated}
      />
    </div>
  );
}
