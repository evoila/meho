// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Recipes Page - Browse, edit, and execute saved automation recipes
 *
 * Session 80: Replaces WorkflowsPage as part of Unified Execution Architecture
 * Phase 63: Added recipe edit modal with name, description, tags, and parameter editing
 *
 * Recipes are captured from successful Q&A conversations and can be:
 * - Re-executed with different parameters
 * - Edited (name, description, tags, parameter defaults)
 * - Scheduled for automatic execution
 * - Shared with team members
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Search, Play, Trash2, List, Clock, LayoutGrid,
  ChefHat, Zap, Pencil, X
} from 'lucide-react';
import { getAPIClient } from '../lib/api-client';
import type { Recipe, RecipeParameter } from '../lib/api-client';
import { config } from '../lib/config';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';

// ---------------------------------------------------------------------------
// Edit Modal
// ---------------------------------------------------------------------------

interface EditRecipeModalProps {
  recipe: Recipe;
  onClose: () => void;
  onSave: (updates: {
    name: string;
    description: string;
    tags: string[];
    parameters: RecipeParameter[];
  }) => void;
  isSaving: boolean;
  saveError: string | null;
}

function EditRecipeModal({ recipe, onClose, onSave, isSaving, saveError }: EditRecipeModalProps) {
  const [name, setName] = useState(recipe.name);
  const [description, setDescription] = useState(recipe.description || '');
  const [tagsInput, setTagsInput] = useState((recipe.tags || []).join(', '));
  const [parameters, setParameters] = useState<RecipeParameter[]>(
    (recipe.parameters || []).map(p => ({ ...p }))
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const parsedTags = tagsInput
      .split(',')
      .map(t => t.trim())
      .filter(Boolean);
    onSave({ name, description, tags: parsedTags, parameters });
  };

  const updateParameter = (index: number, field: keyof RecipeParameter, value: string) => {
    setParameters(prev => {
      const updated = [...prev];
      updated[index] = { ...updated[index], [field]: value };
      return updated;
    });
  };

  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-surface border border-border rounded-2xl p-6 w-full max-w-lg max-h-[80vh] overflow-y-auto"
      >
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">Edit Recipe</h2>
          <button
            onClick={onClose}
            className="p-1.5 text-text-tertiary hover:text-white rounded-lg hover:bg-surface-active transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label htmlFor="recipe-name" className="block text-sm font-medium text-text-secondary mb-1">
              Name
            </label>
            <input
              id="recipe-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="w-full px-3 py-2 bg-background border border-border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:border-primary"
            />
          </div>

          {/* Description */}
          <div>
            <label htmlFor="recipe-description" className="block text-sm font-medium text-text-secondary mb-1">
              Description
            </label>
            <textarea
              id="recipe-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 bg-background border border-border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:border-primary resize-none"
            />
          </div>

          {/* Tags */}
          <div>
            <label htmlFor="recipe-tags" className="block text-sm font-medium text-text-secondary mb-1">
              Tags
              <span className="text-text-tertiary font-normal ml-1">(comma-separated)</span>
            </label>
            <input
              id="recipe-tags"
              type="text"
              value={tagsInput}
              onChange={(e) => setTagsInput(e.target.value)}
              placeholder="kubernetes, debugging, performance"
              className="w-full px-3 py-2 bg-background border border-border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:border-primary"
            />
          </div>

          {/* Parameters */}
          {parameters.length > 0 && (
            <div>
              <span className="block text-sm font-medium text-text-secondary mb-2">
                Parameters
              </span>
              <div className="space-y-3">
                {parameters.map((param, i) => (
                  <div
                    key={param.name}
                    className="p-3 bg-background border border-border rounded-lg space-y-2"
                  >
                    <div className="text-sm font-medium text-white">
                      {param.name}
                    </div>
                    <div>
                      <label htmlFor={`recipe-param-desc-${param.name}`} className="block text-xs text-text-tertiary mb-0.5">
                        Description
                      </label>
                      <input
                        id={`recipe-param-desc-${param.name}`}
                        type="text"
                        value={param.description || ''}
                        onChange={(e) => updateParameter(i, 'description', e.target.value)}
                        className="w-full px-2 py-1.5 text-sm bg-surface border border-border rounded-md text-white placeholder-text-tertiary focus:outline-none focus:border-primary"
                      />
                    </div>
                    <div>
                      <label htmlFor={`recipe-param-default-${param.name}`} className="block text-xs text-text-tertiary mb-0.5">
                        Default Value
                      </label>
                      <input
                        id={`recipe-param-default-${param.name}`}
                        type="text"
                        value={param.default != null ? String(param.default) : ''}
                        onChange={(e) => updateParameter(i, 'default', e.target.value)}
                        className="w-full px-2 py-1.5 text-sm bg-surface border border-border rounded-md text-white placeholder-text-tertiary focus:outline-none focus:border-primary"
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Error */}
          {saveError && (
            <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-400">
              {saveError}
            </div>
          )}

          {/* Actions */}
          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-text-secondary hover:text-white rounded-lg hover:bg-surface-active transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving || !name.trim()}
              className="px-4 py-2 text-sm font-medium text-white bg-primary hover:bg-primary/80 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isSaving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </motion.div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recipes Page
// ---------------------------------------------------------------------------

export function RecipesPage() {
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedTag, setSelectedTag] = useState<string>('');
  const [editingRecipe, setEditingRecipe] = useState<Recipe | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Fetch recipes
  const { data: recipes = [], isLoading } = useQuery({
    queryKey: ['recipes', searchQuery, selectedTag],
    queryFn: () => apiClient.listRecipes({
      search: searchQuery || undefined,
      tag: selectedTag || undefined,
    }),
  });

  // Execute recipe mutation
  const executeRecipeMutation = useMutation({
    mutationFn: ({ id, params }: { id: string; params: Record<string, unknown> }) =>
      apiClient.executeRecipe(id, params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['recipes'] });
    },
    onError: (error) => {
      alert(`Failed to execute recipe: ${error instanceof Error ? error.message : 'Unknown error'}`);
    },
  });

  // Delete recipe mutation
  const deleteRecipeMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteRecipe(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['recipes'] });
    },
    onError: (error) => {
      alert(`Failed to delete recipe: ${error instanceof Error ? error.message : 'Unknown error'}`);
    },
  });

  // Update recipe mutation (Phase 63)
  const updateRecipeMutation = useMutation({
    mutationFn: ({ id, updates }: { id: string; updates: { name: string; description: string; tags: string[]; parameters: RecipeParameter[] } }) =>
      apiClient.updateRecipe(id, updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['recipes'] });
      setEditingRecipe(null);
      setEditError(null);
    },
    onError: (error) => {
      setEditError(error instanceof Error ? error.message : 'Failed to save recipe');
    },
  });

  // Get unique tags
  const allTags = Array.from(new Set(recipes.flatMap(r => r.tags || []))).filter(Boolean);

  const handleExecute = (recipe: Recipe) => {
    if (confirm(`Execute recipe "${recipe.name}"?`)) {
      executeRecipeMutation.mutate({ id: recipe.id, params: {} });
    }
  };

  const handleDelete = (e: React.MouseEvent, recipe: Recipe) => {
    e.stopPropagation();
    if (confirm(`Delete recipe "${recipe.name}"? This cannot be undone.`)) {
      deleteRecipeMutation.mutate(recipe.id);
    }
  };

  const handleEdit = (e: React.MouseEvent, recipe: Recipe) => {
    e.stopPropagation();
    setEditError(null);
    setEditingRecipe(recipe);
  };

  const handleSaveEdit = (updates: { name: string; description: string; tags: string[]; parameters: RecipeParameter[] }) => {
    if (!editingRecipe) return;
    updateRecipeMutation.mutate({ id: editingRecipe.id, updates });
  };

  const filteredRecipes = recipes.filter(r => {
    if (selectedTag && !r.tags?.includes(selectedTag)) return false;
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      return (
        r.name.toLowerCase().includes(query) ||
        r.description?.toLowerCase().includes(query) ||
        r.tags?.some(tag => tag.toLowerCase().includes(query))
      );
    }
    return true;
  });

  return (
    <div className="h-full flex flex-col bg-background">
      {/* Header */}
      <div className="flex-none p-6 border-b border-border">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-3">
              <ChefHat className="w-7 h-7 text-primary" />
              Saved Recipes
            </h1>
            <p className="text-text-secondary mt-1">
              Automation patterns captured from successful conversations
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center bg-surface rounded-lg p-1 border border-border">
              <button
                onClick={() => setViewMode('grid')}
                className={clsx(
                  'p-2 rounded-md transition-colors',
                  viewMode === 'grid' ? 'bg-surface-active text-white' : 'text-text-secondary hover:text-white'
                )}
              >
                <LayoutGrid className="w-4 h-4" />
              </button>
              <button
                onClick={() => setViewMode('list')}
                className={clsx(
                  'p-2 rounded-md transition-colors',
                  viewMode === 'list' ? 'bg-surface-active text-white' : 'text-text-secondary hover:text-white'
                )}
              >
                <List className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>

        {/* Search and Filters */}
        <div className="flex items-center gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
            <input
              type="text"
              placeholder="Search recipes..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-surface border border-border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:border-primary"
            />
          </div>
          {allTags.length > 0 && (
            <select
              value={selectedTag}
              onChange={(e) => setSelectedTag(e.target.value)}
              className="px-4 py-2 bg-surface border border-border rounded-lg text-white focus:outline-none focus:border-primary"
            >
              <option value="">All Tags</option>
              {allTags.map(tag => (
                <option key={tag} value={tag}>{tag}</option>
              ))}
            </select>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {isLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
          </div>
        ) : filteredRecipes.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-center">
            <ChefHat className="w-16 h-16 text-text-tertiary mb-4" />
            <h2 className="text-xl font-semibold text-white mb-2">No Recipes Yet</h2>
            <p className="text-text-secondary max-w-md">
              Recipes are reusable investigation workflows. Complete an investigation and click "Create Recipe" to save it as an automation pattern you can re-run with different parameters.
            </p>
          </div>
        ) : (
          <AnimatePresence mode="popLayout">
            <div className={clsx(
              'gap-4',
              viewMode === 'grid'
                ? 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3'
                : 'flex flex-col'
            )}>
              {filteredRecipes.map((recipe) => (
                <motion.div
                  key={recipe.id}
                  layout
                  initial={{ opacity: 0, scale: 0.95 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  className={clsx(
                    'group bg-surface border border-border rounded-xl p-5 hover:border-primary/50 transition-all cursor-pointer',
                    viewMode === 'list' && 'flex items-center justify-between'
                  )}
                  onClick={() => handleExecute(recipe)}
                >
                  <div className={clsx(viewMode === 'list' && 'flex items-center gap-4 flex-1')}>
                    <div className="flex items-start justify-between mb-3">
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-lg bg-primary/20 flex items-center justify-center">
                          <Zap className="w-5 h-5 text-primary" />
                        </div>
                        <div>
                          <h3 className="font-semibold text-white group-hover:text-primary transition-colors">
                            {recipe.name}
                          </h3>
                          {recipe.execution_count > 0 && (
                            <span className="text-xs text-text-tertiary">
                              {recipe.execution_count} execution{recipe.execution_count !== 1 ? 's' : ''}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    {recipe.description && (
                      <p className="text-sm text-text-secondary mb-3 line-clamp-2">
                        {recipe.description}
                      </p>
                    )}

                    {recipe.tags && recipe.tags.length > 0 && (
                      <div className="flex flex-wrap gap-2 mb-3">
                        {recipe.tags.slice(0, 3).map(tag => (
                          <span
                            key={tag}
                            className="px-2 py-1 text-xs bg-surface-active text-text-secondary rounded-md"
                          >
                            {tag}
                          </span>
                        ))}
                        {recipe.tags.length > 3 && (
                          <span className="px-2 py-1 text-xs text-text-tertiary">
                            +{recipe.tags.length - 3} more
                          </span>
                        )}
                      </div>
                    )}

                    <div className="flex items-center justify-between text-xs text-text-tertiary">
                      <div className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {new Date(recipe.created_at).toLocaleDateString()}
                      </div>
                    </div>
                  </div>

                  <div className={clsx(
                    'flex items-center gap-2',
                    viewMode === 'grid' ? 'mt-4 pt-4 border-t border-border' : ''
                  )}>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleExecute(recipe);
                      }}
                      disabled={executeRecipeMutation.isPending}
                      className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-primary/20 text-primary rounded-lg hover:bg-primary/30 transition-colors disabled:opacity-50"
                    >
                      <Play className="w-4 h-4" />
                      Run
                    </button>
                    <button
                      onClick={(e) => handleEdit(e, recipe)}
                      className="p-2 text-text-tertiary hover:text-primary hover:bg-primary/10 rounded-lg transition-colors"
                    >
                      <Pencil className="w-4 h-4" />
                    </button>
                    <button
                      onClick={(e) => handleDelete(e, recipe)}
                      disabled={deleteRecipeMutation.isPending}
                      className="p-2 text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-50"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </motion.div>
              ))}
            </div>
          </AnimatePresence>
        )}
      </div>

      {/* Edit Modal (Phase 63) */}
      <AnimatePresence>
        {editingRecipe && (
          <EditRecipeModal
            recipe={editingRecipe}
            onClose={() => { setEditingRecipe(null); setEditError(null); }}
            onSave={handleSaveEdit}
            isSaving={updateRecipeMutation.isPending}
            saveError={editError}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
