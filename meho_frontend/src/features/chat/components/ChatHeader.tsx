// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Header Component
 *
 * Displays the chat header with assistant info, static online indicator, and visibility controls.
 * Phase 38: Added session visibility upgrade (Share button + confirmation dialog).
 * Phase 63: Added "Create Recipe" button to capture investigation as reusable recipe.
 * Phase 68.2-02: Removed connectionState prop -- static "Online" indicator only.
 */
import { useState, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Users, ChefHat } from 'lucide-react';
import clsx from 'clsx';
import mehoAvatar from '@/assets/meho-avatar.svg';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import { VisibilityConfirmDialog } from './VisibilityConfirmDialog';
import { AutomationBanner } from './AutomationBanner';

interface ChatHeaderProps {
  sessionId?: string | null;
  visibility?: string;
  onVisibilityChange?: (visibility: string) => void;
  triggerSource?: string | null;
}

export function ChatHeader({
  sessionId,
  visibility,
  onVisibilityChange,
  triggerSource,
}: ChatHeaderProps) {
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [pendingVisibility, setPendingVisibility] = useState<string | null>(null);
  const [recipeCreated, setRecipeCreated] = useState(false);
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // Reset "Created!" indicator after 2 seconds
  useEffect(() => {
    if (!recipeCreated) return;
    const timer = setTimeout(() => setRecipeCreated(false), 2000);
    return () => clearTimeout(timer);
  }, [recipeCreated]);

  const visibilityMutation = useMutation({
    mutationFn: () => apiClient.updateSessionVisibility(sessionId ?? '', pendingVisibility ?? 'private'),
    onSuccess: () => {
      if (pendingVisibility) onVisibilityChange?.(pendingVisibility);
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
      queryClient.invalidateQueries({ queryKey: ['team-sessions'] });
      setShowConfirmDialog(false);
      setPendingVisibility(null);
    },
  });

  // Phase 63: Create Recipe from session mutation
  const createRecipeMutation = useMutation({
    mutationFn: () => apiClient.createRecipeFromSession(sessionId ?? ''),
    onSuccess: () => {
      setRecipeCreated(true);
      queryClient.invalidateQueries({ queryKey: ['recipes'] });
      navigate('/recipes');
    },
  });

  const handleShareClick = () => {
    setPendingVisibility('group');
    setShowConfirmDialog(true);
  };

  const handleConfirm = () => {
    visibilityMutation.mutate();
  };

  const handleCancel = () => {
    setShowConfirmDialog(false);
    setPendingVisibility(null);
  };

  return (
    <>
      <header className="glass border-b border-white/5 px-6 py-4 z-10">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img
              src={mehoAvatar}
              alt="MEHO"
              className="w-10 h-10 rounded-full"
            />
            <div>
              <h2 className="text-lg font-semibold text-white tracking-tight">MEHO Assistant</h2>
              <p className="text-xs text-text-secondary flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
                Online &amp; Ready
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {/* Create Recipe button (Phase 63) */}
            {sessionId && (
              <button
                onClick={() => createRecipeMutation.mutate()}
                disabled={createRecipeMutation.isPending || recipeCreated}
                className={clsx(
                  'flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-full transition-all',
                  recipeCreated
                    ? 'text-green-400 bg-green-500/10 border border-green-500/20'
                    : 'text-text-secondary hover:text-primary bg-surface-active hover:bg-primary/20 border border-white/5 hover:border-primary/30',
                  createRecipeMutation.isPending && 'opacity-60 cursor-wait'
                )}
              >
                <ChefHat className="h-3.5 w-3.5" />
                {recipeCreated
                  ? 'Created!'
                  : createRecipeMutation.isPending
                    ? 'Creating...'
                    : 'Create Recipe'}
              </button>
            )}

            {/* Visibility control (Phase 38) */}
            {sessionId && (
              <>
                {(!visibility || visibility === 'private') ? (
                  <button
                    onClick={handleShareClick}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-text-secondary hover:text-white bg-surface/50 hover:bg-primary/20 border border-white/5 hover:border-primary/30 rounded-full transition-all"
                  >
                    <Users className="h-3.5 w-3.5" />
                    Share
                  </button>
                ) : (
                  <div className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-green-400 bg-green-500/10 border border-green-500/20 rounded-full">
                    <Users className="h-3.5 w-3.5" />
                    Shared
                  </div>
                )}
              </>
            )}

            {/* Static Online indicator (Phase 68.2-02: replaces tri-state connection indicator) */}
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-surface/50 border border-white/5">
              <div className="w-2 h-2 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.5)]" />
              <span className="text-xs text-text-secondary font-medium">Online</span>
            </div>
          </div>
        </div>
      </header>

      {/* Automation banner for automated sessions (Phase 75) */}
      {triggerSource && <AutomationBanner triggerSource={triggerSource} />}

      {/* Visibility confirmation dialog */}
      <VisibilityConfirmDialog
        isOpen={showConfirmDialog}
        targetVisibility={pendingVisibility || 'group'}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
        isLoading={visibilityMutation.isPending}
      />
    </>
  );
}
