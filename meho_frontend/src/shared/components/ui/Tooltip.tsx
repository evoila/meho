// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState, type ReactNode } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { cn } from '../../lib/cn';

interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: 'top' | 'right' | 'bottom' | 'left';
  delay?: number;
  className?: string;
}

/**
 * Simple Tooltip Component
 * 
 * Shows a tooltip on hover with configurable position.
 * 
 * Usage:
 * ```tsx
 * <Tooltip content="Save changes" side="bottom">
 *   <Button>Save</Button>
 * </Tooltip>
 * ```
 */
export function Tooltip({ 
  content, 
  children, 
  side = 'top',
  delay = 300,
  className,
}: TooltipProps) {
  const [isVisible, setIsVisible] = useState(false);
  const [timeoutId, setTimeoutId] = useState<ReturnType<typeof setTimeout> | null>(null);

  const showTooltip = () => {
    const id = setTimeout(() => setIsVisible(true), delay);
    setTimeoutId(id);
  };

  const hideTooltip = () => {
    if (timeoutId) {
      clearTimeout(timeoutId);
      setTimeoutId(null);
    }
    setIsVisible(false);
  };

  // Position classes based on side
  const positionClasses = {
    top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
    bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
    left: 'right-full top-1/2 -translate-y-1/2 mr-2',
    right: 'left-full top-1/2 -translate-y-1/2 ml-2',
  };

  // Arrow position classes
  const arrowClasses = {
    top: 'top-full left-1/2 -translate-x-1/2 border-t-surface border-x-transparent border-b-transparent',
    bottom: 'bottom-full left-1/2 -translate-x-1/2 border-b-surface border-x-transparent border-t-transparent',
    left: 'left-full top-1/2 -translate-y-1/2 border-l-surface border-y-transparent border-r-transparent',
    right: 'right-full top-1/2 -translate-y-1/2 border-r-surface border-y-transparent border-l-transparent',
  };

  // Animation variants
  const variants = {
    top: { initial: { opacity: 0, y: 4 }, animate: { opacity: 1, y: 0 } },
    bottom: { initial: { opacity: 0, y: -4 }, animate: { opacity: 1, y: 0 } },
    left: { initial: { opacity: 0, x: 4 }, animate: { opacity: 1, x: 0 } },
    right: { initial: { opacity: 0, x: -4 }, animate: { opacity: 1, x: 0 } },
  };

  return (
    <div 
      className="relative inline-flex"
      onMouseEnter={showTooltip}
      onMouseLeave={hideTooltip}
      onFocus={showTooltip}
      onBlur={hideTooltip}
    >
      {children}
      
      <AnimatePresence>
        {isVisible && (
          <motion.div
            initial={variants[side].initial}
            animate={variants[side].animate}
            exit={variants[side].initial}
            transition={{ duration: 0.15 }}
            className={cn(
              'absolute z-50 pointer-events-none',
              positionClasses[side]
            )}
          >
            <div 
              className={cn(
                'px-3 py-1.5 text-sm whitespace-nowrap',
                'bg-surface border border-white/10 rounded-lg',
                'text-text-primary shadow-lg',
                className
              )}
              role="tooltip"
            >
              {content}
            </div>
            {/* Arrow */}
            <div 
              className={cn(
                'absolute w-0 h-0 border-4',
                arrowClasses[side]
              )}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

