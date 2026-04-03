// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Utility for merging Tailwind CSS classes
 * 
 * Combines clsx for conditional classes with tailwind-merge
 * to handle class conflicts properly.
 * 
 * @example
 * cn('px-2 py-1', condition && 'bg-primary', className)
 * cn('bg-red-500', 'bg-blue-500') // => 'bg-blue-500' (properly merged)
 */
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

