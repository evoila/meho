// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useAutocomplete Hook (Phase 63)
 *
 * Detects @ and / trigger characters from textarea input and cursor position.
 * Provides filtered autocomplete items, keyboard navigation, and selection handling.
 *
 * All autocomplete UI state (trigger, selectedIndex, items) is kept local to this hook
 * for fast keystroke handling. Only the mention selection result goes to Zustand
 * (needed by ChatPage.handleSendMessage).
 */
import { useState, useMemo, useCallback, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useQuery } from '@tanstack/react-query';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import { useChatStore } from '../stores/useChatStore';
import type { Connector } from '@/api/types/connector';
import type { Recipe } from '@/api/types/recipe';

export interface AutocompleteItem {
  id: string;
  label: string;
  description?: string;
  icon?: React.ReactNode;
  connectorType?: string;
}

interface AutocompleteTrigger {
  type: '@' | '/';
  query: string;
  startIndex: number;
}

/**
 * Detect trigger character from input text and cursor position.
 *
 * `/` trigger: only at start of input (first character is /).
 * `@` trigger: scan backward from cursor to find @ at a word boundary
 *   (start of input or preceded by whitespace).
 */
function detectTrigger(
  input: string,
  cursorPosition: number,
): AutocompleteTrigger | null {
  if (input.length === 0 || cursorPosition === 0) return null;

  // Slash: only when input starts with / and cursor is past it
  if (input.startsWith('/') && cursorPosition > 0) {
    return { type: '/', query: input.slice(1, cursorPosition), startIndex: 0 };
  }

  // @mention: scan backward from cursor to find @
  for (let i = cursorPosition - 1; i >= 0; i--) {
    const char = input[i];
    if (char === '@') {
      // Check it's at a word boundary (start of input or preceded by whitespace)
      if (i === 0 || /\s/.test(input[i - 1])) {
        return { type: '@', query: input.slice(i + 1, cursorPosition), startIndex: i };
      }
      break; // Found @ but not at word boundary
    }
    if (/\s/.test(char)) break; // Hit whitespace before finding @
  }

  return null;
}

/**
 * Filter connectors by prefix match on name (case-insensitive).
 * Maps to AutocompleteItem format.
 */
function filterConnectors(
  connectors: Connector[],
  query: string,
): AutocompleteItem[] {
  const lowerQuery = query.toLowerCase();
  return connectors
    .filter((c) => c.name.toLowerCase().startsWith(lowerQuery))
    .map((c) => ({
      id: c.id,
      label: c.name,
      connectorType: c.connector_type,
    }));
}

export function useAutocomplete(
  input: string,
  textareaRef: React.RefObject<HTMLTextAreaElement | null>,
  options?: {
    onRecipeSelect?: (recipe: Recipe) => void;
  },
) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  // Manual close flag: set true on explicit close, cleared on next input change
  const [closedManually, setClosedManually] = useState(false);
  const lastInputRef = useRef(input);
  const queryClient = useQueryClient();

  // Reset manual close when input changes
  if (input !== lastInputRef.current) {
    lastInputRef.current = input;
    if (closedManually) {
      setClosedManually(false);
    }
  }

  // Derive trigger from input + cursor position (pure computation, no side effects)
  const trigger = useMemo<AutocompleteTrigger | null>(() => {
    if (closedManually) return null;
    const cursorPos = textareaRef.current?.selectionStart ?? input.length;
    return detectTrigger(input, cursorPos);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- textareaRef.current reads cursor at render time
  }, [input, closedManually]);

  // Reset selected index when trigger changes
  const prevTriggerRef = useRef(trigger);
  if (trigger !== prevTriggerRef.current) {
    prevTriggerRef.current = trigger;
    if (selectedIndex !== 0) {
      setSelectedIndex(0);
    }
  }

  // Fetch connectors via react-query (cached, no redundant fetches)
  const hasMentionTrigger = trigger?.type === '@';
  const { data: connectors } = useQuery({
    queryKey: ['connectors'],
    queryFn: () => getAPIClient(config.apiURL).listConnectors(),
    enabled: hasMentionTrigger,
    staleTime: 60_000, // 1 min -- connectors don't change often
  });

  // Phase 63-03: Fetch recipes for / trigger
  const hasSlashTrigger = trigger?.type === '/';
  const { data: recipes } = useQuery({
    queryKey: ['recipes'],
    queryFn: () => getAPIClient(config.apiURL).listRecipes(),
    enabled: hasSlashTrigger,
    staleTime: 60_000,
  });

  // Derive items from trigger + cached data (pure computation)
  const items = useMemo<AutocompleteItem[]>(() => {
    if (!trigger) return [];

    if (trigger.type === '@') {
      // Try cache first, then use fetched data
      const cached = connectors ?? queryClient.getQueryData<Connector[]>(['connectors']);
      if (cached) {
        return filterConnectors(cached, trigger.query);
      }
      return [];
    }

    // Phase 63-03: '/' trigger -- filter recipes by prefix match on name
    if (trigger.type === '/') {
      const cachedRecipes = recipes ?? queryClient.getQueryData<Recipe[]>(['recipes']);
      if (cachedRecipes) {
        const lowerQuery = trigger.query.toLowerCase();
        return cachedRecipes
          .filter((r) => r.name.toLowerCase().startsWith(lowerQuery))
          .slice(0, 10) // Limit dropdown to 10 items
          .map((r) => ({
            id: r.id,
            label: r.name,
            description: r.description || r.original_question,
          }));
      }
      return [];
    }

    return [];
  }, [trigger, connectors, recipes, queryClient]);

  const isOpen = trigger !== null && items.length > 0;

  const close = useCallback(() => {
    setClosedManually(true);
  }, []);

  const selectItem = useCallback(
    (item: AutocompleteItem) => {
      if (!trigger) return;

      if (trigger.type === '@') {
        // Insert @name into input (replace from trigger.startIndex to cursor)
        const cursorPos = textareaRef.current?.selectionStart ?? input.length;
        const before = input.slice(0, trigger.startIndex);
        const after = input.slice(cursorPos);
        const newText = `${before}@${item.label} ${after}`;

        // Update input via Zustand
        useChatStore.getState().setInput(newText);

        // Store mention in Zustand
        useChatStore.getState().setActiveMention({
          connectorId: item.id,
          connectorName: item.label,
          connectorType: item.connectorType ?? '',
        });

        // Move cursor after the inserted mention
        const newCursorPos = before.length + 1 + item.label.length + 1; // @name + space
        requestAnimationFrame(() => {
          if (textareaRef.current) {
            textareaRef.current.selectionStart = newCursorPos;
            textareaRef.current.selectionEnd = newCursorPos;
            textareaRef.current.focus();
          }
        });
      } else if (trigger.type === '/') {
        // Phase 63-03: Recipe selection -- look up the full recipe object
        const cachedRecipes = recipes ?? queryClient.getQueryData<Recipe[]>(['recipes']);
        const recipe = cachedRecipes?.find((r) => r.id === item.id);
        if (recipe) {
          // Clear input immediately
          useChatStore.getState().setInput('');

          // Notify ChatPage of recipe selection (for param form or immediate execution)
          options?.onRecipeSelect?.(recipe);
        }
      }

      close();
    },
    [trigger, input, textareaRef, close, recipes, queryClient, options],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (!isOpen) return;

      switch (e.key) {
        case 'ArrowUp':
          e.preventDefault();
          setSelectedIndex((prev) => (prev <= 0 ? items.length - 1 : prev - 1));
          break;
        case 'ArrowDown':
          e.preventDefault();
          setSelectedIndex((prev) => (prev >= items.length - 1 ? 0 : prev + 1));
          break;
        case 'Enter':
          e.preventDefault();
          if (items[selectedIndex]) {
            selectItem(items[selectedIndex]);
          }
          break;
        case 'Escape':
          e.preventDefault();
          close();
          break;
      }
    },
    [isOpen, items, selectedIndex, selectItem, close],
  );

  return {
    trigger,
    items,
    selectedIndex,
    isOpen,
    handleKeyDown,
    selectItem,
    close,
  };
}
