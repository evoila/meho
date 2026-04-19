// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Session Sidebar - Displays and manages conversation sessions
 *
 * Features:
 * - Two tabs: My Chats and Team (Phase 38)
 * - List all chat sessions (My Chats tab)
 * - Team sessions with status indicators (Team tab)
 * - Create new sessions
 * - Select/switch between sessions
 * - Delete sessions
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, MessageSquare, Trash2, Search, History, Users } from 'lucide-react';
import { getAPIClient } from '../lib/api-client';
import { config } from '../lib/config';
import type { ChatSession } from '../lib/api-client';
import { motion } from 'motion/react';
import clsx from 'clsx';
import { useLicense } from '../hooks/useLicense';
import { useTeamSessions } from '@/features/chat/hooks/useTeamSessions';
import { TeamSessionItem } from '@/features/chat/components/TeamSessionItem';

interface ChatSessionSidebarProps {
  currentSessionId: string | null;
  onSelectSession: (session: ChatSession | null) => void;
  onNewSession: () => void;
}

export function ChatSessionSidebar({ // NOSONAR (cognitive complexity)
  currentSessionId,
  onSelectSession,
  onNewSession,
}: Readonly<ChatSessionSidebarProps>) {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'my-chats' | 'team'>('my-chats');
  const license = useLicense();
  const showTeamTab = license.edition === 'enterprise';
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // List sessions
  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ['chat-sessions'],
    queryFn: () => apiClient.listSessions(),
    // Phase 59: Faster polling when any session is active (detect is_active changes)
    refetchInterval: (query) => {
      const data = query.state.data as ChatSession[] | undefined;
      const hasActive = data?.some((s) => s.is_active);
      return hasActive ? 10000 : 30000;
    },
  });

  // Team sessions (Phase 38) — only fetch in enterprise mode
  const { data: teamSessions = [], isLoading: isTeamLoading } = useTeamSessions(showTeamTab);

  // Team badge count: sessions awaiting approval
  const teamBadgeCount = teamSessions.filter(
    (s) => s.status === 'awaiting_approval'
  ).length;

  // Delete session mutation
  const deleteMutation = useMutation({
    mutationFn: (sessionId: string) => apiClient.deleteSession(sessionId),
    onSuccess: () => {
      // Refetch sessions list
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
    },
  });

  // Filter sessions by search query
  const filteredSessions = sessions.filter((session) =>
    session.title?.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Group sessions by date
  const groupedSessions = filteredSessions.reduce((acc, session) => {
    const date = new Date(session.updated_at);
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    let group = 'Older';
    if (date.toDateString() === today.toDateString()) {
      group = 'Today';
    } else if (date.toDateString() === yesterday.toDateString()) {
      group = 'Yesterday';
    }

    if (!acc[group]) {
      acc[group] = [];
    }
    acc[group].push(session);
    return acc;
  }, {} as Record<string, ChatSession[]>);

  const handleDelete = (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (confirm('Delete this conversation?')) {
      deleteMutation.mutate(sessionId);
      if (currentSessionId === sessionId) {
        onNewSession();
      }
    }
  };

  // Handle team session click: adapt to ChatSession shape
  const handleTeamSessionClick = (teamSession: (typeof teamSessions)[0]) => {
    const adapted: ChatSession = {
      id: teamSession.id,
      title: teamSession.title,
      visibility: teamSession.visibility,
      created_at: teamSession.created_at,
      updated_at: teamSession.updated_at,
    };
    onSelectSession(adapted);
  };

  // Current tab item count
  const currentCount = (!showTeamTab || activeTab === 'my-chats') ? sessions.length : teamSessions.length;
  const currentLabel = (!showTeamTab || activeTab === 'my-chats') ? 'conversation' : 'team session';

  return (
    <div className="w-72 glass border-r border-white/5 flex flex-col h-full bg-black/20 backdrop-blur-xl">
      {/* Header with New Chat button */}
      <div className="p-4 border-b border-white/5">
        <button
          onClick={onNewSession}
          className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-gradient-to-r from-primary to-accent text-white rounded-xl font-medium hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] transition-all duration-200"
        >
          <Plus className="h-5 w-5" />
          New Chat
        </button>
      </div>

      {/* Search */}
      <div className="p-3">
        <div className="relative group">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-text-tertiary group-focus-within:text-primary transition-colors" />
          <input
            type="text"
            placeholder="Search conversations..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-3 py-2.5 text-sm bg-surface/50 border border-white/5 rounded-xl text-text-primary placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
          />
        </div>
      </div>

      {/* Tabs (Phase 38) - hidden in community mode (no Team tab) */}
      {showTeamTab && (
        <div className="px-3 flex border-b border-white/5">
          <button
            onClick={() => setActiveTab('my-chats')}
            className={clsx(
              'flex-1 py-2.5 text-xs font-medium text-center transition-all relative',
              activeTab === 'my-chats'
                ? 'text-primary'
                : 'text-text-tertiary hover:text-text-secondary'
            )}
          >
            My Chats
            {activeTab === 'my-chats' && (
              <motion.div
                layoutId="sidebar-tab-indicator"
                className="absolute bottom-0 left-2 right-2 h-0.5 bg-primary rounded-full"
              />
            )}
          </button>
          <button
            onClick={() => setActiveTab('team')}
            className={clsx(
              'flex-1 py-2.5 text-xs font-medium text-center transition-all relative',
              activeTab === 'team'
                ? 'text-primary'
                : 'text-text-tertiary hover:text-text-secondary'
            )}
          >
            <span className="inline-flex items-center gap-1.5">
              Team
              {teamBadgeCount > 0 && (
                <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 text-[10px] font-bold text-amber-400 bg-amber-500/20 border border-amber-500/30 rounded-full">
                  {teamBadgeCount}
                </span>
              )}
            </span>
            {activeTab === 'team' && (
              <motion.div
                layoutId="sidebar-tab-indicator"
                className="absolute bottom-0 left-2 right-2 h-0.5 bg-primary rounded-full"
              />
            )}
          </button>
        </div>
      )}

      {/* Sessions list */}
      <div className="flex-1 overflow-y-auto scrollbar-hide px-2">
        {(!showTeamTab || activeTab === 'my-chats') ? (
          // My Chats tab
          <>
            {(() => {
              if (isLoading) return (
                <div className="p-8 text-center">
                  <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-2" />
                  <p className="text-xs text-text-tertiary">Loading history...</p>
                </div>
              );
              if (filteredSessions.length === 0) return (
              <div className="p-8 text-center text-text-tertiary flex flex-col items-center gap-3 opacity-60">
                <History className="h-8 w-8" />
                <p className="text-sm">
                  {searchQuery ? 'No conversations found' : 'No history yet'}
                </p>
              </div>
              );
              return (
              <div className="py-2 space-y-6">
                {Object.entries(groupedSessions).map(([group, groupSessions]) => (
                  <div key={group}>
                    <div className="px-3 mb-2 text-[10px] font-bold text-text-tertiary uppercase tracking-wider opacity-70">
                      {group}
                    </div>
                    <div className="space-y-1">
                      {groupSessions.map((session) => (
                        <motion.div
                          key={session.id}
                          layoutId={`session-${session.id}`}
                          onClick={() => onSelectSession(session)}
                          className={clsx(
                            'group relative flex items-center gap-3 px-3 py-3 rounded-xl cursor-pointer transition-all duration-200',
                            currentSessionId === session.id
                              ? 'bg-primary/10 text-primary'
                              : 'hover:bg-white/5 text-text-secondary hover:text-text-primary'
                          )}
                        >
                          {currentSessionId === session.id && (
                            <motion.div
                              layoutId="active-session-indicator"
                              className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-8 bg-primary rounded-r-full"
                            />
                          )}

                          <MessageSquare
                            className={clsx(
                              'h-4 w-4 flex-shrink-0 transition-colors',
                              currentSessionId === session.id
                                ? 'text-primary'
                                : 'text-text-tertiary group-hover:text-text-secondary'
                            )}
                          />

                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5">
                              <div
                                className={clsx(
                                  'text-sm font-medium truncate transition-colors',
                                  currentSessionId === session.id
                                    ? 'text-primary'
                                    : 'text-text-secondary group-hover:text-text-primary'
                                )}
                              >
                                {session.title || 'New conversation'}
                              </div>
                              {/* Phase 59: Active investigation indicator */}
                              {session.is_active && (
                                <span
                                  className="w-2 h-2 rounded-full bg-green-500 animate-pulse flex-shrink-0"
                                  title="Investigation in progress"
                                />
                              )}
                            </div>
                            {session.message_count !== undefined && (
                              <div className="text-[10px] text-text-tertiary opacity-70 group-hover:opacity-100 transition-opacity">
                                {session.message_count} messages
                              </div>
                            )}
                          </div>

                          <button
                            onClick={(e) => handleDelete(e, session.id)}
                            className="opacity-0 group-hover:opacity-100 p-1.5 hover:bg-red-500/10 text-text-tertiary hover:text-red-400 rounded-lg transition-all"
                            title="Delete conversation"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </motion.div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              );
            })()}
          </>
        ) : (
          // Team tab (Phase 38)
          <>
            {(() => {
              if (isTeamLoading) return (
                <div className="p-8 text-center">
                  <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-2" />
                  <p className="text-xs text-text-tertiary">Loading team sessions...</p>
                </div>
              );
              if (teamSessions.length === 0) return (
              <div className="p-8 text-center text-text-tertiary flex flex-col items-center gap-3 opacity-60">
                <Users className="h-8 w-8" />
                <p className="text-sm">No team investigations</p>
              </div>
              );
              return (
              <div className="py-2 space-y-1">
                {teamSessions.map((session) => (
                  <TeamSessionItem
                    key={session.id}
                    session={session}
                    isActive={currentSessionId === session.id}
                    onClick={() => handleTeamSessionClick(session)}
                  />
                ))}
              </div>
              );
            })()}
          </>
        )}
      </div>

      {/* Footer with count */}
      <div className="p-4 border-t border-white/5 text-[10px] text-text-tertiary text-center font-medium uppercase tracking-wider opacity-50">
        {currentCount} {currentLabel}{currentCount !== 1 ? 's' : ''}
      </div>
    </div>
  );
}
