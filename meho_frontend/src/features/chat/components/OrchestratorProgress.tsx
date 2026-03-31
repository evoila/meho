// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator Progress Component
 *
 * Shows multi-agent progress for the orchestrator.
 * Displays connector cards with status, iteration indicator,
 * and early findings preview.
 */
import { useState, useMemo, useEffect } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { ChevronDown, ChevronRight, Layers, Zap } from 'lucide-react';
import clsx from 'clsx';
import type {
  OrchestratorEvent,
  ConnectorState,
  ConnectorStatus,
  WrappedAgentEvent,
} from '@/api/types/orchestrator';
import { ConnectorCard } from './ConnectorCard';
import { InvestigationPlan } from './InvestigationPlan';
import { useChatStore } from '../stores';

interface OrchestratorProgressProps {
  events: OrchestratorEvent[];
  startTime: number;
  isLive?: boolean;
}

/**
 * Extended connector state with timing information.
 */
interface ConnectorStateWithTiming extends ConnectorState {
  firstEventTime?: number;
  eventsWithTimestamps: Array<WrappedAgentEvent & { arrivalTime: number }>;
}

/**
 * Process events to build connector states with timing information.
 */
export function buildConnectorStates(
  events: OrchestratorEvent[],
  startTime: number
): Map<string, ConnectorStateWithTiming> {
  const states = new Map<string, ConnectorStateWithTiming>();

  for (let i = 0; i < events.length; i++) {
    const event = events[i];
    const eventWithTime = event as OrchestratorEvent & { _arrivalTime?: number };
    const eventTime = eventWithTime._arrivalTime || startTime;

    if (event.type === 'dispatch_start') {
      const flatEvent = event as { 
        connectors?: Array<{ id: string; name: string }>;
        data?: { connectors?: Array<{ id: string; name: string }> };
      };
      const connectors = flatEvent.connectors || flatEvent.data?.connectors;
      if (!connectors) continue;
      for (const conn of connectors) {
        if (!states.has(conn.id)) {
          states.set(conn.id, {
            id: conn.id,
            name: conn.name,
            status: 'running',
            events: [],
            eventsWithTimestamps: [],
          });
        } else {
          const existing = states.get(conn.id);
          if (!existing) continue;
          existing.status = 'running';
        }
      }
    } else if (event.type === 'early_findings' || event.type === 'connector_complete') {
      const flatEvent = event as { 
        connector_id?: string; 
        status?: string; 
        findings_preview?: string;
        data?: { connector_id?: string; status?: string; findings_preview?: string };
      };
      const id = flatEvent.connector_id || flatEvent.data?.connector_id;
      if (!id) continue;
      const existing = states.get(id);
      if (existing) {
        const status = flatEvent.status || flatEvent.data?.status;
        const findings = flatEvent.findings_preview ?? flatEvent.data?.findings_preview;
        existing.status = (status as ConnectorStatus) || existing.status;
        existing.findings = findings;
      }
    } else if (event.type === 'agent_event') {
      const flatEvent = event as { 
        agent_source?: { connector_id?: string }; 
        inner_event?: WrappedAgentEvent['inner_event'];
        data?: { agent_source?: { connector_id?: string }; inner_event?: WrappedAgentEvent['inner_event'] };
      };
      const agentSource = flatEvent.agent_source || flatEvent.data?.agent_source;
      const innerEvent = flatEvent.inner_event || flatEvent.data?.inner_event;
      const id = agentSource?.connector_id;
      
      if (id) {
        const existing = states.get(id);
        if (existing) {
          const wrapped: WrappedAgentEvent = {
            type: 'agent_event',
            agent_source: agentSource as WrappedAgentEvent['agent_source'],
            inner_event: innerEvent || { type: 'unknown', data: {} },
            _arrivalTime: eventTime,
          };
          existing.events.push(wrapped);
          
          existing.eventsWithTimestamps.push({
            ...wrapped,
            arrivalTime: eventTime,
          });
          
          if (!existing.firstEventTime) {
            existing.firstEventTime = eventTime;
          }
        }
      }
    }
  }

  return states;
}

/**
 * Extract iteration info from events.
 * Shows the iteration that actually dispatched to connectors,
 * avoiding counting the final "decide to respond" loop.
 */
function getIterationInfo(events: OrchestratorEvent[]): {
  currentIteration: number;
  maxIterations: number;
} {
  let lastDispatchIteration = 0;
  const maxIterations = 3;

  for (const event of events) {
    if (event.type === 'dispatch_start' || event.type === 'iteration_complete') {
      const flatEvent = event as { 
        iteration?: number;
        data?: { iteration?: number };
      };
      const iteration = flatEvent.iteration ?? flatEvent.data?.iteration;
      if (iteration && iteration > lastDispatchIteration) {
        lastDispatchIteration = iteration;
      }
    }
  }

  const currentIteration = lastDispatchIteration || (events.length > 0 ? 1 : 0);

  return { currentIteration, maxIterations };
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

function getElapsedColor(ms: number): string {
  if (ms < 5000) return 'text-emerald-400';
  if (ms < 10000) return 'text-green-400';
  if (ms < 15000) return 'text-yellow-400';
  if (ms < 20000) return 'text-amber-400';
  return 'text-red-400';
}

export function OrchestratorProgress({
  events,
  startTime,
  isLive = false,
}: OrchestratorProgressProps) {
  const investigationPlan = useChatStore(state => state.investigationPlan);
  const [isExpanded, setIsExpanded] = useState(isLive);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [elapsedFrozen, setElapsedFrozen] = useState<number | null>(null);

  const connectorStates = useMemo(() => buildConnectorStates(events, startTime), [events, startTime]);
  const connectors = useMemo(() => Array.from(connectorStates.values()), [connectorStates]);

  const { currentIteration, maxIterations } = useMemo(
    () => getIterationInfo(events),
    [events]
  );

  const completedCount = useMemo(
    () => connectors.filter((c) => c.status !== 'pending' && c.status !== 'running').length,
    [connectors]
  );

  const hasErrors = useMemo(
    () => connectors.some((c) => c.status === 'failed' || c.status === 'timeout'),
    [connectors]
  );

  const isComplete = useMemo(
    () => events.some((e) =>
      e.type === 'orchestrator_complete' ||
      e.type === 'final_answer' ||
      e.type === 'synthesis_start'
    ),
    [events]
  );

  useEffect(() => {
    if (!isLive || isComplete) return;
    const interval = setInterval(() => {
      setLiveElapsed(Date.now() - startTime);
    }, 100);
    return () => clearInterval(interval);
  }, [isLive, isComplete, startTime]);

  // Freeze elapsed when investigation completes (setState-during-render)
  const [prevComplete, setPrevComplete] = useState(false);
  if (isComplete && !prevComplete && liveElapsed > 0) {
    setPrevComplete(true);
    setElapsedFrozen(liveElapsed);
  }
  if (!isComplete && prevComplete) {
    setPrevComplete(false);
  }

  // Reset on new startTime (setState-during-render)
  const [prevStartTime, setPrevStartTime] = useState(startTime);
  if (startTime !== prevStartTime) {
    setPrevStartTime(startTime);
    setElapsedFrozen(null);
    setLiveElapsed(0);
  }

  const displayElapsed = useMemo(() => {
    if (elapsedFrozen !== null) {
      return elapsedFrozen;
    }
    if (isLive && !isComplete) {
      return liveElapsed;
    }
    const completeEvent = events.find((e) => e.type === 'orchestrator_complete');
    if (completeEvent && completeEvent.type === 'orchestrator_complete') {
      // SSE adapter flattens event data, so handle both nested and flat formats
      const flat = completeEvent as unknown as Record<string, unknown>;
      return (completeEvent.data?.total_time_ms ?? flat.total_time_ms ?? liveElapsed) as number;
    }
    if (events.length > 0) {
      const lastEvent = events[events.length - 1] as OrchestratorEvent & { _arrivalTime?: number };
      if (lastEvent._arrivalTime) {
        return lastEvent._arrivalTime - startTime;
      }
    }
    return liveElapsed;
  }, [events, isLive, isComplete, liveElapsed, startTime, elapsedFrozen]);

  if (events.length === 0) return null;

  return (
    <div className="flex gap-4 mb-3 px-4">
      <div className="flex-shrink-0 w-9" />

      <div className="flex-1 max-w-3xl">
        {/* Header */}
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className={clsx(
            'w-full flex items-center gap-2 px-3 py-2 rounded-md transition-all text-xs',
            'bg-slate-800/60 hover:bg-slate-800/80 border border-slate-700/40'
          )}
        >
          <span className="text-slate-500">
            {isExpanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </span>

          <Layers className="w-3.5 h-3.5 text-cyan-400" />
          <span className="font-medium text-slate-300">Orchestrator</span>

          <div className="flex items-center gap-2 ml-2">
            <span className="px-1.5 py-0.5 rounded bg-slate-700/50 text-slate-400 font-mono text-xs">
              Iter {currentIteration}/{maxIterations}
            </span>

            <span
              className={clsx(
                'px-1.5 py-0.5 rounded font-mono text-xs',
                isComplete
                  ? 'bg-emerald-900/30 text-emerald-400'
                  : 'bg-cyan-900/30 text-cyan-400'
              )}
            >
              {isComplete ? (
                <>&#10003; Complete</>
              ) : (
                <>
                  {completedCount}/{connectors.length} agents
                </>
              )}
            </span>

            {hasErrors && (
              <span className="px-1.5 py-0.5 rounded bg-red-900/30 text-red-400 font-mono text-xs">
                &#9888; Errors
              </span>
            )}
          </div>

          <div className="flex items-center gap-3 ml-auto font-mono text-xs">
            <span title="Total elapsed time">
              <span className="text-slate-500 mr-1">Elapsed</span>
              <span className={getElapsedColor(displayElapsed)}>
                {formatDuration(displayElapsed)}
              </span>
            </span>
          </div>
        </button>

        <AnimatePresence>
          {isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="overflow-hidden"
            >
              <div className="mt-2 space-y-2">
                {/* Phase 99: Investigation Plan (before connector cards) */}
                {investigationPlan && <InvestigationPlan plan={investigationPlan} />}

                {connectors.length === 0 ? (
                  <div className="px-3 py-4 rounded-md bg-slate-900/60 border border-slate-800/50 text-center">
                    <div className="flex items-center justify-center gap-2 text-slate-500 text-sm">
                      {isComplete ? (
                        <span>No connectors were queried for this request</span>
                      ) : (
                        <>
                          <Zap className="w-4 h-4 animate-pulse" />
                          <span>Routing to connectors...</span>
                        </>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="grid gap-2">
                    {connectors.map((connector) => (
                      <ConnectorCard
                        key={connector.id}
                        connector={connector}
                        startTime={startTime}
                        totalElapsed={displayElapsed}
                        defaultExpanded={connector.status === 'failed' || connector.status === 'timeout'}
                        isLive={isLive && connector.status === 'running'}
                      />
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
