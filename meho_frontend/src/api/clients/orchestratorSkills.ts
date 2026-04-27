// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator skills domain client (CRUD + LLM-assisted generation).
 *
 * Orchestrator skills are tenant-scoped prompt snippets the orchestrator can
 * consult at planning time. This client mirrors `/api/orchestrator-skills/*`:
 * list/get/create/update/delete plus an LLM-backed content generator.
 *
 * Migrated from `lib/api-client.ts` in Phase 4 (#350).
 */
import type { AxiosInstance } from 'axios';
import type {
  OrchestratorSkillSummary,
  OrchestratorSkill,
  CreateSkillRequest,
  UpdateSkillRequest,
  GenerateSkillRequest,
  GenerateSkillResponse,
} from '../types';
import { getTransport } from './transport';

export function createOrchestratorSkillsClient(transport: AxiosInstance) {
  return {
    async listOrchestratorSkills(): Promise<OrchestratorSkillSummary[]> {
      const response = await transport.get<OrchestratorSkillSummary[]>(
        '/api/orchestrator-skills/',
      );
      return response.data;
    },

    async getOrchestratorSkill(id: string): Promise<OrchestratorSkill> {
      const response = await transport.get<OrchestratorSkill>(
        `/api/orchestrator-skills/${id}`,
      );
      return response.data;
    },

    async createOrchestratorSkill(data: CreateSkillRequest): Promise<OrchestratorSkill> {
      const response = await transport.post<OrchestratorSkill>(
        '/api/orchestrator-skills/',
        data,
      );
      return response.data;
    },

    async updateOrchestratorSkill(
      id: string,
      data: UpdateSkillRequest,
    ): Promise<OrchestratorSkill> {
      const response = await transport.put<OrchestratorSkill>(
        `/api/orchestrator-skills/${id}`,
        data,
      );
      return response.data;
    },

    async deleteOrchestratorSkill(id: string): Promise<void> {
      await transport.delete(`/api/orchestrator-skills/${id}`);
    },

    async generateOrchestratorSkill(
      data: GenerateSkillRequest,
    ): Promise<GenerateSkillResponse> {
      const response = await transport.post<GenerateSkillResponse>(
        '/api/orchestrator-skills/generate',
        data,
      );
      return response.data;
    },
  };
}

let orchestratorSkillsClient: ReturnType<typeof createOrchestratorSkillsClient> | null = null;

export function getOrchestratorSkillsClient(): ReturnType<typeof createOrchestratorSkillsClient> {
  if (!orchestratorSkillsClient) {
    orchestratorSkillsClient = createOrchestratorSkillsClient(getTransport());
  }
  return orchestratorSkillsClient;
}
