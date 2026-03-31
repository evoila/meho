// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Empty State Component
 *
 * Displayed when no messages are present. Shows contextual suggestions:
 * - No knowledge + no connectors: day-one CTA guiding to /knowledge upload
 * - Knowledge exists but no connectors: ask-mode prompt
 * - Connectors exist: agent-mode investigation suggestions
 *
 * Agent mode remains the default for new sessions. The CTA is visual guidance only.
 */
import { motion } from 'motion/react';
import { BookOpen, Plug, Sparkles, Terminal, Search } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getAPIClient } from '../../../lib/api-client';
import { config } from '../../../lib/config';
import type { KnowledgeTreeResponse } from '../../../api/types/knowledge';
import type { Connector } from '../../../api/types/connector';

interface ChatEmptyStateProps {
  onSuggestionClick: (text: string) => void;
}

export function ChatEmptyState({ onSuggestionClick }: ChatEmptyStateProps) {
  const apiClient = getAPIClient(config.apiURL);

  // Quick checks for knowledge and connector state
  const { data: knowledgeTree } = useQuery<KnowledgeTreeResponse>({
    queryKey: ['knowledge-tree'],
    queryFn: () => apiClient.getKnowledgeTree(),
    staleTime: 60_000,
  });

  const { data: connectors } = useQuery<Connector[]>({
    queryKey: ['connectors'],
    queryFn: () => apiClient.listConnectors(),
    staleTime: 60_000,
  });

  const hasKnowledge = knowledgeTree
    ? knowledgeTree.global.document_count > 0 ||
      knowledgeTree.types.some(t => t.document_count > 0)
    : false;
  const hasConnectors = (connectors?.length ?? 0) > 0;

  // ------------------------------------------------------------------
  // State 1: No knowledge AND no connectors -- day-one CTA
  // ------------------------------------------------------------------
  if (!hasKnowledge && !hasConnectors) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="text-center py-20"
      >
        <div className="w-24 h-24 mx-auto mb-8 relative">
          <div className="absolute inset-0 bg-gradient-to-br from-primary to-accent rounded-3xl blur-2xl opacity-20 animate-pulse-slow" />
          <div className="relative w-full h-full bg-surface border border-white/10 rounded-3xl flex items-center justify-center shadow-2xl">
            <Sparkles className="w-10 h-10 text-primary" />
          </div>
        </div>
        <h3 className="text-3xl font-bold text-white mb-3 tracking-tight">
          Get Started with MEHO
        </h3>
        <p className="text-text-secondary mb-10 max-w-lg mx-auto text-lg">
          Upload your first documentation to start asking questions about your infrastructure, runbooks, and guides.
        </p>

        <div className="flex flex-col items-center gap-4 max-w-md mx-auto">
          <Link
            to="/knowledge"
            className="w-full group flex items-center justify-center gap-3 px-6 py-4 bg-primary/20 hover:bg-primary/30 border border-primary/30 hover:border-primary/50 rounded-2xl transition-all text-white font-semibold text-lg hover:shadow-lg hover:shadow-primary/10"
          >
            <BookOpen className="w-6 h-6 text-primary group-hover:scale-110 transition-transform" />
            Upload Docs
          </Link>

          <p className="text-sm text-text-tertiary max-w-sm">
            MEHO can answer questions about your infrastructure documentation, runbooks, and guides
          </p>

          <div className="flex items-center gap-2 mt-2 text-sm text-text-tertiary">
            <span>Or</span>
            <Link
              to="/connectors"
              className="inline-flex items-center gap-1.5 text-text-secondary hover:text-primary transition-colors"
            >
              <Plug className="w-3.5 h-3.5" />
              configure connectors
            </Link>
            <span>to investigate live infrastructure</span>
          </div>
        </div>
      </motion.div>
    );
  }

  // ------------------------------------------------------------------
  // State 2: Knowledge exists but no connectors -- ask-mode prompt
  // ------------------------------------------------------------------
  if (hasKnowledge && !hasConnectors) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="text-center py-20"
      >
        <div className="w-24 h-24 mx-auto mb-8 relative">
          <div className="absolute inset-0 bg-gradient-to-br from-primary to-accent rounded-3xl blur-2xl opacity-20 animate-pulse-slow" />
          <div className="relative w-full h-full bg-surface border border-white/10 rounded-3xl flex items-center justify-center shadow-2xl">
            <Search className="w-10 h-10 text-primary" />
          </div>
        </div>
        <h3 className="text-3xl font-bold text-white mb-3 tracking-tight">
          Ask MEHO about your docs
        </h3>
        <p className="text-text-secondary mb-4 max-w-md mx-auto text-lg">
          Switch to <span className="text-primary font-medium">Ask mode</span> to search your knowledge base
        </p>
        <p className="text-text-tertiary mb-10 max-w-md mx-auto text-sm">
          Try asking: "How is networking configured in VCF 5.2?"
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-2xl mx-auto">
          <button
            onClick={() => onSuggestionClick('Summarize the key topics in my uploaded docs')}
            className="group p-4 bg-surface/50 hover:bg-surface border border-white/5 hover:border-primary/30 rounded-2xl transition-all text-left hover:shadow-lg hover:shadow-primary/5"
          >
            <div className="flex items-center gap-3 mb-2">
              <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400 group-hover:bg-blue-500/20 transition-colors">
                <BookOpen className="w-4 h-4" />
              </div>
              <span className="font-semibold text-white">Knowledge Search</span>
            </div>
            <p className="text-sm text-text-secondary">Ask about your uploaded documentation</p>
          </button>

          <Link
            to="/connectors"
            className="group p-4 bg-surface/50 hover:bg-surface border border-white/5 hover:border-primary/30 rounded-2xl transition-all text-left hover:shadow-lg hover:shadow-primary/5"
          >
            <div className="flex items-center gap-3 mb-2">
              <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400 group-hover:bg-emerald-500/20 transition-colors">
                <Plug className="w-4 h-4" />
              </div>
              <span className="font-semibold text-white">Add Connectors</span>
            </div>
            <p className="text-sm text-text-secondary">Connect live infrastructure for agent investigations</p>
          </Link>
        </div>
      </motion.div>
    );
  }

  // ------------------------------------------------------------------
  // State 3: Connectors exist -- agent-mode suggestions (current behavior)
  // ------------------------------------------------------------------
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className="text-center py-20"
    >
      <div className="w-24 h-24 mx-auto mb-8 relative">
        <div className="absolute inset-0 bg-gradient-to-br from-primary to-accent rounded-3xl blur-2xl opacity-20 animate-pulse-slow" />
        <div className="relative w-full h-full bg-surface border border-white/10 rounded-3xl flex items-center justify-center shadow-2xl">
          <Sparkles className="w-10 h-10 text-primary" />
        </div>
      </div>
      <h3 className="text-3xl font-bold text-white mb-3 tracking-tight">
        How can I help you?
      </h3>
      <p className="text-text-secondary mb-10 max-w-md mx-auto text-lg">
        I can help you monitor systems, diagnose issues, and automate complex workflows.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-2xl mx-auto">
        <button
          onClick={() => onSuggestionClick('What systems are available for monitoring?')}
          className="group p-4 bg-surface/50 hover:bg-surface border border-white/5 hover:border-primary/30 rounded-2xl transition-all text-left hover:shadow-lg hover:shadow-primary/5"
        >
          <div className="flex items-center gap-3 mb-2">
            <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400 group-hover:bg-blue-500/20 transition-colors">
              <Terminal className="w-4 h-4" />
            </div>
            <span className="font-semibold text-white">System Status</span>
          </div>
          <p className="text-sm text-text-secondary">Check available monitoring systems and health</p>
        </button>

        <button
          onClick={() => onSuggestionClick('Help me diagnose why my-app is slow')}
          className="group p-4 bg-surface/50 hover:bg-surface border border-white/5 hover:border-primary/30 rounded-2xl transition-all text-left hover:shadow-lg hover:shadow-primary/5"
        >
          <div className="flex items-center gap-3 mb-2">
            <div className="p-2 rounded-lg bg-purple-500/10 text-purple-400 group-hover:bg-purple-500/20 transition-colors">
              <Sparkles className="w-4 h-4" />
            </div>
            <span className="font-semibold text-white">Diagnostics</span>
          </div>
          <p className="text-sm text-text-secondary">Investigate performance issues and errors</p>
        </button>
      </div>

      {/* Knowledge nudge when docs are available */}
      {hasKnowledge && (
        <p className="mt-8 text-sm text-text-tertiary">
          Switch to <span className="text-primary">Ask mode</span> to search your knowledge base
        </p>
      )}
    </motion.div>
  );
}
