import { useState, useEffect, useRef, useCallback } from 'react';

interface InvestigationStep {
  type: 'user' | 'connector' | 'synthesis';
  system?: string;
  systemColor?: string;
  label?: string;
  action?: string;
  result?: string;
  delay: number;
}

const investigationSteps: InvestigationStep[] = [
  {
    type: 'user',
    action: 'The payment service has been slow for the last 30 minutes',
    delay: 0,
  },
  {
    type: 'connector',
    system: 'Prometheus',
    systemColor: '#E6522C',
    label: 'connector query',
    action: 'Querying payment-svc RED metrics...',
    result: 'p99 latency spiked from 120ms to 2.4s at 14:32 UTC. Error rate 12.3%. Request volume normal — not a traffic surge.',
    delay: 1200,
  },
  {
    type: 'connector',
    system: 'Kubernetes',
    systemColor: '#3B82F6',
    label: 'connector query',
    action: 'Inspecting pods on affected nodes...',
    result: 'Pod payment-svc-7b9f4d-xk2p4 on node-7: 3 OOMKilled restarts in last hour. Memory limit 512Mi, usage peaked at 510Mi. Node-7 memory pressure: True.',
    delay: 1800,
  },
  {
    type: 'connector',
    system: 'Loki',
    systemColor: '#F9A825',
    label: 'connector query',
    action: 'Pulling error logs from payment-svc...',
    result: 'ERROR 14:31:47 "Connection pool exhausted, waiting for available connection" (47 occurrences). WARN "GC pause 340ms" (12 occurrences).',
    delay: 1400,
  },
  {
    type: 'connector',
    system: 'VMware',
    systemColor: '#10B981',
    label: 'connector query',
    action: 'Checking node-7 VM and ESXi host...',
    result: 'VM node-7: 4 vCPU, 8GB RAM. Host esxi-prod-03: 92% memory utilization (187GB / 204GB). Memory ballooning active on 6 VMs including node-7.',
    delay: 1600,
  },
  {
    type: 'connector',
    system: 'ArgoCD',
    systemColor: '#F97316',
    label: 'connector query',
    action: 'Checking recent deployments...',
    result: 'Last sync 14:28 UTC: payment-svc image updated v2.3.1 → v2.4.0. Change: added in-memory cache (commit abc1234).',
    delay: 1200,
  },
  {
    type: 'synthesis',
    system: 'MEHO',
    systemColor: '#8051B8',
    label: 'synthesis',
    action: 'Synthesizing root cause...',
    result: 'payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache, increasing baseline memory usage. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills. Remaining pods overloaded, saturating the connection pool (20/20 active, 47 pending), driving p99 latency from 120ms to 2.4s.',
    delay: 2000,
  },
];

const allSystems = [
  { name: 'Prometheus', color: '#E6522C' },
  { name: 'Kubernetes', color: '#3B82F6' },
  { name: 'Loki', color: '#F9A825' },
  { name: 'VMware', color: '#10B981' },
  { name: 'ArgoCD', color: '#F97316' },
];

function SystemBadge({ name, color, active }: { name: string; color: string; active: boolean }) {
  return (
    <span
      className="hidden sm:inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-300"
      style={{
        backgroundColor: active ? `${color}15` : 'transparent',
        borderWidth: '1px',
        borderColor: active ? `${color}40` : '#27272A',
        color: active ? color : '#71717A',
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full transition-all duration-300"
        style={{ backgroundColor: active ? color : '#3F3F46' }}
      />
      {name}
    </span>
  );
}

function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 ml-1">
      <span className="h-1 w-1 rounded-full bg-[#71717A] animate-bounce" style={{ animationDelay: '0ms' }} />
      <span className="h-1 w-1 rounded-full bg-[#71717A] animate-bounce" style={{ animationDelay: '150ms' }} />
      <span className="h-1 w-1 rounded-full bg-[#71717A] animate-bounce" style={{ animationDelay: '300ms' }} />
    </span>
  );
}

function InvestigationDemo() {
  const [visibleSteps, setVisibleSteps] = useState<number>(0);
  const [isTyping, setIsTyping] = useState(false);
  const [activeSystem, setActiveSystem] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const timeoutsRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const cleanup = useCallback(() => {
    timeoutsRef.current.forEach(clearTimeout);
    timeoutsRef.current = [];
  }, []);

  const runInvestigation = useCallback(() => {
    cleanup();
    setVisibleSteps(0);
    setIsTyping(false);
    setActiveSystem(null);
    setIsRunning(true);
    setHasRun(true);

    let cumulativeDelay = 500;

    investigationSteps.forEach((step, index) => {
      cumulativeDelay += step.delay;

      const typingTimeout = setTimeout(() => {
        setIsTyping(true);
        setActiveSystem(step.system || null);
      }, cumulativeDelay - 600);
      timeoutsRef.current.push(typingTimeout);

      const showTimeout = setTimeout(() => {
        setVisibleSteps(index + 1);
        setIsTyping(false);
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
      }, cumulativeDelay);
      timeoutsRef.current.push(showTimeout);
    });

    const doneTimeout = setTimeout(() => {
      setIsRunning(false);
      setActiveSystem(null);
    }, cumulativeDelay + 500);
    timeoutsRef.current.push(doneTimeout);
  }, [cleanup]);

  useEffect(() => {
    const timer = setTimeout(() => runInvestigation(), 1000);
    return () => { clearTimeout(timer); cleanup(); };
  }, [runInvestigation, cleanup]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [visibleSteps, isTyping]);

  return (
    <section className="px-6 py-24">
      <div className="mx-auto max-w-5xl">
        <div className="text-center mb-12">
          <h2 className="text-3xl font-bold md:text-4xl" style={{ fontFamily: "'Rubik', sans-serif", color: '#1B2C56' }}>
            Watch MEHO investigate
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-lg" style={{ color: '#6B7183' }}>
            A simulated cross-system investigation — from alert to root cause across 5 systems.
          </p>
        </div>

        <div className="overflow-hidden rounded-2xl border shadow-2xl" style={{ borderColor: '#27272A', backgroundColor: '#0F0F12' }}>
          {/* Chat header */}
          <div className="flex items-center justify-between border-b px-4 py-3" style={{ borderColor: '#27272A', backgroundColor: '#18181Bcc' }}>
            <div className="flex items-center gap-3">
              <img src="/meho-logo.svg" className="w-8 h-8 rounded-full" alt="" />
              <div>
                <div className="text-sm font-semibold text-white">MEHO Assistant</div>
                <div className="flex items-center gap-1.5 text-[10px]" style={{ color: '#34D399' }}>
                  <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: '#34D399', boxShadow: '0 0 6px rgba(34,197,94,0.5)' }} />
                  {isRunning ? 'Investigating...' : 'Online'}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {allSystems.map((sys) => (
                <SystemBadge key={sys.name} name={sys.name} color={sys.color} active={activeSystem === sys.name} />
              ))}
            </div>
          </div>

          {/* Chat messages */}
          <div ref={scrollRef} className="overflow-y-auto p-5 space-y-4" style={{ height: '480px', scrollBehavior: 'smooth' }}>
            {investigationSteps.slice(0, visibleSteps).map((step, i) => {
              if (step.type === 'user') {
                return (
                  <div key={i} className="flex justify-end">
                    <div
                      className="max-w-md px-4 py-3 rounded-2xl text-white text-sm"
                      style={{
                        borderTopRightRadius: '4px',
                        background: 'linear-gradient(135deg, #8051B8, #CDA3E0)',
                      }}
                    >
                      {step.action}
                    </div>
                  </div>
                );
              }

              const isSynthesis = step.type === 'synthesis';

              return (
                <div key={i} className="flex gap-3 items-start">
                  <img src="/meho-logo.svg" className="w-7 h-7 rounded-full mt-1 shrink-0" alt="" />
                  <div className="flex-1 min-w-0 space-y-2">
                    {/* Connector badge + label */}
                    <div className="flex items-center gap-2">
                      <span
                        className="rounded-md px-2 py-0.5 text-[10px] font-bold"
                        style={{
                          backgroundColor: `${step.systemColor}15`,
                          color: step.systemColor,
                        }}
                      >
                        {step.system}
                      </span>
                      <span className="text-[10px]" style={{ color: '#71717A' }}>{step.label}</span>
                    </div>

                    {/* Action text */}
                    <p className="text-sm" style={{ color: '#A1A1AA' }}>{step.action}</p>

                    {/* Result card */}
                    {step.result && (
                      <div
                        className="rounded-xl border p-4 text-sm leading-relaxed"
                        style={{
                          borderColor: isSynthesis ? '#8051B840' : '#27272A',
                          backgroundColor: isSynthesis ? '#8051B808' : '#18181B',
                          color: isSynthesis ? '#FAFAFA' : '#A1A1AA',
                        }}
                      >
                        {isSynthesis && (
                          <p className="text-xs font-semibold mb-2" style={{ color: '#8051B8' }}>Root Cause</p>
                        )}
                        {step.result}
                        {isSynthesis && (
                          <div className="flex gap-2 flex-wrap mt-3">
                            <span className="px-2.5 py-0.5 text-[10px] rounded-full" style={{ border: '1px solid #8051B840', color: '#A977C2' }}>
                              Increase pod memory to 768Mi
                            </span>
                            <span className="px-2.5 py-0.5 text-[10px] rounded-full" style={{ border: '1px solid #8051B840', color: '#A977C2' }}>
                              Investigate ESXi overcommit
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}

            {/* Typing indicator */}
            {isTyping && (
              <div className="flex gap-3 items-start">
                <img src="/meho-logo.svg" className="w-7 h-7 rounded-full mt-1 shrink-0 animate-pulse" alt="" />
                <div>
                  {activeSystem && (
                    <div className="flex items-center gap-2 mb-1">
                      <span
                        className="rounded-md px-2 py-0.5 text-[10px] font-bold"
                        style={{
                          backgroundColor: `${investigationSteps.find(s => s.system === activeSystem)?.systemColor || '#8051B8'}15`,
                          color: investigationSteps.find(s => s.system === activeSystem)?.systemColor || '#8051B8',
                        }}
                      >
                        {activeSystem}
                      </span>
                    </div>
                  )}
                  <TypingDots />
                </div>
              </div>
            )}
          </div>

          {/* Bottom bar */}
          <div className="flex items-center justify-between border-t px-4 py-3" style={{ borderColor: '#27272A', backgroundColor: '#18181Bcc' }}>
            <div className="flex items-center gap-2 text-xs" style={{ color: '#71717A' }}>
              {isRunning ? (
                <>
                  <span className="h-2 w-2 rounded-full animate-pulse" style={{ backgroundColor: '#8051B8' }} />
                  Investigating across 5 systems...
                </>
              ) : visibleSteps === investigationSteps.length ? (
                <>
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: '#34D399' }} />
                  Investigation complete — 5 systems queried
                </>
              ) : (
                <>
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: '#71717A' }} />
                  Ready
                </>
              )}
            </div>
            {hasRun && !isRunning && (
              <button
                onClick={runInvestigation}
                className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                style={{
                  backgroundColor: '#8051B815',
                  color: '#A977C2',
                  border: '1px solid #8051B830',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = '#8051B825'; }}
                onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = '#8051B815'; }}
              >
                Replay
              </button>
            )}
          </div>
        </div>

        <p className="mt-4 text-center text-sm" style={{ color: '#6B7183' }}>
          This is a simulation of a real MEHO investigation. Actual investigations query live systems.
        </p>
      </div>
    </section>
  );
}

export { InvestigationDemo };
