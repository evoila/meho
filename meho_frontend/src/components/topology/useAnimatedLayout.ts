// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useAnimatedLayout.ts - requestAnimationFrame-based node position tweening
 *
 * Smoothly animates React Flow nodes from their current positions to new
 * target positions using rAF (NOT CSS transitions, which don't work with
 * React Flow's DOM replacement strategy).
 *
 * Uses ease-out cubic: 1 - (1-t)^3 for natural deceleration.
 */

import { useCallback, useRef, useEffect } from 'react';
import { useReactFlow, type Node } from '@xyflow/react';

const ANIMATION_DURATION = 500; // ms

/**
 * Hook that provides smooth animated position transitions for React Flow nodes.
 *
 * Must be called inside a ReactFlowProvider context.
 *
 * Returns `animateToPositions(targetNodes)` which initiates the animation.
 * Only calls `fitView()` after the animation completes (not during frames).
 */
export function useAnimatedLayout() {
  const { setNodes, getNodes, fitView } = useReactFlow();
  const animationRef = useRef<number | null>(null);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (animationRef.current !== null) {
        cancelAnimationFrame(animationRef.current);
        animationRef.current = null;
      }
    };
  }, []);

  const animateToPositions = useCallback(
    (targetNodes: Node[]) => {
      // Cancel any running animation
      if (animationRef.current !== null) {
        cancelAnimationFrame(animationRef.current);
        animationRef.current = null;
      }

      // Capture starting positions
      const currentNodes = getNodes();
      const startPositions = new Map(
        currentNodes.map((n) => [n.id, { x: n.position.x, y: n.position.y }]),
      );

      // Build target position map
      const targetPositions = new Map(
        targetNodes.map((n) => [n.id, { x: n.position.x, y: n.position.y }]),
      );

      const startTime = performance.now();

      const animate = (now: number) => {
        const elapsed = now - startTime;
        const t = Math.min(elapsed / ANIMATION_DURATION, 1);
        // Ease-out cubic: 1 - (1-t)^3
        const eased = 1 - Math.pow(1 - t, 3);

        setNodes((nodes) =>
          nodes.map((node) => {
            const start = startPositions.get(node.id);
            const target = targetPositions.get(node.id);
            if (!start || !target) return node;

            return {
              ...node,
              position: {
                x: start.x + (target.x - start.x) * eased,
                y: start.y + (target.y - start.y) * eased,
              },
            };
          }),
        );

        if (t < 1) {
          animationRef.current = requestAnimationFrame(animate);
        } else {
          animationRef.current = null;
          // Fit view only after animation completes
          fitView({ duration: 200 });
        }
      };

      animationRef.current = requestAnimationFrame(animate);
    },
    [setNodes, getNodes, fitView],
  );

  return { animateToPositions };
}
