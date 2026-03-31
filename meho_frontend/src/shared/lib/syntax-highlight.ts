// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Syntax Highlighting Utilities
 *
 * Provides line-by-line JSON syntax highlighting with colors
 * matching the MEHO app theme (purple primary accent).
 */
import React from 'react';

/**
 * Pattern definitions for JSON syntax highlighting.
 * Colors are aligned with MEHO design tokens.
 */
const syntaxPatterns = [
  { type: 'string', regex: /"(?:[^"\\]|\\.)*"/, color: 'text-emerald-400' },
  { type: 'number', regex: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, color: 'text-amber-400' },
  { type: 'boolean', regex: /\b(?:true|false)\b/, color: 'text-violet-400' },
  { type: 'null', regex: /\bnull\b/, color: 'text-text-tertiary' },
  { type: 'brace', regex: /[{}[\]]/, color: 'text-text-secondary' },
  { type: 'colon', regex: /:/, color: 'text-text-tertiary' },
  { type: 'comma', regex: /,/, color: 'text-text-tertiary' },
] as const;

/**
 * Syntax-highlight a single line of JSON.
 * Returns an array of React elements with appropriate color classes.
 *
 * @param line - The line of text to highlight
 * @param keyOffset - Offset for React key generation (use lineNumber * 1000)
 * @returns Array of React elements with syntax highlighting
 *
 * @example
 * ```tsx
 * const highlighted = highlightLine('  "name": "John",', 0);
 * // Returns spans with appropriate colors for keys, strings, etc.
 * ```
 */
export function highlightLine(line: string, keyOffset: number): React.ReactNode[] {
  const result: React.ReactNode[] = [];
  let i = 0;
  let key = keyOffset;

  while (i < line.length) {
    // Handle leading whitespace (preserve indentation)
    if (line[i] === ' ') {
      let spaces = '';
      while (i < line.length && line[i] === ' ') {
        spaces += ' ';
        i++;
      }
      result.push(React.createElement('span', { key: key++ }, spaces));
      continue;
    }

    let matched = false;
    for (const { regex, color, type } of syntaxPatterns) {
      const match = line.slice(i).match(new RegExp(`^${regex.source}`));
      if (match) {
        const text = match[0];

        if (type === 'string') {
          // Check if this string is a key (followed by colon)
          const afterMatch = line.slice(i + text.length).match(/^\s*:/);
          if (afterMatch) {
            // Keys in primary purple color to match app theme
            result.push(React.createElement('span', { key: key++, className: 'text-primary' }, text));
          } else {
            result.push(React.createElement('span', { key: key++, className: color }, text));
          }
        } else {
          result.push(React.createElement('span', { key: key++, className: color }, text));
        }

        i += text.length;
        matched = true;
        break;
      }
    }

    if (!matched) {
      result.push(React.createElement('span', { key: key++ }, line[i]));
      i++;
    }
  }

  return result;
}

/**
 * Formats a JSON value into a pretty-printed string.
 * Handles circular references gracefully.
 *
 * @param data - Any JSON-serializable value
 * @param indent - Number of spaces for indentation (default: 2)
 * @returns Formatted JSON string, or string representation on error
 */
export function formatJson(data: unknown, indent: number = 2): string {
  try {
    return JSON.stringify(data, null, indent);
  } catch {
    return String(data);
  }
}

/**
 * SQL keywords for syntax highlighting.
 * Used by SQLViewer component.
 */
export const SQL_KEYWORDS = [
  'SELECT',
  'FROM',
  'WHERE',
  'JOIN',
  'LEFT',
  'RIGHT',
  'INNER',
  'OUTER',
  'ON',
  'AND',
  'OR',
  'NOT',
  'IN',
  'IS',
  'NULL',
  'LIKE',
  'ORDER',
  'BY',
  'ASC',
  'DESC',
  'GROUP',
  'HAVING',
  'LIMIT',
  'OFFSET',
  'INSERT',
  'INTO',
  'VALUES',
  'UPDATE',
  'SET',
  'DELETE',
  'CREATE',
  'TABLE',
  'INDEX',
  'DROP',
  'ALTER',
  'AS',
  'DISTINCT',
  'COUNT',
  'SUM',
  'AVG',
  'MAX',
  'MIN',
  'CASE',
  'WHEN',
  'THEN',
  'ELSE',
  'END',
  'CAST',
  'COALESCE',
  'UNION',
  'ALL',
  'EXISTS',
  'BETWEEN',
] as const;

/**
 * Highlight SQL query syntax.
 * Keywords are highlighted in purple, strings in emerald, numbers in amber.
 *
 * @param query - SQL query string
 * @returns React elements with syntax highlighting
 */
export function highlightSQL(query: string): React.ReactNode[] {
  const result: React.ReactNode[] = [];
  let key = 0;

  // Tokenizer for SQL with support for escaped quotes
  const tokens = query.split(/(\s+|'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|[(),;])/);

  for (const token of tokens) {
    if (!token) continue;

    // Whitespace
    if (/^\s+$/.test(token)) {
      result.push(React.createElement('span', { key: key++ }, token));
      continue;
    }

    // String literals
    if (/^['"]/.test(token)) {
      result.push(React.createElement('span', { key: key++, className: 'text-emerald-400' }, token));
      continue;
    }

    // Numbers
    if (/^\d+(?:\.\d+)?$/.test(token)) {
      result.push(React.createElement('span', { key: key++, className: 'text-amber-400' }, token));
      continue;
    }

    // Keywords (case-insensitive)
    if (SQL_KEYWORDS.includes(token.toUpperCase() as typeof SQL_KEYWORDS[number])) {
      result.push(React.createElement('span', { key: key++, className: 'text-primary font-semibold' }, token));
      continue;
    }

    // Punctuation
    if (/^[(),;]$/.test(token)) {
      result.push(React.createElement('span', { key: key++, className: 'text-text-tertiary' }, token));
      continue;
    }

    // Default (identifiers, etc.)
    result.push(React.createElement('span', { key: key++, className: 'text-text-secondary' }, token));
  }

  return result;
}
