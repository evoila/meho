// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * MSW server setup for tests
 * 
 * Note: MSW 2.x has issues with jsdom localStorage in some environments.
 * We provide a mock server that does nothing for unit tests.
 */

// Create a mock server with no-op methods for environments where MSW doesn't work
// For integration tests, use the actual MSW server by importing from 'msw/node' directly
export const server = {
  listen: () => {},
  close: () => {},
  resetHandlers: () => {},
  use: (..._handlers: unknown[]) => {},
  restoreHandlers: () => {},
  listHandlers: () => [],
  events: {
    on: () => {},
    removeListener: () => {},
  },
};

// Note: For tests that need actual MSW mocking, 
// import setupServer directly and set up handlers inline
