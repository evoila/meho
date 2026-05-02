// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for the knowledge domain client.
 *
 * Focuses on document-versioning because those endpoints have non-trivial
 * URL shapes and multipart payloads that were historically covered by
 * `lib/__tests__/api-client.versions.test.ts`. Under the #263 refactor
 * the implementation now lives on `createKnowledgeClient`, so the
 * coverage moves here.
 *
 * The Axios instance is replaced with a vi-mocked shim so assertions can
 * target request URLs and payloads without network I/O.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import { createKnowledgeClient } from '../knowledge';
import type { DocumentFamilyVersionsResponse } from '../../types/knowledge';

interface MockAxiosClient {
  post: ReturnType<typeof vi.fn>;
  get: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
}

function createMockTransport(): MockAxiosClient {
  return {
    post: vi.fn(),
    get: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  };
}

describe('knowledge client -- document versioning', () => {
  let mockTransport: MockAxiosClient;
  let client: ReturnType<typeof createKnowledgeClient>;

  beforeEach(() => {
    mockTransport = createMockTransport();
    client = createKnowledgeClient(mockTransport as unknown as AxiosInstance);
  });

  describe('uploadDocumentVersion', () => {
    it('posts multipart form-data to the per-document versions endpoint', async () => {
      mockTransport.post.mockResolvedValueOnce({
        data: { job_id: 'job-9', status: 'processing' },
      });

      const file = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], 'doc.pdf', {
        type: 'application/pdf',
      });

      const result = await client.uploadDocumentVersion('doc-123', {
        file,
        doc_version: 'v10',
      });

      expect(result).toEqual({ job_id: 'job-9', status: 'processing' });
      expect(mockTransport.post).toHaveBeenCalledTimes(1);

      const [url, payload, options] = mockTransport.post.mock.calls[0];
      expect(url).toBe('/api/knowledge/documents/doc-123/versions');
      expect(payload).toBeInstanceOf(FormData);
      expect((payload as FormData).get('doc_version')).toBe('v10');
      expect((payload as FormData).get('file')).toBeInstanceOf(File);
      expect(options).toEqual({
        headers: { 'Content-Type': 'multipart/form-data' },
      });
    });

    it('propagates server errors (e.g., 409 duplicate version) to the caller', async () => {
      mockTransport.post.mockRejectedValueOnce(
        Object.assign(new Error('Request failed with status code 409'), {
          response: { status: 409, data: { detail: "Version 'v10' already exists." } },
        }),
      );

      const file = new File(['%PDF'], 'doc.pdf', { type: 'application/pdf' });

      await expect(
        client.uploadDocumentVersion('doc-123', { file, doc_version: 'v10' }),
      ).rejects.toThrow(/409/);
    });
  });

  describe('listDocumentVersions', () => {
    it('fetches family versions by family id and returns the response body', async () => {
      const body: DocumentFamilyVersionsResponse = {
        family_id: 'fam-1',
        family_name: 'VCF Admin Guide',
        versions: [
          {
            job_id: 'job-a',
            doc_version: 'v9',
            filename: 'vcf9.pdf',
            status: 'completed',
            chunks_created: 42,
            started_at: '2025-01-01T00:00:00Z',
            completed_at: '2025-01-01T00:02:00Z',
          },
        ],
      };
      mockTransport.get.mockResolvedValueOnce({ data: body });

      const result = await client.listDocumentVersions('fam-1');

      expect(result).toEqual(body);
      expect(mockTransport.get).toHaveBeenCalledWith(
        '/api/knowledge/families/fam-1/versions',
      );
    });
  });
});
