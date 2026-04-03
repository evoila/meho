// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * JSON Viewer Component
 *
 * Syntax-highlighted JSON viewer with line numbers (editor-style).
 * Styled to match MEHO app theme.
 */
import { useMemo } from 'react';
import { highlightLine, formatJson } from '../../lib/syntax-highlight';
import { cn } from '../../lib/cn';

export interface JsonViewerProps {
  /** The data to display as JSON */
  data: unknown;
  /** Additional CSS classes */
  className?: string;
  /** Whether to show line numbers (default: true) */
  showLineNumbers?: boolean;
  /** Maximum height before scrolling (CSS value) */
  maxHeight?: string;
}

/**
 * Syntax-highlighted JSON viewer with line numbers.
 *
 * Features:
 * - Line numbers gutter (optional)
 * - Syntax highlighting for keys, strings, numbers, booleans, null
 * - Horizontal scroll for long lines
 * - Theme-consistent colors (purple keys, emerald strings, etc.)
 *
 * @example
 * ```tsx
 * <JsonViewer data={{ name: "John", age: 30 }} />
 * ```
 */
export function JsonViewer({
  data,
  className,
  showLineNumbers = true,
  maxHeight,
}: Readonly<JsonViewerProps>) {
  const { lines, lineNumWidth } = useMemo(() => {
    const json = formatJson(data);
    const parsedLines = json.split('\n');
    return {
      lines: parsedLines,
      lineNumWidth: String(parsedLines.length).length,
    };
  }, [data]);

  // Check if this is raw text (not JSON parseable) or actual JSON
  const isValidJson = useMemo(() => {
    try {
      if (typeof data === 'string') {
        JSON.parse(data);
        return true;
      }
      return true; // Objects/arrays are always valid
    } catch {
      return false;
    }
  }, [data]);

  // If it's just a plain string that's not JSON, show it plainly
  if (typeof data === 'string' && !isValidJson) {
    return (
      <div className={cn('text-text-secondary text-sm whitespace-pre-wrap font-mono', className)}>
        {data}
      </div>
    );
  }

  return (
    <div
      className={cn('flex text-[15px] font-mono leading-6', className)}
      style={{ maxHeight }}
    >
      {/* Line numbers gutter */}
      {showLineNumbers && (
        <div className="flex-shrink-0 select-none text-right pr-4 border-r border-border/30 text-text-tertiary min-w-[3rem]">
          {lines.map((_, idx) => (
            <div key={`line-${idx}`}>{String(idx + 1).padStart(lineNumWidth, ' ')}</div>
          ))}
        </div>
      )}

      {/* Code content */}
      <div className={cn('flex-1 overflow-x-auto', showLineNumbers && 'pl-4')}>
        {lines.map((line, idx) => (
          <div key={`line-${idx}`} className="whitespace-pre">
            {highlightLine(line, idx * 1000)}
          </div>
        ))}
      </div>
    </div>
  );
}
