// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Mode Toggle Component
 *
 * Phase 65-05: Ask/Agent mode toggle for chat input area.
 * Users switch between knowledge Q&A (Ask) and infrastructure actions (Agent).
 */
import { BookOpen, Cpu } from 'lucide-react';
import clsx from 'clsx';

interface ModeToggleProps {
  mode: 'ask' | 'agent';
  onModeChange: (mode: 'ask' | 'agent') => void;
  disabled?: boolean;
}

export function ModeToggle({ mode, onModeChange, disabled = false }: ModeToggleProps) {
  return (
    <div
      role="radiogroup"
      aria-label="Chat mode"
      className="flex items-center gap-1 px-2 py-1"
    >
      <button
        type="button"
        role="radio"
        aria-checked={mode === 'ask'}
        aria-label="Switch to ask mode"
        title="Search knowledge base and get information"
        disabled={disabled}
        onClick={() => onModeChange('ask')}
        className={clsx(
          'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all duration-200',
          mode === 'ask'
            ? 'bg-primary/20 text-primary border border-primary/30'
            : 'text-text-tertiary hover:text-text-secondary border border-transparent',
          disabled && 'opacity-50 cursor-not-allowed',
        )}
      >
        <BookOpen className="h-3.5 w-3.5" />
        Ask
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={mode === 'agent'}
        aria-label="Switch to agent mode"
        title="Investigate infrastructure and take actions"
        disabled={disabled}
        onClick={() => onModeChange('agent')}
        className={clsx(
          'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all duration-200',
          mode === 'agent'
            ? 'bg-primary/20 text-primary border border-primary/30'
            : 'text-text-tertiary hover:text-text-secondary border border-transparent',
          disabled && 'opacity-50 cursor-not-allowed',
        )}
      >
        <Cpu className="h-3.5 w-3.5" />
        Agent
      </button>
    </div>
  );
}
