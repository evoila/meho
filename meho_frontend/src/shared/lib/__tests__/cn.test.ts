// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for cn (classNames) utility
 */
import { describe, it, expect } from 'vitest';
import { cn } from '../cn';

describe('cn', () => {
  describe('basic functionality', () => {
    it('returns empty string for no arguments', () => {
      expect(cn()).toBe('');
    });

    it('returns single class unchanged', () => {
      expect(cn('px-4')).toBe('px-4');
    });

    it('joins multiple classes with space', () => {
      expect(cn('px-4', 'py-2')).toBe('px-4 py-2');
    });

    it('handles string with multiple classes', () => {
      expect(cn('px-4 py-2 bg-blue-500')).toBe('px-4 py-2 bg-blue-500');
    });
  });

  describe('conditional classes', () => {
    it('includes truthy conditional classes', () => {
      const shouldInclude = true;
      expect(cn('base', shouldInclude && 'included')).toBe('base included');
    });

    it('excludes falsy conditional classes', () => {
      const shouldExclude = false;
      expect(cn('base', shouldExclude && 'excluded')).toBe('base');
    });

    it('handles undefined values', () => {
      expect(cn('base', undefined, 'end')).toBe('base end');
    });

    it('handles null values', () => {
      expect(cn('base', null, 'end')).toBe('base end');
    });

    it('handles empty strings', () => {
      expect(cn('base', '', 'end')).toBe('base end');
    });
  });

  describe('tailwind-merge conflict resolution', () => {
    it('resolves padding conflicts (later wins)', () => {
      expect(cn('px-2', 'px-4')).toBe('px-4');
    });

    it('resolves margin conflicts', () => {
      expect(cn('mt-2', 'mt-8')).toBe('mt-8');
    });

    it('resolves background color conflicts', () => {
      expect(cn('bg-red-500', 'bg-blue-500')).toBe('bg-blue-500');
    });

    it('resolves text color conflicts', () => {
      expect(cn('text-white', 'text-gray-900')).toBe('text-gray-900');
    });

    it('keeps non-conflicting classes', () => {
      expect(cn('px-4 py-2', 'bg-blue-500', 'px-8')).toBe('py-2 bg-blue-500 px-8');
    });

    it('resolves font weight conflicts', () => {
      expect(cn('font-normal', 'font-bold')).toBe('font-bold');
    });

    it('resolves width conflicts', () => {
      expect(cn('w-full', 'w-1/2')).toBe('w-1/2');
    });

    it('resolves flex conflicts', () => {
      expect(cn('flex-row', 'flex-col')).toBe('flex-col');
    });
  });

  describe('array inputs', () => {
    it('handles array of classes', () => {
      expect(cn(['px-4', 'py-2'])).toBe('px-4 py-2');
    });

    it('handles mixed arrays and strings', () => {
      expect(cn('base', ['px-4', 'py-2'], 'end')).toBe('base px-4 py-2 end');
    });
  });

  describe('object inputs', () => {
    it('includes classes with truthy values', () => {
      expect(cn({ 'px-4': true, 'py-2': true })).toBe('px-4 py-2');
    });

    it('excludes classes with falsy values', () => {
      expect(cn({ 'px-4': true, 'py-2': false })).toBe('px-4');
    });

    it('handles mixed object and string inputs', () => {
      expect(cn('base', { 'px-4': true, excluded: false }, 'end')).toBe(
        'base px-4 end'
      );
    });
  });

  describe('real-world usage patterns', () => {
    it('handles component variant pattern', () => {
      // Using variables that TypeScript can't narrow to test the cn function
      const getVariant = () => 'primary' as 'primary' | 'secondary';
      const getSize = () => 'md' as 'sm' | 'md' | 'lg';
      const variant = getVariant();
      const size = getSize();
      const isDisabled = false;

      const result = cn(
        'btn',
        variant === 'primary' && 'bg-primary-500 text-white',
        variant === 'secondary' && 'bg-gray-200 text-gray-800',
        size === 'sm' && 'px-2 py-1 text-sm',
        size === 'md' && 'px-4 py-2 text-base',
        size === 'lg' && 'px-6 py-3 text-lg',
        isDisabled && 'opacity-50 cursor-not-allowed'
      );

      expect(result).toBe('btn bg-primary-500 text-white px-4 py-2 text-base');
    });

    it('handles className override pattern', () => {
      const baseClasses = 'px-4 py-2 bg-blue-500 text-white';
      const userClassName = 'bg-red-500 mt-4';

      expect(cn(baseClasses, userClassName)).toBe('px-4 py-2 text-white bg-red-500 mt-4');
    });

    it('handles hover/focus state pattern', () => {
      expect(
        cn(
          'bg-white',
          'hover:bg-gray-100',
          'focus:ring-2 focus:ring-blue-500'
        )
      ).toBe('bg-white hover:bg-gray-100 focus:ring-2 focus:ring-blue-500');
    });

    it('handles responsive breakpoint pattern', () => {
      expect(
        cn('w-full', 'md:w-1/2', 'lg:w-1/3')
      ).toBe('w-full md:w-1/2 lg:w-1/3');
    });
  });
});

