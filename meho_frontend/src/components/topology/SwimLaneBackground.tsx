// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SwimLaneBackground.tsx - Horizontal tier lane backgrounds with labels
 *
 * Renders absolutely positioned horizontal bands behind the React Flow canvas,
 * each representing an infrastructure tier (Application, Service, Pod, etc.)
 * with a subtle background color and a sticky label on the left.
 *
 * Only rendered in hierarchical layout mode when tier bounds are available.
 * Uses pointer-events-none and z-0 so React Flow interactions pass through.
 */

import type { SwimLane } from './tierMapping';

// ============================================================================
// Tailwind color to CSS mapping
// ============================================================================

/**
 * Map Tailwind bg classes to actual CSS rgba values.
 * We can't use dynamic Tailwind classes in absolute-positioned elements
 * overlaying React Flow (they'd need to be in the Tailwind content scan).
 * Using inline styles for reliable rendering.
 */
const COLOR_MAP: Record<string, string> = {
  'bg-purple-500/5': 'rgba(168, 85, 247, 0.05)',
  'bg-blue-500/5': 'rgba(59, 130, 246, 0.05)',
  'bg-cyan-500/5': 'rgba(6, 182, 212, 0.05)',
  'bg-green-500/5': 'rgba(34, 197, 94, 0.05)',
  'bg-yellow-500/5': 'rgba(234, 179, 8, 0.05)',
  'bg-orange-500/5': 'rgba(249, 115, 22, 0.05)',
  'bg-red-500/5': 'rgba(239, 68, 68, 0.05)',
};

const LABEL_COLOR_MAP: Record<string, string> = {
  Application: 'rgba(168, 85, 247, 0.6)',
  Service: 'rgba(59, 130, 246, 0.6)',
  Workload: 'rgba(6, 182, 212, 0.6)',
  Pod: 'rgba(34, 197, 94, 0.6)',
  Node: 'rgba(234, 179, 8, 0.6)',
  VM: 'rgba(249, 115, 22, 0.6)',
  Cloud: 'rgba(239, 68, 68, 0.6)',
};

// ============================================================================
// Component
// ============================================================================

interface SwimLaneBackgroundProps {
  lanes: SwimLane[];
}

export function SwimLaneBackground({ lanes }: SwimLaneBackgroundProps) {
  if (lanes.length === 0) return null;

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: 'none',
        zIndex: 0,
        overflow: 'hidden',
      }}
    >
      {lanes.map((lane) => {
        const bgColor = COLOR_MAP[lane.color] ?? 'rgba(107, 114, 128, 0.05)';
        const labelColor = LABEL_COLOR_MAP[lane.tier] ?? 'rgba(107, 114, 128, 0.5)';
        const height = lane.yEnd - lane.yStart;

        return (
          <div
            key={lane.tier}
            style={{
              position: 'absolute',
              left: 0,
              right: 0,
              top: `${lane.yStart}px`,
              height: `${height}px`,
              backgroundColor: bgColor,
              borderTop: '1px solid rgba(55, 65, 81, 0.3)',
            }}
          >
            <span
              style={{
                position: 'sticky',
                left: 16,
                top: 8,
                display: 'inline-block',
                padding: '2px 8px',
                fontSize: '11px',
                fontWeight: 600,
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
                color: labelColor,
                userSelect: 'none',
              }}
            >
              {lane.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}
