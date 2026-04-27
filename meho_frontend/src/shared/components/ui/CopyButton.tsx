// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Copy Button Component
 *
 * Copy-to-clipboard button with visual feedback.
 * Styled to match MEHO app theme.
 */
import { useState, useCallback, useRef, useEffect } from 'react';
import { Copy, Check } from 'lucide-react';
import { cn } from '../../lib/cn';

export interface CopyButtonProps {
  /** The data to copy (will be JSON.stringified if object) */
  data: unknown;
  /** Additional CSS classes */
  className?: string;
  /** Size variant */
  size?: 'sm' | 'md';
  /** Label to show next to icon */
  label?: string;
}

/**
 * Copy to clipboard button with feedback.
 *
 * Features:
 * - Click to copy data to clipboard
 * - Shows checkmark feedback for 2 seconds after copy
 * - Handles objects by JSON.stringifying
 *
 * @example
 * ```tsx
 * <CopyButton data={{ key: "value" }} />
 * <CopyButton data="Plain text" label="Copy" />
 * ```
 */
export function CopyButton({ data, className, size = 'md', label }: Readonly<CopyButtonProps>) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Cleanup timeout on unmount to prevent memory leaks
  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  const handleCopy = useCallback(async () => {
    try {
      const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
      await navigator.clipboard.writeText(text);
      setCopied(true);
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  }, [data]);

  const iconSize = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4';
  const padding = size === 'sm' ? 'p-1' : 'p-1.5';

  return (
    <button
      onClick={handleCopy}
      className={cn(
        'text-text-tertiary hover:text-text-primary transition-colors rounded-lg hover:bg-surface-hover',
        'flex items-center gap-1.5',
        padding,
        className
      )}
      title="Copy to clipboard"
      aria-label={copied ? 'Copied!' : 'Copy to clipboard'}
    >
      {copied ? (
        <Check className={cn(iconSize, 'text-green-400')} />
      ) : (
        <Copy className={iconSize} />
      )}
      {label && <span className="text-xs">{label}</span>}
    </button>
  );
}
