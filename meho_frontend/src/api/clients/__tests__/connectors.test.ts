// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for the connectors domain client's service-credential surface.
 *
 * These three methods (`getServiceCredentialStatus`, `setServiceCredential`,
 * `deleteServiceCredential`) are new API surfaces introduced in #349 — they
 * replace `apiClient.client.*` leaks that `CredentialManagement.tsx` used to
 * rely on. Their only production callsite is that one component, so a unit
 * test here is the only automated contract we have with the backend
 * (`meho_app/api/connectors/operations/credentials.py`).
 *
 * Uses the factory-injection pattern (`createConnectorsClient(mockTransport)`)
 * rather than the singleton + reset pattern: the factory is designed for this
 * exact shape and it avoids module-level state bleed between tests.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import {
  createConnectorsClient,
  type ServiceCredentialStatus,
  type SetServiceCredentialRequest,
} from '../connectors';

type MockedTransport = {
  transport: AxiosInstance;
  get: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
};

function makeMockTransport(): MockedTransport {
  const get = vi.fn();
  const put = vi.fn();
  const del = vi.fn();
  const post = vi.fn();
  const transport = {
    get,
    put,
    delete: del,
    post,
  } as unknown as AxiosInstance;
  return { transport, get, put, delete: del, post };
}

describe('connectorsClient — service credentials', () => {
  const connectorId = 'conn-42';

  let mock: MockedTransport;
  let client: ReturnType<typeof createConnectorsClient>;

  beforeEach(() => {
    mock = makeMockTransport();
    client = createConnectorsClient(mock.transport);
  });

  describe('getServiceCredentialStatus', () => {
    it('issues GET /api/connectors/:id/service-credential and returns the typed payload', async () => {
      const payload: ServiceCredentialStatus = {
        has_service_credential: true,
        credential_type: 'PASSWORD',
        updated_at: '2026-04-22T09:00:00Z',
      };
      mock.get.mockResolvedValueOnce({ data: payload });

      const result = await client.getServiceCredentialStatus(connectorId);

      expect(mock.get).toHaveBeenCalledTimes(1);
      expect(mock.get).toHaveBeenCalledWith(
        `/api/connectors/${connectorId}/service-credential`,
      );
      expect(result).toEqual(payload);
    });

    it('propagates the "no credential configured" null-filled shape unchanged', async () => {
      const payload: ServiceCredentialStatus = {
        has_service_credential: false,
        credential_type: null,
        updated_at: null,
      };
      mock.get.mockResolvedValueOnce({ data: payload });

      const result = await client.getServiceCredentialStatus(connectorId);

      expect(result.has_service_credential).toBe(false);
      expect(result.credential_type).toBeNull();
      expect(result.updated_at).toBeNull();
    });
  });

  describe('setServiceCredential', () => {
    it('issues PUT /api/connectors/:id/service-credential with the request body unchanged', async () => {
      const body: SetServiceCredentialRequest = {
        credential_type: 'PASSWORD',
        credentials: { username: 'svc-user', password: 'swordfish' },
      };
      mock.put.mockResolvedValueOnce({ data: undefined });

      await client.setServiceCredential(connectorId, body);

      expect(mock.put).toHaveBeenCalledTimes(1);
      expect(mock.put).toHaveBeenCalledWith(
        `/api/connectors/${connectorId}/service-credential`,
        body,
      );
    });

    it('does not transform credential-type or credentials — forwards the exact payload the backend enum expects', async () => {
      const body: SetServiceCredentialRequest = {
        credential_type: 'API_KEY',
        credentials: { api_key: 'sk-XXXX' },
      };
      mock.put.mockResolvedValueOnce({ data: undefined });

      await client.setServiceCredential(connectorId, body);

      const [, sentBody] = mock.put.mock.calls[0];
      expect(sentBody).toBe(body);
      expect(sentBody).toEqual({
        credential_type: 'API_KEY',
        credentials: { api_key: 'sk-XXXX' },
      });
    });
  });

  describe('deleteServiceCredential', () => {
    it('issues DELETE /api/connectors/:id/service-credential and resolves to void', async () => {
      mock.delete.mockResolvedValueOnce({ data: undefined });

      const result = await client.deleteServiceCredential(connectorId);

      expect(mock.delete).toHaveBeenCalledTimes(1);
      expect(mock.delete).toHaveBeenCalledWith(
        `/api/connectors/${connectorId}/service-credential`,
      );
      expect(result).toBeUndefined();
    });
  });

  describe('URL construction', () => {
    it('escapes connector IDs into the path exactly — no encoding, matches the backend route shape', async () => {
      mock.get.mockResolvedValueOnce({
        data: {
          has_service_credential: false,
          credential_type: null,
          updated_at: null,
        } satisfies ServiceCredentialStatus,
      });

      await client.getServiceCredentialStatus('weird-id-with-dashes-123');

      expect(mock.get).toHaveBeenCalledWith(
        '/api/connectors/weird-id-with-dashes-123/service-credential',
      );
    });
  });
});
