// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Topology API Unit Tests (TASK-144 Phase 4)
 * 
 * Tests for suggestion-related API functions
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  fetchSuggestions,
  approveSuggestion,
  rejectSuggestion,
  verifySuggestion,
  triggerDiscovery,
} from '../topologyApi';

// Mock the API client (token is now in memory, not localStorage)
vi.mock('../api-client', () => ({
  getAPIClient: vi.fn(() => ({
    getToken: vi.fn(() => 'mock-token'),
  })),
}));

describe('Topology API - Suggestions', () => {
  const mockFetch = vi.fn();
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = mockFetch;
    mockFetch.mockReset();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  describe('fetchSuggestions', () => {
    it('fetches suggestions successfully', async () => {
      const mockResponse = {
        suggestions: [
          {
            id: 'suggestion-1',
            entity_a_id: 'entity-a',
            entity_b_id: 'entity-b',
            entity_a_name: 'API Connector',
            entity_b_name: 'K8s Ingress',
            confidence: 0.95,
            match_type: 'hostname_match',
            status: 'pending',
            suggested_at: '2025-01-03T10:00:00Z',
            tenant_id: 'tenant-1',
          },
        ],
        total: 1,
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await fetchSuggestions();

      expect(result.suggestions).toHaveLength(1);
      expect(result.total).toBe(1);
      expect(result.suggestions[0].entity_a_name).toBe('API Connector');
    });

    it('passes limit and offset parameters', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ suggestions: [], total: 0 }),
      });

      await fetchSuggestions({ limit: 50, offset: 10 });

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('limit=50'),
        expect.any(Object)
      );
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('offset=10'),
        expect.any(Object)
      );
    });

    it('throws error on failed request', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        statusText: 'Internal Server Error',
      });

      await expect(fetchSuggestions()).rejects.toThrow('Failed to fetch suggestions');
    });

    it('includes authorization header', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ suggestions: [], total: 0 }),
      });

      await fetchSuggestions();

      expect(mockFetch).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'Bearer mock-token',
          }),
        })
      );
    });
  });

  describe('approveSuggestion', () => {
    it('approves a suggestion successfully', async () => {
      const mockResponse = {
        success: true,
        message: 'Created SAME_AS relationship',
        same_as_created: true,
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await approveSuggestion('suggestion-123');

      expect(result.success).toBe(true);
      expect(result.same_as_created).toBe(true);
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/suggestions/suggestion-123/approve'),
        expect.objectContaining({
          method: 'POST',
        })
      );
    });

    it('throws error with detail on failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        statusText: 'Bad Request',
        json: () => Promise.resolve({ detail: 'Suggestion already approved' }),
      });

      await expect(approveSuggestion('suggestion-123')).rejects.toThrow('Suggestion already approved');
    });
  });

  describe('rejectSuggestion', () => {
    it('rejects a suggestion successfully', async () => {
      const mockResponse = {
        success: true,
        message: 'Suggestion rejected',
        same_as_created: false,
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await rejectSuggestion('suggestion-123');

      expect(result.success).toBe(true);
      expect(result.same_as_created).toBe(false);
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/suggestions/suggestion-123/reject'),
        expect.objectContaining({
          method: 'POST',
        })
      );
    });

    it('throws error on failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        statusText: 'Not Found',
        json: () => Promise.resolve({ detail: 'Suggestion not found' }),
      });

      await expect(rejectSuggestion('suggestion-123')).rejects.toThrow('Suggestion not found');
    });
  });

  describe('verifySuggestion', () => {
    it('triggers LLM verification successfully', async () => {
      const mockResponse = {
        success: true,
        suggestion_id: 'suggestion-123',
        new_status: 'approved',
        llm_result: {
          is_same: true,
          confidence: 0.92,
          reasoning: 'Both entities refer to the same API.',
        },
        message: 'LLM verified and approved',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await verifySuggestion('suggestion-123');

      expect(result.success).toBe(true);
      expect(result.new_status).toBe('approved');
      expect(result.llm_result?.reasoning).toBe('Both entities refer to the same API.');
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/suggestions/suggestion-123/verify'),
        expect.objectContaining({
          method: 'POST',
        })
      );
    });

    it('handles LLM verification that leaves suggestion pending', async () => {
      const mockResponse = {
        success: true,
        suggestion_id: 'suggestion-123',
        new_status: 'pending',
        llm_result: {
          is_same: null,
          confidence: 0.5,
          reasoning: 'Unable to determine with confidence.',
        },
        message: 'LLM uncertain, left for manual review',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await verifySuggestion('suggestion-123');

      expect(result.success).toBe(true);
      expect(result.new_status).toBe('pending');
    });

    it('throws error on verification failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        statusText: 'Internal Server Error',
        json: () => Promise.resolve({ detail: 'LLM verification failed' }),
      });

      await expect(verifySuggestion('suggestion-123')).rejects.toThrow('LLM verification failed');
    });
  });

  describe('triggerDiscovery', () => {
    it('triggers discovery successfully', async () => {
      const mockResponse = {
        success: true,
        suggestions_created: 3,
        suggestions_skipped_existing: 5,
        suggestions_skipped_ineligible: 2,
        total_pairs_analyzed: 100,
        message: 'Discovery completed: created 3 suggestions',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await triggerDiscovery();

      expect(result.success).toBe(true);
      expect(result.suggestions_created).toBe(3);
      expect(result.total_pairs_analyzed).toBe(100);
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/suggestions/discover'),
        expect.objectContaining({
          method: 'POST',
        })
      );
    });

    it('passes min_similarity, limit, and verify parameters', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          success: true,
          suggestions_created: 0,
          suggestions_skipped_existing: 0,
          suggestions_skipped_ineligible: 0,
          total_pairs_analyzed: 0,
          message: 'No pairs found',
        }),
      });

      await triggerDiscovery({ min_similarity: 0.8, limit: 25, verify: true });

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('min_similarity=0.8'),
        expect.any(Object)
      );
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('limit=25'),
        expect.any(Object)
      );
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('verify=true'),
        expect.any(Object)
      );
    });

    it('throws error on discovery failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        statusText: 'Internal Server Error',
        json: () => Promise.resolve({ detail: 'Discovery failed: no embeddings found' }),
      });

      await expect(triggerDiscovery()).rejects.toThrow('Discovery failed: no embeddings found');
    });

    it('calls endpoint without query params when no params provided', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          success: true,
          suggestions_created: 0,
          suggestions_skipped_existing: 0,
          suggestions_skipped_ineligible: 0,
          total_pairs_analyzed: 0,
          message: 'No pairs found',
        }),
      });

      await triggerDiscovery();

      // Should not have query params
      const calledUrl = mockFetch.mock.calls[0][0] as string;
      expect(calledUrl).toMatch(/\/suggestions\/discover$/);
    });
  });
});

