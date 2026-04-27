// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * MSW handlers for mocking MEHO API
 * 
 * Note: Due to MSW compatibility issues with jsdom in Vitest,
 * these handlers are provided for reference and for environments
 * where MSW works properly (like integration tests with real browser).
 * 
 * For unit tests, use vi.mock() to mock API calls directly.
 */

// Export empty handlers array - MSW is not used in unit tests
// due to compatibility issues with jsdom localStorage
export const handlers: unknown[] = [];

// Re-export for backwards compatibility
export const createHandlers = async () => {
  // This function can be used in environments where MSW works
  return [];
};
