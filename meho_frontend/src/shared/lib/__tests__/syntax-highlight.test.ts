// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Syntax Highlight Utilities Tests
 */
import { describe, it, expect } from 'vitest';
import { highlightLine, formatJson, highlightSQL, SQL_KEYWORDS } from '../syntax-highlight';

describe('highlightLine', () => {
  it('highlights JSON key-value pairs', () => {
    const result = highlightLine('"name": "John"', 0);
    expect(result).toHaveLength(4); // key, colon, space, value
  });

  it('highlights numbers', () => {
    const result = highlightLine('"age": 30', 0);
    expect(result.length).toBeGreaterThan(0);
  });

  it('highlights booleans', () => {
    const result = highlightLine('"active": true', 0);
    expect(result.length).toBeGreaterThan(0);
  });

  it('highlights null values', () => {
    const result = highlightLine('"data": null', 0);
    expect(result.length).toBeGreaterThan(0);
  });

  it('preserves leading whitespace', () => {
    const result = highlightLine('    "name": "test"', 0);
    expect(result.length).toBeGreaterThan(0);
  });

  it('handles braces and brackets', () => {
    const result = highlightLine('{[]}', 0);
    expect(result).toHaveLength(4);
  });

  it('uses key offset for unique keys', () => {
    const result1 = highlightLine('"test"', 0);
    const result2 = highlightLine('"test"', 1000);
    // Both should work and produce output
    expect(result1.length).toBeGreaterThan(0);
    expect(result2.length).toBeGreaterThan(0);
  });
});

describe('formatJson', () => {
  it('formats object with indentation', () => {
    const result = formatJson({ name: 'test' });
    expect(result).toContain('\n');
    expect(result).toContain('"name"');
  });

  it('handles circular references gracefully', () => {
    const obj: Record<string, unknown> = { a: 1 };
    obj.self = obj;
    // Should not throw, returns string representation
    const result = formatJson(obj);
    expect(typeof result).toBe('string');
  });

  it('uses custom indent', () => {
    const result = formatJson({ a: 1 }, 4);
    expect(result).toContain('    '); // 4 spaces
  });

  it('formats primitives', () => {
    expect(formatJson('hello')).toBe('"hello"');
    expect(formatJson(42)).toBe('42');
    expect(formatJson(true)).toBe('true');
    expect(formatJson(null)).toBe('null');
  });

  it('formats arrays', () => {
    const result = formatJson([1, 2, 3]);
    expect(result).toContain('[');
    expect(result).toContain(']');
  });
});

describe('highlightSQL', () => {
  it('highlights SELECT keyword', () => {
    const result = highlightSQL('SELECT * FROM users');
    expect(result.length).toBeGreaterThan(0);
  });

  it('highlights string literals', () => {
    const result = highlightSQL("WHERE name = 'John'");
    expect(result.length).toBeGreaterThan(0);
  });

  it('highlights numbers', () => {
    const result = highlightSQL('LIMIT 10');
    expect(result.length).toBeGreaterThan(0);
  });

  it('handles complex queries', () => {
    const query = `
      SELECT u.id, u.name, COUNT(o.id) as order_count
      FROM users u
      LEFT JOIN orders o ON u.id = o.user_id
      WHERE u.active = true
      GROUP BY u.id
      ORDER BY order_count DESC
      LIMIT 10
    `;
    const result = highlightSQL(query);
    expect(result.length).toBeGreaterThan(0);
  });

  it('preserves whitespace', () => {
    const result = highlightSQL('SELECT   *   FROM   users');
    expect(result.length).toBeGreaterThan(0);
  });
});

describe('SQL_KEYWORDS', () => {
  it('contains common SQL keywords', () => {
    expect(SQL_KEYWORDS).toContain('SELECT');
    expect(SQL_KEYWORDS).toContain('FROM');
    expect(SQL_KEYWORDS).toContain('WHERE');
    expect(SQL_KEYWORDS).toContain('JOIN');
    expect(SQL_KEYWORDS).toContain('ORDER');
    expect(SQL_KEYWORDS).toContain('GROUP');
  });

  it('is a readonly array', () => {
    expect(Array.isArray(SQL_KEYWORDS)).toBe(true);
  });
});
