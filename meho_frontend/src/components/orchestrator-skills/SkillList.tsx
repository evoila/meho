// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SkillList Component
 *
 * Displays a scrollable list of orchestrator skills with active/inactive
 * badge, description preview, toggle switch, and delete button.
 *
 * Phase 52 - Orchestrator Skills Frontend
 */

import { Trash2 } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { OrchestratorSkillSummary } from '../../api/orchestratorSkills';

interface SkillListProps {
  skills: OrchestratorSkillSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onToggleActive: (id: string, currentActive: boolean) => void;
}

export function SkillList({
  skills,
  selectedId,
  onSelect,
  onDelete,
  onToggleActive,
}: Readonly<SkillListProps>) {
  return (
    <div className="space-y-1">
      <AnimatePresence mode="popLayout">
        {skills.map((skill) => (
          <motion.div
            key={skill.id}
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, x: -20 }}
            transition={{ duration: 0.2 }}
            onClick={() => onSelect(skill.id)}
            className={clsx(
              'group relative flex items-start gap-3 px-4 py-3 rounded-xl cursor-pointer transition-all duration-150',
              selectedId === skill.id
                ? 'bg-primary/10 border border-primary/30'
                : 'hover:bg-zinc-700/30 border border-transparent'
            )}
          >
            {/* Active indicator dot */}
            <div className="mt-1.5 shrink-0">
              <span
                className={clsx(
                  'block w-2.5 h-2.5 rounded-full',
                  skill.is_active ? 'bg-green-400' : 'bg-zinc-500'
                )}
              />
            </div>

            {/* Content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-white truncate">
                  {skill.name}
                </span>
                <span
                  className={clsx(
                    'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0',
                    skill.is_active
                      ? 'bg-green-400/10 text-green-400'
                      : 'bg-zinc-500/10 text-zinc-500'
                  )}
                >
                  {skill.is_active ? 'Active' : 'Inactive'}
                </span>
              </div>
              {skill.description && (
                <p className="text-xs text-zinc-400 mt-0.5 truncate">
                  {skill.description}
                </p>
              )}
            </div>

            {/* Actions */}
            <div className="flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
              {/* Toggle active */}
              <button
                type="button"
                role="switch"
                aria-checked={skill.is_active}
                onClick={(e) => {
                  e.stopPropagation();
                  onToggleActive(skill.id, skill.is_active);
                }}
                className={clsx(
                  'relative inline-flex h-4 w-7 shrink-0 cursor-pointer rounded-full border-2 border-transparent',
                  'transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary/50',
                  skill.is_active ? 'bg-green-500' : 'bg-zinc-600'
                )}
              >
                <span
                  className={clsx(
                    'pointer-events-none inline-block h-3 w-3 transform rounded-full bg-white shadow ring-0',
                    'transition duration-200 ease-in-out',
                    skill.is_active ? 'translate-x-3' : 'translate-x-0'
                  )}
                />
              </button>

              {/* Delete */}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirm(`Delete skill "${skill.name}"?`)) {
                    onDelete(skill.id);
                  }
                }}
                className="p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-700/50 transition-colors"
                title="Delete skill"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
