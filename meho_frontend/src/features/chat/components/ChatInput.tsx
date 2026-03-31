// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Input Component
 *
 * Text input area for sending messages to MEHO.
 * Phase 63: Extended with autocomplete dropdown integration for @ and / triggers.
 * Phase 65-05: Extended with Ask/Agent mode toggle.
 */
import { useCallback, type KeyboardEvent, type ChangeEvent } from 'react';
import { Send, StopCircle, ArrowUp } from 'lucide-react';
import clsx from 'clsx';
import { AutocompleteDropdown } from './AutocompleteDropdown';
import { ModeToggle } from './ModeToggle';
import type { AutocompleteItem } from '../hooks/useAutocomplete';

interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onKeyPress?: (e: KeyboardEvent<HTMLTextAreaElement>) => void;
  isProcessing: boolean;
  /** Disables input entirely (e.g., during approval modal) */
  disabled?: boolean;
  /** Phase 39: War room processing state -- input blocked while agent processes in group session */
  isWarRoomProcessing?: boolean;
  userName?: string;
  /** @deprecated Use userName instead */
  userEmail?: string;
  /** Phase 63: Autocomplete dropdown items */
  autocompleteItems?: AutocompleteItem[];
  /** Phase 63: Currently selected autocomplete index */
  autocompleteSelectedIndex?: number;
  /** Phase 63: Whether autocomplete dropdown is visible */
  autocompleteVisible?: boolean;
  /** Phase 63: Active trigger type (@ or /) */
  autocompleteTriggerType?: '@' | '/' | null;
  /** Phase 63: Handler for autocomplete item selection */
  onAutocompleteSelect?: (item: AutocompleteItem) => void;
  /** Phase 63: Handler for autocomplete keyboard navigation */
  onAutocompleteKeyDown?: (e: KeyboardEvent<HTMLTextAreaElement>) => void;
  /** Phase 63: Forwarded textarea ref for cursor position tracking */
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
  /** Phase 65-05: Current session mode (ask or agent) */
  sessionMode?: 'ask' | 'agent';
  /** Phase 65-05: Handler for session mode change */
  onSessionModeChange?: (mode: 'ask' | 'agent') => void;
}

export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  onKeyPress,
  isProcessing,
  disabled = false,
  isWarRoomProcessing = false,
  userName,
  userEmail,
  autocompleteItems = [],
  autocompleteSelectedIndex = 0,
  autocompleteVisible = false,
  autocompleteTriggerType = null,
  onAutocompleteSelect,
  onAutocompleteKeyDown,
  textareaRef,
  sessionMode = 'agent',
  onSessionModeChange,
}: ChatInputProps) {
  const displayName = userName || userEmail;
  const isDisabled = isProcessing || disabled || isWarRoomProcessing;

  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Phase 63: Route to autocomplete handler first
    if (onAutocompleteKeyDown) {
      onAutocompleteKeyDown(e);
      // If autocomplete handled the key (prevented default), don't process further
      if (e.defaultPrevented) return;
    }

    if (onKeyPress) {
      onKeyPress(e);
    } else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  }, [onSend, onKeyPress, onAutocompleteKeyDown]);

  const handleChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(e.target.value);
  }, [onChange]);

  return (
    <div className="p-6 relative z-20">
      <div className="max-w-4xl mx-auto">
        <div className="relative group">
          {/* Phase 63: Autocomplete dropdown positioned above input */}
          <AutocompleteDropdown
            items={autocompleteItems}
            selectedIndex={autocompleteSelectedIndex}
            onSelect={(item) => onAutocompleteSelect?.(item)}
            visible={autocompleteVisible}
            triggerType={autocompleteTriggerType}
          />

          <div className="absolute -inset-0.5 bg-gradient-to-r from-primary to-accent rounded-2xl opacity-20 group-hover:opacity-40 transition duration-500 blur" />
          <div className="relative bg-surface rounded-2xl border border-white/10 shadow-2xl">
            {/* Phase 65-05: Mode toggle above textarea */}
            {onSessionModeChange && (
              <div className="flex items-center justify-between px-2 pt-2">
                <ModeToggle
                  mode={sessionMode}
                  onModeChange={onSessionModeChange}
                  disabled={isDisabled}
                />
              </div>
            )}

            <div className="flex items-end gap-2 p-2">
              <textarea
                ref={textareaRef}
                value={value}
                onChange={handleChange}
                onKeyDown={handleKeyDown}
                placeholder={isWarRoomProcessing ? 'MEHO is processing...' : disabled ? 'Respond to the approval request...' : sessionMode === 'ask' ? 'Ask a question about your infrastructure...' : 'Ask MEHO anything...'}
                rows={1}
                disabled={isDisabled}
                data-testid="chat-input"
                className={clsx(
                  'flex-1 max-h-[200px] min-h-[50px] w-full bg-transparent border-0 text-white placeholder-text-tertiary focus:ring-0 resize-none py-3 px-4 leading-relaxed',
                  disabled && 'pointer-events-none opacity-50'
                )}
                style={{ minHeight: '50px' }}
              />

              <div className="pb-1 pr-1">
                {isProcessing ? (
                  <button
                    onClick={onStop}
                    className="p-3 bg-red-500/10 text-red-400 rounded-xl hover:bg-red-500/20 transition-all"
                    title="Stop generation"
                  >
                    <StopCircle className="h-5 w-5" />
                  </button>
                ) : (
                  <button
                    onClick={onSend}
                    disabled={!value.trim()}
                    data-testid="chat-send-button"
                    className={clsx(
                      "p-3 rounded-xl transition-all duration-200",
                      value.trim()
                        ? "bg-primary text-white shadow-lg shadow-primary/25 hover:scale-105 active:scale-95"
                        : "bg-surface-active text-text-tertiary cursor-not-allowed"
                    )}
                  >
                    {value.trim() ? <ArrowUp className="h-5 w-5" /> : <Send className="h-5 w-5" />}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="mt-3 flex items-center justify-between px-2">
          <div className="flex items-center gap-4">
            <p className="text-xs text-text-tertiary flex items-center gap-2">
              <span className="px-1.5 py-0.5 rounded border border-white/10 bg-white/5 font-mono text-[10px]">&#8629;</span>
              to send
              <span className="px-1.5 py-0.5 rounded border border-white/10 bg-white/5 font-mono text-[10px] ml-2">&#8679; &#8629;</span>
              for new line
            </p>
            {/* Phase 65-05: Mode indicator */}
            {onSessionModeChange && (
              <p className="text-[11px] text-text-tertiary">
                {sessionMode === 'ask'
                  ? 'Ask mode \u2014 searching knowledge base'
                  : 'Agent mode \u2014 can investigate and take actions'}
              </p>
            )}
          </div>
          {displayName && (
            <p className="text-xs text-text-tertiary">
              Signed in as <span className="text-text-secondary font-medium">{displayName}</span>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
